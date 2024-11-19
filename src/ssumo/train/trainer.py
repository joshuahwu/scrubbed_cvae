import torch
from ssumo.train.losses import get_batch_loss, balance_disentangle
from ssumo.train.mutual_inf import MutInfoEstimator
from ssumo.model.disentangle import MovingAvgLeastSquares, QuadraticDiscriminantFilter
from ssumo.plot.eval import loss as plt_loss
from ssumo.eval import generative_restrictiveness
from ssumo.eval import cluster
import torch.optim as optim
import tqdm
import pickle
import functools
import time
import wandb
from ssumo.eval.metrics import (
    linear_rand_cv,
    mlp_rand_cv,
    log_class_rand_cv,
    qda_rand_cv,
    shannon_entropy,
)
import torch.nn.functional as F
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score
import numpy as np
from pandas import crosstab
from scipy.optimize import linear_sum_assignment
import numpy.typing as npt
# from torch.profiler import profile, record_function, ProfilerActivity
from line_profiler import profile
from pathlib import Path

class CyclicalBetaAnnealing(torch.nn.Module):
    def __init__(self, beta_max=1, len_cycle=100, R=0.5):
        self.beta_max = beta_max
        self.len_cycle = len_cycle
        self.R = R
        self.len_increasing = int(len_cycle * R)

    def get(self, epoch):
        remainder = (epoch - 1) % self.len_cycle
        if remainder >= self.len_increasing:
            beta = self.beta_max
        else:
            beta = self.beta_max * remainder / self.len_increasing

        return beta


def get_beta_schedule(schedule, beta):
    if schedule == "cyclical":
        print("Initializing cyclical beta annealing")
        beta_scheduler = CyclicalBetaAnnealing(beta_max=beta)
    else:
        print("No beta annealing selected")
        beta_scheduler = None

    return beta_scheduler


def predict_batch(model, data, disentangle_keys=None):
    data_i = {
        k: v
        for k, v in data.items()
        if (k in disentangle_keys) or (k in ["x6d", "root", "var"])
    }

    return model(data_i)


def get_optimizer_and_lr_scheduler(
    model, train_config, load_path=None, start_epoch=None
):
    if train_config["optimizer"] == "adam":
        print("Initializing Adam optimizer ...")
        optimizer = optim.Adam(model.parameters(), lr=train_config["lr"])
    elif train_config["optimizer"] == "adamw":
        print("Initializing AdamW optimizer ...")
        optimizer = optim.AdamW(model.parameters(), lr=train_config["lr"])
    elif train_config["optimizer"] == "sgd":
        print("Initializing SGD optimizer ...")
        optimizer = optim.SGD(
            model.parameters(), lr=train_config["lr"], momentum=0.2, nesterov=True
        )
    else:
        raise ValueError("No valid optimizer selected")

    if train_config["lr_schedule"] == "cawr":
        print("Initializing cosine annealing w/warm restarts learning rate scheduler")
        scheduler = optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, T_0=50)
    elif train_config["lr_schedule"] is None:
        print("No learning rate scheduler selected")
        scheduler = None

    if load_path is not None:
        if Path("{}/checkpoints/epoch_{}.pth".format(load_path, start_epoch)).exists():
            checkpoint = torch.load(
                "{}/checkpoints/epoch_{}.pth".format(load_path, start_epoch)
            )
            optimizer.load_state_dict(checkpoint["optimizer"])
            scheduler = checkpoint["lr_scheduler"]

    return optimizer, scheduler

