import unittest
import bento as bt
import spatialdata as sd


class TestFluxEnrichement(unittest.TestCase):
    def setUp(self):
        datadir = "/".join(bt.__file__.split("/")[:-1]) + "/datasets"
        self.data = sd.read_zarr(f"{datadir}/small_data.zarr")
        self.data = bt.io.format_sdata(
            sdata=self.data,
            points_key="transcripts",
            feature_key="feature_name",
            instance_key="cell_boundaries",
            shape_keys=["cell_boundaries", "nucleus_boundaries"],
        )

        bt.tl.flux(
            sdata=self.data,
            points_key="transcripts",
            instance_key="cell_boundaries",
            feature_key="feature_name",
        )
        bt.tl.fluxmap(
            sdata=self.data,
            points_key="transcripts",
            instance_key="cell_boundaries",
            n_clusters=3,
        )



