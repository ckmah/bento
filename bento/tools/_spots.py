import pickle
import warnings

import numpy as np
import pandas as pd
import scipy.stats as stats
import statsmodels.formula.api as sfm
import torch
import torch.nn as nn
import torch.nn.functional as F
from joblib import Parallel, delayed
from sklearn.preprocessing import OneHotEncoder
from skorch import NeuralNetClassifier
from skorch.callbacks import Checkpoint
from statsmodels.stats.multitest import multipletests
from statsmodels.tools.sm_exceptions import (ConvergenceWarning,
                                             PerfectSeparationError)
from torchvision import datasets, transforms
from tqdm.auto import tqdm

import bento

warnings.simplefilter("ignore", ConvergenceWarning)


PATTERN_NAMES = [
    "cell_edge",
            "foci",
    "nuclear_edge",
            "perinuclear",
            "protrusions",
            "random",
        ]

def detect_spots(cell_patterns, imagedir, device="auto", model="pattern", copy=False):
    """
    Detect and label localization patterns.
    TODO change cell_patterns to be iterable compatible with skorch.predict_proba

    Parameters
    ----------
    cell_patterns : [type]
        [description]
    imagedir : str
        Folder for rasterized images.
    Returns
    -------
    [type]
        [description]
    """
    adata = cell_patterns.copy() if copy else cell_patterns

    # Default to gpu if possible. Otherwise respect specified parameter
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model_dir = "/".join(bento.__file__.split("/")[:-1]) + "/models/spots/" + model

    model_params = pickle.load(open(f"{model_dir}/params.p", "rb"))

    # Load model
    modules = dict(pattern=SpotsModule, five_pattern=FiveSpotsModule)
    module = modules[model](**model_params)

    net = NeuralNetClassifier(module=module, device=device)
    net.initialize()
    net.load_params(checkpoint=Checkpoint(dirname=f"{model_dir}"))

    dataset = datasets.ImageFolder(
        imagedir,
        transform=transforms.Compose([transforms.Grayscale(), transforms.ToTensor()]),
    )

    # 2d array, sample by n_classes
    pred_prob = net.predict_proba(dataset)

    if isinstance(module, FiveSpotsModule):
        label_names = [
            "cell_edge",
            "nuclear_edge",
            "perinuclear",
            "protrusions",
            "random",
        ]
    else:
        label_names = PATTERN_NAMES

    encoder = OneHotEncoder(handle_unknown="ignore").fit(
        np.array(label_names).reshape(-1, 1)
    )

    # Cell gene names
    sample_names = [
        str(path).split("/")[-1].split(".")[0].split("_") for path, _ in dataset.imgs
    ]
    spots_pred_long = pd.DataFrame(sample_names, columns=["cell", "gene"])

    # TODO use spares matrices to avoid slow/big df pivots
    # https://stackoverflow.com/questions/55404617/faster-alternatives-to-pandas-pivot-table
    # Build "pattern" genexcell layer, where values are pattern labels
    spots_pred_long["label"] = encoder.inverse_transform(pred_prob >= 0.5)

    pattern_labels = (
        spots_pred_long.pivot(index="cell", columns="gene", values="label")
        .fillna("none")
        .reindex(index=adata.obs_names, columns=adata.var_names, fill_value="none")
    )

    adata.layers[model] = pattern_labels

    # Annotate points with pattern labels
    plabels_long = pattern_labels.reset_index().melt(id_vars="cell")
    plabels_long = plabels_long.rename({"value": model}, axis=1)

    # Overwrite existing values
    if model in adata.uns["points"].columns:
        adata.uns["points"].drop([model], axis=1, inplace=True)

    # Annotate points
    adata.uns["points"] = adata.uns["points"].merge(
        plabels_long, how="left", on=["cell", "gene"]
    )

    # Save pattern values as categorical to save memory
    adata.uns["points"][model] = adata.uns["points"][model].astype("category")

    # Save to adata.var
    distr_to_var(adata, model)

    return adata if copy else None


