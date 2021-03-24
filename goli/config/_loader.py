from typing import List, Dict, Union, Any

import omegaconf
from copy import deepcopy
import torch
from loguru import logger

from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning import Trainer

from goli.trainer.metrics import MetricWrapper
from goli.nn import FullDGLNetwork, FullDGLSiameseNetwork
from goli.data.datamodule import DGLFromSmilesDataModule
from goli.trainer.predictor import PredictorModule

# from goli.trainer.model_summary import BestEpochFromSummary
from pytorch_lightning.loggers.tensorboard import TensorBoardLogger


def load_datamodule(
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
):
    datamodule = DGLFromSmilesDataModule(**config["datamodule"])

    return datamodule


def load_metrics(config: Union[omegaconf.DictConfig, Dict[str, Any]]):

    metrics = {}
    cfg_metrics = deepcopy(config["metrics"])

    for this_metric in cfg_metrics:
        name = this_metric.pop("name")
        metrics[name] = MetricWrapper(**this_metric)

    return metrics


def load_architecture(
    config: Union[omegaconf.DictConfig, Dict[str, Any]],
    in_dim_nodes: int,
    in_dim_edges: int,
):

    if isinstance(config, dict):
        config = omegaconf.OmegaConf.create(config)
    cfg_arch = config["architecture"]

    kwargs = {}

    # Select the architecture
    model_type = cfg_arch["model_type"].lower()
    if model_type == "fulldglnetwork":
        model_class = FullDGLNetwork
    elif model_type == "fulldglsiamesenetwork":
        model_class = FullDGLSiameseNetwork
        kwargs["dist_method"] = cfg_arch["dist_method"]
    else:
        raise ValueError(f"Unsupported model_type=`{model_type}`")

    # Prepare the various kwargs
    pre_nn_kwargs = dict(cfg_arch["pre_nn"]) if cfg_arch["pre_nn"] is not None else None
    gnn_kwargs = dict(cfg_arch["gnn"])
    post_nn_kwargs = dict(cfg_arch["post_nn"]) if cfg_arch["post_nn"] is not None else None

    # Set the input dimensions
    if pre_nn_kwargs is not None:
        pre_nn_kwargs = dict(pre_nn_kwargs)
        pre_nn_kwargs.setdefault("in_dim", in_dim_nodes)
    else:
        gnn_kwargs.setdefault("in_dim", in_dim_nodes)

    gnn_kwargs.setdefault("in_dim_edges", in_dim_edges)

    # Set the parameters for the full network
    model_kwargs = dict(
        gnn_kwargs=gnn_kwargs,
        pre_nn_kwargs=pre_nn_kwargs,
        post_nn_kwargs=post_nn_kwargs,
    )

    return model_class, model_kwargs


def load_predictor(config, model_class, model_kwargs, metrics):
    # Defining the predictor

    cfg_pred = dict(deepcopy(config["predictor"]))
    predictor = PredictorModule(
        model_class=model_class,
        model_kwargs=model_kwargs,
        metrics=metrics,
        **cfg_pred,
    )

    return predictor


def load_trainer(config, metrics):
    cfg_trainer = deepcopy(config["trainer"])

    # Set the number of gpus to 0 if no GPU is available
    gpus = cfg_trainer["trainer"].pop("gpus", 0)
    num_gpus = 0
    if isinstance(gpus, int):
        num_gpus = gpus
    elif isinstance(gpus, (list, tuple)):
        num_gpus = len(gpus)
    if (num_gpus > 0) and (not torch.cuda.is_available()):
        logger.warning(
            f"Number of GPUs selected is `{num_gpus}`, but will be ignored since no GPU are available on this device"
        )
        gpus = 0

    early_stopping = EarlyStopping(**cfg_trainer["early_stopping"])
    checkpoint_callback = ModelCheckpoint(**cfg_trainer["model_checkpoint"])

    tb_logger = TensorBoardLogger(**cfg_trainer["logger"], default_hp_metric=False)

    trainer = Trainer(
        logger=tb_logger,
        callbacks=[checkpoint_callback, early_stopping],
        terminate_on_nan=True,
        **cfg_trainer["trainer"],
        gpus=gpus,
    )

    return trainer