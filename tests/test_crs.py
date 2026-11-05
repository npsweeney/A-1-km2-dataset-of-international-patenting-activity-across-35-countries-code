import unittest
from geographic_uncertainty.retrieve_and_rasterise import mollweide, grid_cell, cell_centroid


class CoordinateAlignment(unittest.TestCase):
    def test_projection_matches_ghsl_crs(self):
        try:
            from rasterio.warp import transform
        except ImportError:
            self.skipTest("Install the raster extra for projection checks")
        points = [(-2.231, 53.464), (151.2, -33.86), (-74, 40.7),
                  (179.99, 0), (-179.99, 0), (0, 85), (0, -85)]
        xs, ys = transform("EPSG:4326", "ESRI:54009",
                           [p[0] for p in points], [p[1] for p in points])
        for point, x, y in zip(points, xs, ys):
            with self.subTest(point=point):
                actual_x, actual_y = mollweide(*point)
                self.assertAlmostEqual(actual_x, x, places=5)
                self.assertAlmostEqual(actual_y, y, places=5)
                row, column = grid_cell(*point)
                self.assertEqual(row, int((9000000 - y) // 100))
                self.assertEqual(column, int((x + 18041000) // 100))
                self.assertEqual(grid_cell(*cell_centroid(row, column)), (row, column))

    def test_projected_boundaries_are_rejected(self):
        import json
        import tempfile
        from pathlib import Path
        from geocoding.validation.spatial_checks import TL3Index
        polygon = {"type": "FeatureCollection", "crs": {"properties": {"name": "EPSG:3857"}},
                   "features": [{"properties": {"code": "x"}, "geometry": {
                       "type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.json"
            path.write_text(json.dumps(polygon))
            with self.assertRaisesRegex(ValueError, "WGS84"):
                TL3Index.from_geojson(path, "code")
            polygon.pop("crs")
            polygon["features"][0]["geometry"]["coordinates"][0][1] = [1000, 0]
            path.write_text(json.dumps(polygon))
            with self.assertRaisesRegex(ValueError, "WGS84"):
                TL3Index.from_geojson(path, "code")