def distr_to_var(cell_patterns, layer, copy=False):
    """Computes frequencies of input layer values across cells and across genes.
    Assumes layer values are categorical.

    Parameters
    ----------
    cell_patterns : [type]
        [description]
    layer : [type]
        [description]
    copy : bool, optional
        [description], by default False

    Returns
    -------
    [type]
        [description]
    """
    adata = cell_patterns.copy() if copy else cell_patterns

    # Save frequencies across genes to adata.var
    gene_summary = (
        adata.to_df(layer).apply(lambda g: g.value_counts()).fillna(0)
    ).T
    adata.var[gene_summary.columns] = gene_summary

    # Save frequencies across cells to adata.obs
    cell_summary = (
        cell_patterns.to_df(layer).apply(lambda row: row.value_counts(), axis=1).fillna(0)
    )
    adata.obs[cell_summary.columns] = cell_summary

    return adata if copy else None


def get_conv_dim(in_size, padding, dilation, kernel_size, stride):
    outsize = 1 + (in_size + 2 * padding - dilation * (kernel_size - 1) - 1) / stride
    return int(outsize)


class DataFlatten:
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        X = X.reshape(X.shape[0], -1)
        return X


class DataReshape:
    def fit(self, X, y=None):
        return self

    def transform(self, X):
        X = X.reshape(X.shape[0], 1, 64, 64)
        return X


class SpotsModule(nn.Module):
    def __init__(
        self,
        n_conv_layers,
        in_dim,
        out_channels,
        kernel_size,
        f_units_l0,
        f_units_l1,
    ) -> None:
        super().__init__()
        conv_layers = []

        in_channels = 1
        in_dim = in_dim

        # Stack (convolutions + batchnorm + activation) + maxpool
        for i in range(n_conv_layers):
            conv_layers.append(
                nn.Conv2d(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    kernel_size=kernel_size,
                )
            )
            conv_layers.append(nn.BatchNorm2d(out_channels))
            conv_layers.append(nn.ReLU())

            # Compute convolved output dimensions
            in_dim = get_conv_dim(
                in_dim, padding=0, dilation=1, kernel_size=kernel_size, stride=1
            )

            in_channels = out_channels
            out_channels *= 2

        out_channels = int(out_channels / 2)

        conv_layers.append(nn.MaxPool2d(2, 2))
        in_dim = int(in_dim / 2)

        # We optimize the number of layers, hidden units and dropout ratio in each layer.
        fc_layers = [nn.Flatten()]

        # Compute flatten size
        in_features = out_channels * in_dim * in_dim
        for i in [f_units_l0, f_units_l1]:
            out_features = i
            fc_layers.append(nn.Linear(in_features, out_features))
            fc_layers.append(nn.BatchNorm1d(out_features))
            fc_layers.append(nn.ReLU())

            in_features = out_features

        fc_layers.append(nn.Linear(in_features, 6))

        self.model = nn.Sequential(*[*conv_layers, *fc_layers])

    def forward(self, x):
        x = self.model(x)

        x = F.softmax(x, dim=-1)

        return x


class FiveSpotsModule(nn.Module):
    def __init__(
        self,
        n_conv_layers,
        in_dim,
        out_channels,
        kernel_size,
        f_units_l0,
        f_units_l1,
    ) -> None:
        super().__init__()
        conv_layers = []

        in_channels = 1
        in_dim = in_dim

        # Stack (convolutions + batchnorm + activation) + maxpool
        for i in range(n_conv_layers):
            conv_layers.append(
                nn.Conv2d(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    kernel_size=kernel_size,
                )
            )
            conv_layers.append(nn.BatchNorm2d(out_channels))
            conv_layers.append(nn.ReLU())

            # Compute convolved output dimensions
            in_dim = get_conv_dim(
                in_dim, padding=0, dilation=1, kernel_size=kernel_size, stride=1
            )

            in_channels = out_channels
            out_channels *= 2

        out_channels = int(out_channels / 2)

        conv_layers.append(nn.MaxPool2d(2, 2))
        in_dim = int(in_dim / 2)

        # We optimize the number of layers, hidden units and dropout ratio in each layer.
        fc_layers = [nn.Flatten()]

        # Compute flatten size
        in_features = out_channels * in_dim * in_dim
        for i in [f_units_l0, f_units_l1]:
            out_features = i
            fc_layers.append(nn.Linear(in_features, out_features))
            fc_layers.append(nn.BatchNorm1d(out_features))
            fc_layers.append(nn.ReLU())

            in_features = out_features

        fc_layers.append(nn.Linear(in_features, 5))

        self.model = nn.Sequential(*[*conv_layers, *fc_layers])

    def forward(self, x):
        x = self.model(x)

        x = F.softmax(x, dim=-1)

        return x


