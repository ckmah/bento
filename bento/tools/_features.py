import warnings

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from scipy.stats import zscore
from tqdm.auto import tqdm

from ..preprocessing import get_points

warnings.simplefilter(action="ignore", category=FutureWarning)

dd = None
ig = None
la = None
UMAP = None
NearestNeighbors = None


def gene_leiden(data, copy=False):

    global UMAP, NearestNeighbors

    if UMAP is None:
        from umap import UMAP

    if NearestNeighbors is None:
        from sklearn.neighbors import NearestNeighbors

    adata = data.copy() if copy else data

    coloc_sim = (
        adata.uns["coloc_sim_agg"]
        .pivot_table(index="g1", columns="g2", values="coloc_sim")
        .fillna(0)
    )
    coloc_sim = coloc_sim.dropna()

    genes = coloc_sim.index

    # Z scale features
    coloc_sim = zscore(coloc_sim, axis=0)

    nn = NearestNeighbors().fit(coloc_sim)
    connectivity = nn.kneighbors_graph(coloc_sim, n_neighbors=5).toarray()

    loc_umap = UMAP().fit_transform(connectivity)
    loc_umap = pd.DataFrame(loc_umap, index=genes)
    adata.varm["loc_umap"] = loc_umap.reindex(adata.var_names)

    return adata if copy else None


def coloc_cluster_genes(data, resolution=1, copy=False):

    global ig, la, z_score, NearestNeighbors
    if ig is None:
        import igraph as ig

    if la is None:
        import leidenalg as la

    if NearestNeighbors is None:
        from sklearn.neighbors import NearestNeighbors

    adata = data.copy() if copy else data

    coloc_sim = (
        adata.uns["coloc_sim_agg"]
        .pivot_table(index="g1", columns="g2", values="coloc_sim")
        .fillna(0)
    )

    genes = coloc_sim.index.tolist()
    coloc_sim = coloc_sim.values

    # Z scale features
    coloc_sim = zscore(coloc_sim, axis=0)

    nn = NearestNeighbors().fit(coloc_sim)
    connectivity = nn.kneighbors_graph(coloc_sim, n_neighbors=5).toarray()

    g = ig.Graph.Adjacency(connectivity)
    g.es["weight"] = connectivity[connectivity != 0]
    g.vs["label"] = genes
    partition = la.find_partition(
        g,
        la.CPMVertexPartition,
        weights=g.es["weight"],
        resolution_parameter=resolution,
    )
    gene_clusters = pd.Series(partition.membership, dtype=int, index=g.vs["label"])

    adata.var["coloc_group"] = gene_clusters.reindex(adata.var_names)
    return adata if copy else None


def coloc_sim(data, radius=3, min_count=5, n_cores=1, copy=False):
    """Calculate pairwise gene colocalization similarity using a KNN approach.

    Parameters
    ----------
    adata : AnnData
        Anndata formatted spatial data.
    outer_radius : int, optional
        Number of pixels to search for neighbors, by default 3
    Returns
    -------
    adata : AnnData
        .uns['coloc_sim']: Pairwise gene colocalization similarity within each cell.
    """

    global dd, NearestNeighbors
    if dd is None:
        import dask.dataframe as dd

    if NearestNeighbors is None:
        from sklearn.neighbors import NearestNeighbors

    adata = data.copy() if copy else data

    # Filter points and counts by min_count
    counts = adata.to_df()

    # Helper function to apply per cell
    def cell_coloc_sim(p, g_density, name):

        # Get xy coordinates
        xy = p[["x", "y"]].values

        # Get neighbors within fixed outer_radius for every point
        nn = NearestNeighbors(radius=radius).fit(xy)
        distances, point_index = nn.radius_neighbors(xy, return_distance=True)

        # Enumerate point-wise gene labels
        gene_index = p["gene"].reset_index(drop=True).cat.remove_unused_categories()

        # Convert to adjacency list of points, no double counting
        neighbor_pairs = []
        for g1, neighbors, n_dists in zip(gene_index.values, point_index, distances):
            for g2, d in zip(neighbors, n_dists):
                neighbor_pairs.append([g1, g2, d])

        # Calculate pair-wise gene similarity
        neighbor_pairs = pd.DataFrame(neighbor_pairs, columns=["g1", "g2", "p_dist"])

        # Keep minimum distance to g2 point
        neighbor_pairs = neighbor_pairs.groupby(["g1", "g2"]).agg("min").reset_index()
        neighbor_pairs.columns = ["g1", "g2", "point_dist"]

        # Map to gene index
        neighbor_pairs["g2"] = neighbor_pairs["g2"].map(gene_index)

        # Count number of points within distance of increasing radius
        r_step = 0.5
        expected_counts = [
            lambda dists: (dists <= r).sum()
            for r in np.arange(r_step, radius + r_step, r_step)
        ]
        metrics = (
            neighbor_pairs.groupby(["g1", "g2"])
            .agg({"point_dist": expected_counts})
            .reset_index()
        )

        # Colocalization metric: max of L_ij(r) for r <= radius
        g2_density = g_density.loc[metrics["g2"].tolist()].values
        metrics["coloc_sim"] = (
            (metrics["point_dist"].divide(g2_density * np.pi, axis=0))
            .pow(0.5)
            .max(axis=1)
        )
        metrics["cell"] = name

        # Ignore self colocalization
        # metrics = metrics.loc[metrics["g1"] != metrics["g2"]]

        return metrics[["cell", "g1", "g2", "coloc_sim"]]

    # Only keep genes >= min_count in each cell
    gene_densities = []
    counts.apply(lambda row: gene_densities.append(row[row >= min_count]), axis=1)
    # Calculate point density per gene per cell
    gene_densities /= adata.obs["cell_area"]
    gene_densities = gene_densities.values

    cell_metrics = Parallel(n_jobs=n_cores)(
        delayed(cell_coloc_sim)(
            get_points(adata, cells=g_density.name, genes=g_density.index.tolist()),
            g_density,
            g_density.name,
        )
        for g_density in tqdm(gene_densities)
    )

    cell_metrics = pd.concat(cell_metrics)
    cell_metrics.columns = cell_metrics.columns.get_level_values(0)

    # Save coloc similarity
    adata.uns["coloc_sim"] = cell_metrics

    # Aggregate metric across cells
    cell_metrics_agg = dd.from_pandas(cell_metrics, chunksize=1000000)
    agg = cell_metrics_agg.groupby(["g1", "g2"]).coloc_sim.sum().compute().reset_index()

    # Mean based on number of cells
    agg["coloc_sim"] /= data.shape[0]

    adata.uns["coloc_sim_agg"] = agg

    return adata if copy else None