@profile
def train_test_epoch(
    config,
    model,
    loader,
    device,
    epoch,
    optimizer=None,
    scheduler=None,
    mode="train",
    get_z=False,
):

    if mode == "train":
        model.train()
        grad_env = torch.enable_grad
    elif mode == "test":
        model.eval()
        grad_env = torch.no_grad
    else:
        raise ValueError("This mode is not recognized.")
    with grad_env():
        z = []
        model.mi_estimator = None
        epoch_metrics = {k: 0 for k in ["total"] + list(config["loss"].keys())}
        for batch_idx, data in enumerate(loader):
            # with profile(activities = [ProfilerActivity.CPU, ProfilerActivity.CUDA], record_shapes=True) as prof:
            #     with record_function("model_inference"):
            data = {k: v.to(device) for k, v in data.items()}

            data_o = predict_batch(model, data, model.disentangle_keys)

            if mode == "Train":
                if bool(model.disentangle):
                    for method in model.disentangle.keys():
                        if method == "adversarial_net":
                            for k in model.disentangle[method].keys():
                                model.disentangle[method][k].fit(
                                    data_o["mu"].detach(),
                                    data_o["var"].clone(),
                                    model.disentangle_keys.index(k),
                                    None,
                                    config["disentangle"]["n_iter"],
                                )

            if get_z:
                z += [data_o["mu"].clone().detach()]

            # print(config["loss"])
            batch_loss = get_batch_loss(
                model,
                data,
                data_o,
                config["loss"],
                config["disentangle"],
            )

            if mode == "train":
                for param in model.parameters():
                    param.grad = None

                batch_loss["total"].backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1e6)
                optimizer.step()
                if scheduler is not None:
                    scheduler.step(epoch + batch_idx / len(loader))

                if bool(model.disentangle):
                    for method in model.disentangle.keys():
                        if method in ["moving_avg_lsq", "moving_avg", "qda"]:
                            for k in model.disentangle[method].keys():
                                model.disentangle[method][k].update(
                                    data_o["mu"].detach().clone(),
                                    data[k].detach().clone(),
                                )

            epoch_metrics = {
                k: v + batch_loss[k].detach() for k, v in epoch_metrics.items()
            }

            if "mcmi" in config["loss"].keys():
                updated_data_o = model.encode(data)

                model.mi_estimator = MutInfoEstimator(
                    x_s=updated_data_o["mu"].detach().clone(),
                    y_s=data_o["var"].clone(),
                    bandwidth=config["disentangle"]["bandwidth"],
                    var_mode=config["disentangle"]["var_mode"],
                    model_var=(
                        updated_data_o["L"].detach().clone()
                        if "L" in updated_data_o.keys()
                        else None
                    ),
                    device=device,
                )

        for k, v in epoch_metrics.items():
            epoch_metrics[k] = v.item() / len(loader)
            print(
                "====> Epoch: {} Average {} loss: {:.4f}".format(
                    epoch, k, epoch_metrics[k]
                )
            )

    if get_z:
        return epoch_metrics, torch.cat(z, dim=0).cpu()
    else:
        return epoch_metrics, 0


def test_epoch(config, model, loader, device="cuda", epoch=0):
    print("Running test epoch")
    # loader.dataset.data["avg_speed_3d_rand"] = loader.dataset[:]["avg_speed_3d"][
    #     torch.randperm(
    #         len(loader.dataset), generator=torch.Generator().manual_seed(100)
    #     )
    # ]

    model.eval()
    with torch.no_grad():
        z = []
        epoch_metrics = {k: 0 for k in ["total"] + list(config["loss"].keys())}
        # gen_res = {
        #     k1: {k2: [] for k2 in ["pred", "target"]}
        #     for k1 in ["heading", "avg_speed_3d"]
        # }

        if "mcmi" in config["loss"].keys():
            # Update mi_estimator
            data = loader.dataset[
                :: int(len(loader.dataset) / config["data"]["batch_size"])
            ]
            # data_o["var"] = torch.cat(
            #     [data[k] for k in model.conditional_keys], dim=-1
            # ).to(device)
            data_o = model.encode(
                {k: v.to(device) for k, v in data.items() if k in ["x6d", "root"]}
            )
            model.mi_estimator = MutInfoEstimator(
                x_s=data_o["mu"].detach().clone(),
                y_s=torch.cat([data[k] for k in model.conditional_keys], dim=-1).to(
                    device
                ),
                bandwidth=config["disentangle"]["bandwidth"],
                var_mode=config["disentangle"]["var_mode"],
                model_var=(
                    data_o["L"].detach().clone() if "L" in data_o.keys() else None
                ),
                device=device,
            )

        for batch_idx, data in enumerate(loader):
            data = {k: v.to(device) for k, v in data.items()}
            data["ids"] = torch.zeros_like(data["ids"])
            data_o = predict_batch(model, data, model.disentangle_keys)

            z += [data_o["mu"].clone().detach()]
            batch_metrics = get_batch_loss(
                model,
                data,
                data_o,
                config["loss"],
                config["disentangle"],
            )

            # for key in gen_res.keys():
            #     key_pred, key_target = generative_restrictiveness(
            #         model, data_o["mu"], data, key, loader.dataset.kinematic_tree
            #     )
            #     if "speed" in key:
            #         norm_params = {
            #             k: v.to(key_pred.device)
            #             for k, v in loader.dataset.norm_params[key].items()
            #         }
            #         if "mean" in norm_params.keys():
            #             key_pred -= norm_params["mean"]
            #             key_pred /= norm_params["std"]
            #         elif "min" in norm_params.keys():
            #             key_pred -= norm_params["min"]
            #             key_pred = 2 * key_pred / norm_params["max"] - 1

            #     gen_res[key]["pred"] += [key_pred.detach().cpu()]
            #     gen_res[key]["target"] += [key_target.detach().cpu()]

            epoch_metrics = {
                k: v + batch_metrics[k].detach() for k, v in epoch_metrics.items()
            }

    for k, v in epoch_metrics.items():
        epoch_metrics[k] = v.item() / len(loader)
        print(
            "====> Epoch: {} Average {} loss: {:.4f}".format(epoch, k, epoch_metrics[k])
        )

    # for key in gen_res.keys():
    #     epoch_metrics["r2_gen_restrict_{}".format(key)] = r2_score(
    #         torch.cat(gen_res[key]["target"], dim=0),
    #         torch.cat(gen_res[key]["pred"], dim=0),
    #     )

    return epoch_metrics, torch.cat(z, dim=0).cpu()