def spots_diff(cell_patterns, groupby=None, continuous=None, n_cores=1, copy=False):
    """Gene-wise test for differential localization across phenotype of interest.

    One of `groupby` or `continuous` must be specified, but not both.

    Parameters
    ----------
    cell_patterns : AnnData
        Anndata formatted spatial cell_patterns.
    groupby : str
        Variable grouping cells for differential analysis. Must be in cell_patterns.obs_names.
    continuous : str, pd.Series
    n_cores : int, optional
        cores used for multiprocessing, by default 1
    copy : bool, optional
        Return view of AnnData if False, return copy if True. By default False.
    """
    adata = cell_patterns.copy() if copy else cell_patterns

    # Get group/continuous phenotype
    phenotype = None
    if groupby and not continuous:
        phenotype = groupby
    elif continuous and not groupby:
        phenotype = continuous
    else:
        print(
            'Either "groupby" or "continuous" parameters need to be specified, not both.'
        )

    # Test genes in parallel
    diff_output = Parallel(n_jobs=n_cores)(
        delayed(_test_gene)(
            gene_name,
            adata.layers['pattern'][:, gene_name],
            adata.obs[phenotype],
            continuous
        )
        for gene_name in tqdm(adata.var_names.tolist())
    )

    # Format pattern column
    diff_output = pd.concat(diff_output)

    # FDR correction
    results_adj = []
    for _, df in results.groupby("pattern"):
        df["padj"] = multipletests(df["pvalue"], method="hs")[1]
        results_adj.append(df)

    results_adj = pd.concat(results_adj)
    results_adj = results_adj.dropna()

    # -log10pvalue, padj
    results_adj["-log10p"] = - \
        np.log10(results_adj["pvalue"].astype(np.float32))
    results_adj["-log10padj"] = - \
        np.log10(results_adj["padj"].astype(np.float32))

    # Sort results
    results_adj = results_adj.sort_values("pvalue")

    # Save back to AnnData
    adata.uns["sample_data"][f"dl_{phenotype}"] = results_adj

    return adata if copy else None


def _test_gene(gene, cell_patterns, phenotype, continuous):
    """Perform pairwise comparison between groupby and every class.

    Parameters
    ----------
    cell_patterns : DataFrame
        Phenotype and localization pattern labels across cells for a single gene.
    groupby : str
        Variable grouping cells for differential analysis. Should be present in cell_patterns.columns.

    Returns
    -------
    DataFrame
        Differential localization test results. [# of patterns, ]
    """
    results = []

    # Series denoting pattern frequencies
    freqs = pd.Series(cell_patterns, index=phenotype.index).value_counts()

    # Continuous test: spearman correlation between phenotype and pattern frequency
    if continuous:
        for c in PATTERN_NAMES:
            corr, p = stats.spearmanr(cell_patterns[phenotype], cell_patterns[c])
            results.append(
                pd.Series(
                    [corr, p, gene, continuous],
                    index=["r", "pvalue", "gene", "phenotype"],
                )
                .to_frame()
                .T
            )
        results = pd.concat(results)
        results["pattern"] = classes
    else:
        group_dummies = pd.get_dummies(cell_patterns[phenotype])
        group_names = group_dummies.columns.tolist()
        group_data = pd.concat([cell_patterns, group_dummies], axis=1)

        for g in group_names:
            for c in classes:
                try:
                    res = sfm.logit(
                        formula=f"{g} ~ {c}", cell_patterns=group_data).fit(disp=0)
                    r = res.get_margeff().summary_frame()
                    r["gene"] = gene
                    r["phenotype"] = g
                    r["pattern"] = c
                    r.columns = [
                        "dy/dx",
                        "std_err",
                        "z",
                        "pvalue",
                        "ci_low",
                        "ci_high",
                        "gene",
                        "phenotype",
                        "pattern",
                    ]
                    r = r.reset_index(drop=True)
                    results.append(r)
                except (np.linalg.LinAlgError, PerfectSeparationError):
                    continue
        results = pd.concat(results)

    return results
