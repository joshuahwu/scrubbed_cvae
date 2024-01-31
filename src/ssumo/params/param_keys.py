PARAM_KEYS = dict(
    data=[
        "arena_size",
        "batch_size",
        "data_path",
        "direction_process",
        "filter_pose",
        "remove_speed_outliers",
        "skeleton_path",
        "stride",
    ],
    disentangle=["alpha", "balance_loss", "detach_gr", "features", "method"],
    model=[
        "activation",
        "channel",
        "diag",
        "init_dilation",
        "kernel",
        "start_epoch",
        "load_model",
        "type",
        "window",
        "z_dim",
    ],
    train=["beta_anneal", "num_epochs"],
)