def train_epoch(config, model, loader, optimizer, scheduler, device="cuda", epoch=0):
    epoch_metrics = train_test_epoch(
        config=config,
        model=model,
        loader=loader,
        optimizer=optimizer,
        scheduler=scheduler,
        device=device,
        epoch=epoch,
        mode="train",
        get_z=epoch % 5 == 0,
    )

    return epoch_metrics

@profile
def train(config, model, train_loader, test_loader, run=None):
    torch.set_float32_matmul_precision("medium")
    torch.autograd.set_detect_anomaly(True)
    torch.backends.cudnn.benchmark = True
    # config = balance_disentangle(config, train_loader.dataset)

    optimizer, scheduler = get_optimizer_and_lr_scheduler(
        model,
        config["train"],
        config["model"]["load_model"],
        config["model"]["start_epoch"],
    )

    if "prior" in config["loss"].keys():
        beta_scheduler = get_beta_schedule(
            config["loss"]["prior"],
            config["train"]["beta_anneal"],
        )
    else:
        beta_scheduler = None

    for epoch in tqdm.trange(
        config["model"]["start_epoch"] + 1, config["train"]["num_epochs"] + 1
    ):
        if beta_scheduler is not None:
            config["loss"]["prior"] = beta_scheduler.get(epoch)
            print("Beta schedule: {:.3f}".format(config["loss"]["prior"]))

        starttime = time.time()
        train_metrics, z_train = train_epoch(
            config=config,
            model=model,
            loader=train_loader,
            optimizer=optimizer,
            scheduler=scheduler,
            device="cuda",
            epoch=epoch,
        )
        metrics = {"{}_train".format(k): v for k, v in train_metrics.items()}

        if "grad_reversal" in model.disentangle.keys():
            for key in model.disentangle["grad_reversal"].keys():
                model.disentangle["grad_reversal"][key].reset_parameters()

        if "moving_avg_lsq" in model.disentangle.keys():
            for key in model.disentangle["moving_avg_lsq"].keys():
                metrics["lambda_mals_{}".format(key)] = (
                    model.disentangle["moving_avg_lsq"][key].lam1.detach().cpu().numpy()
                )

        if "qda" in model.disentangle.keys():
            for key in model.disentangle["qda"].keys():
                metrics["lambda_qda_{}".format(key)] = (
                    model.disentangle["qda"][key].lama.detach().cpu().numpy()
                )

        metrics["time"] = time.time() - starttime

        if epoch % 5 == 0:
            if epoch >= 50:
                # rand_state = torch.random.get_rng_state()
                # print(rand_state)
                # torch.manual_seed(100)
                test_metrics, z_test = test_epoch(
                    config=config,
                    model=model,
                    loader=test_loader,
                    device="cuda",
                    epoch=epoch,
                )
                metrics.update(
                    {"{}_test".format(k): v for k, v in test_metrics.items()}
                )

                for key in ["avg_speed_3d", "heading"]:
                    y_true = test_loader.dataset[:][key].detach().cpu().numpy()
                    r2_lin = linear_rand_cv(
                        z_test,
                        y_true,
                        int(np.ceil(model.window / config["data"]["stride"])),
                        5,
                    )
                    r2_mlp = mlp_rand_cv(
                        z_test,
                        y_true,
                        int(np.ceil(model.window / config["data"]["stride"])),
                        5,
                    )
                    metrics["r2_{}_lin_mean".format(key)] = np.mean(r2_lin)
                    metrics["r2_{}_lin_std".format(key)] = np.std(r2_lin)
                    metrics["r2_{}_mlp_mean".format(key)] = np.mean(r2_mlp)
                    metrics["r2_{}_mlp_std".format(key)] = np.std(r2_mlp)

                z_scaled = StandardScaler().fit_transform(z_train)
                y_true = (
                    train_loader.dataset[:]["ids"].detach().cpu().numpy().astype(np.int)
                )
                acc_log = log_class_rand_cv(
                    z_scaled,
                    y_true,
                    int(np.ceil(model.window / config["data"]["stride"])),
                    5,
                )
                acc_qda = qda_rand_cv(
                    z_scaled,
                    y_true,
                    int(np.ceil(model.window / config["data"]["stride"])),
                    5,
                )
                metrics["acc_ids_log_mean"] = np.mean(acc_log)
                metrics["acc_ids_log_std"] = np.std(acc_log)
                metrics["acc_ids_qda_mean"] = np.mean(acc_qda)
                metrics["acc_ids_qda_std"] = np.std(acc_qda)

                k_pred_e = cluster.gmm(
                    latents=z_test,
                    n_components=50,
                    label="".format(epoch),
                    covariance_type="diag" if config["model"]["diag"] else "full",
                    path=None,
                )[0]

                walking_inds = np.in1d(
                    test_loader.dataset.gmm_pred["midfwd_test"],
                    test_loader.dataset.walking_clusters["midfwd_test"],
                )
                metrics["entropy_midfwd_test"] = shannon_entropy(k_pred_e[walking_inds])

                for cluster_key in test_loader.dataset.gmm_pred.keys():
                    mapped = hungarian_match(
                        k_pred_e, test_loader.dataset.gmm_pred[cluster_key]
                    )
                    metrics["mof_gmm_{}".format(cluster_key)] = (
                        (test_loader.dataset.gmm_pred[cluster_key] == mapped)
                    ).sum() / len(k_pred_e)

            # metrics.update({"{}_test".format(k):v for k,v in test_loss.items()})
            # run = wandb.Api().run("joshuahwu/wandb_test/{}".format(wandb_run.))
            # wandb_run.run.history().to_csv("metrics.csv")

            print("Saving model to folder: {}".format(config["out_path"]))
            torch.save(
                {k: v.cpu() for k, v in model.state_dict().items()},
                "{}/weights/epoch_{}.pth".format(config["out_path"], epoch),
            )

            if epoch % 20 == 0:
                torch.save(
                    {"optimizer": optimizer.state_dict(), "lr_scheduler": scheduler},
                    "{}/checkpoints/epoch_{}.pth".format(config["out_path"], epoch),
                )

        wandb.log(metrics, epoch)

    return model


import numpy as np
from pandas import crosstab
from scipy.optimize import linear_sum_assignment
import numpy.typing as npt


def hungarian_match(x1: npt.ArrayLike, x2: npt.ArrayLike):
    """Matches the categorical values between two sequences using the Hungarian matching algorithm.

    Parameters
    ----------
    x1 : npt.ArrayLike
        Sequence of categorical values.
    x2 : npt.ArrayLike
        Sequence of categorical values.

    Returns
    -------
    mapped_x
        Returns x1 sequence using the matched categorical labels of x2.
    """

    cost = np.array(crosstab(x1, x2))
    row_ind, col_ind = linear_sum_assignment(cost, maximize=True)
    row_k = np.unique(x1)[row_ind]
    col_v = np.unique(x2)[col_ind]
    idx = np.searchsorted(row_k, x1)
    idx[idx == len(row_k)] = 0
    mask = row_k[idx] == x1
    mapped_x = np.where(mask, col_v[idx], x1)
    return mapped_x
