from typing import Optional

import decoupler as dc

import numpy as np
import pandas as pd
import dask.dataframe as dd
import pkg_resources
from scipy import sparse
from spatialdata._core.spatialdata import SpatialData
from spatialdata.models import PointsModel

from ..geometry import get_points


def fe_fazal2019(sdata: SpatialData, **kwargs):
    """Compute enrichment scores from subcellular compartment gene sets from Fazal et al. 2019 (APEX-seq).
    See `bento.tl.fe` docs for parameter details.

    Parameters
    ----------
    data : SpatialData
        Spatial formatted SpatialData object.

    Returns
    -------
    DataFrame
        Enrichment scores for each gene set.
    """

    gene_sets = load_gene_sets("fazal2019")
    fe(sdata, net=gene_sets, **kwargs)


def fe_xia2019(sdata: SpatialData, **kwargs):
    """Compute enrichment scores from subcellular compartment gene sets from Xia et al. 2019 (MERFISH 10k U2-OS).
    See `bento.tl.fe` docs for parameters details.

    Parameters
    ----------
    data : SpatialData
        Spatial formatted SpatialData object.

    Returns
    -------
    DataFrame
        Enrichment scores for each gene set.
    """

    gene_sets = load_gene_sets("xia2019")
    fe(sdata, gene_sets, **kwargs)


def fe(
    sdata: SpatialData,
    net: pd.DataFrame,
    source: Optional[str] = "source",
    target: Optional[str] = "target",
    weight: Optional[str] = "weight",
    batch_size: int = 10000,
    min_n: int = 0,
):
    """
    Perform functional enrichment on point embeddings. Wrapper for decoupler wsum function.

    Parameters
    ----------
    sdata : SpatialData
        Spatial formatted SpatialData object.
    net : DataFrame
        DataFrame with columns "source", "target", and "weight". See decoupler API for more details.
    source : str, optional
        Column name for source nodes in `net`. Default "source".
    target : str, optional
        Column name for target nodes in `net`. Default "target".
    weight : str, optional
        Column name for weights in `net`. Default "weight".
    batch_size : int
        Number of points to process in each batch. Default 10000.
    min_n : int
        Minimum number of targets per source. If less, sources are removed.

    Returns
    -------
    sdata : SpatialData
        .points["cell_raster"]["flux_fe"] : DataFrame
            Enrichment scores for each gene set.
    """
    # Make sure embedding is run first
    if "flux_genes" in sdata.table.uns:
        flux_genes = set(sdata.table.uns["flux_genes"])
        cell_raster_columns = set(sdata.points["cell_raster"].columns)
        if len(flux_genes.intersection(cell_raster_columns)) != len(flux_genes):
            print("Recompute bento.tl.flux first.")
            return
    else:
        print("Run bento.tl.flux first.")
        return
    
    flux_genes = sdata.table.uns["flux_genes"]
    cell_raster_points = get_points(sdata, points_key="cell_raster", astype="dask")[flux_genes]
    cell_raster_matrix = np.mat(cell_raster_points.values.compute())
    mat = sparse.csr_matrix(cell_raster_matrix)  # sparse matrix in csr format

    samples = sdata.points["cell_raster"].index.astype(str)
    features = sdata.table.uns["flux_genes"]

    enrichment = dc.run_wsum(
        mat=[mat, samples, features],
        net=net,
        source=source,
        target=target,
        weight=weight,
        batch_size=batch_size,
        min_n=min_n,
        verbose=True,
    )

    scores = enrichment[1].reindex(index=samples)
    cell_raster_points = sdata.points["cell_raster"].compute()
    for col in scores.columns:
        score_key = f"flux_{col}"
        cell_raster_points[score_key] = scores[col].values

    transform = sdata.points["cell_raster"].attrs
    sdata.points["cell_raster"] = PointsModel.parse(cell_raster_points, coordinates={'x': 'x', 'y': 'y'})
    sdata.points["cell_raster"].attrs = transform

    _fe_stats(sdata, net, source=source, target=target)


def _fe_stats(
    sdata: SpatialData,
    net: pd.DataFrame,
    source: str = "source",
    target: str = "target",
):
    # rows = cells, columns = pathways, values = count of genes in pathway
    expr_binary = sdata.table.to_df() >= 5
    # {cell : present gene list}
    expr_genes = expr_binary.apply(lambda row: sdata.table.var_names[row], axis=1)

    # Count number of genes present in each pathway
    net_ngenes = net.groupby(source).size().to_frame().T.rename(index={0: "n_genes"})

    sources = []
    # common_genes = {}  # list of [cells: gene set overlaps]
    common_ngenes = []  # list of [cells: overlap sizes]
    for source, group in net.groupby(source):
        sources.append(source)
        common = expr_genes.apply(lambda genes: set(genes).intersection(group[target]))
        common_ngenes.append(common.apply(len))

    fe_stats = pd.concat(common_ngenes, axis=1)
    fe_stats.columns = sources

    sdata.table.uns["fe_stats"] = fe_stats
    sdata.table.uns["fe_ngenes"] = net_ngenes


gene_sets = dict(
    fazal2019="fazal2019.csv",
    xia2019="xia2019.csv",
)


def load_gene_sets(name):
    """Load a gene set from bento.

    Parameters
    ----------
    name : str
        Name of gene set to load.

    Returns
    -------
    DataFrame
        Gene set.
    """
    global pkg_resources
    if pkg_resources is None:
        import pkg_resources

    fname = gene_sets[name]
    stream = pkg_resources.resource_stream(__name__, f"gene_sets/{fname}")
    gs = pd.read_csv(stream)

    return gs