from neuroposelib.embed import Embed
import scrubvae
from neuroposelib import read
from torch.utils.data import DataLoader
import numpy as np
import scipy.linalg as spl
from base_path import RESULTS_PATH
import matplotlib.pyplot as plt
from cmocean.cm import phase
import colorcet as cc
from scrubvae.plot import scatter_cmap
import sys

analysis_key = sys.argv[1]
config = read.config(RESULTS_PATH + analysis_key + "/model_config.yaml")

dataset_label = "Train"
### Load Datasets
dataset, _, model = scrubvae.get.data_and_model(
    config,
    load_model=config["out_path"],
    epoch=sys.argv[2],
    dataset_label=dataset_label,
    data_keys=["x6d", "root", "heading"],
    shuffle=False,
    verbose=0,
)

latents = (
    scrubvae.get.latents(
        config, model, sys.argv[2], dataset, device="cuda", dataset_label=dataset_label
    )
    .cpu()
    .detach()
    .numpy()
)

heading = dataset[:]["heading"].cpu().detach().numpy()
yaw = np.arctan2(heading[:, 0], heading[:, 1])

embedder = Embed(
    embed_method="fitsne",
    perplexity=50,
    lr="auto",
)
embed_vals = embedder.embed(latents, save_self=True)
np.save(config["out_path"] + "tSNE_z_{}.npy".format(dataset_label), embed_vals)

embed_vals = np.load(config["out_path"] + "tSNE_z_{}.npy".format(dataset_label))

downsample = 10
rand_ind = np.random.permutation(np.arange(len(embed_vals)))
scatter_cmap(
    embed_vals[rand_ind, :][::downsample, :],
    yaw[rand_ind][::downsample],
    "z_yaw_{}".format(dataset_label),
    path=config["out_path"],
)


# k_pred = np.load(config["out_path"] + "vis_latents/z_gmm.npy")
# scatter_cmap(embed_vals[::downsample, :], k_pred[::downsample], "gmm", path=config["out_path"], cmap=plt.get_cmap("gist_rainbow"))

# z_null = scrubvae.eval.project_to_null(
#     z, model.disentangle["heading"].decoder.weight.detach().cpu().numpy()
# )[0]

# # embed_vals = embedder.embed(z_null, save_self=True)
# # np.save(config["out_path"] + "tSNE_znull.npy", embed_vals)

# # embed_vals = np.load(config["out_path"] + "tSNE_znull.npy")

# scatter_cmap(
#     embed_vals[::downsample, :], yaw[::downsample], "znull_yaw", path=config["out_path"]
# )
