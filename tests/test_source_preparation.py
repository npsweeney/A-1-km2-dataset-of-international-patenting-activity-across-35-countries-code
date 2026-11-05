import csv
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from ingest.patent_metadata.import_patstat import build, FIELDS
from geocoding.validation.prepare_boundaries import prepare
from geographic_uncertainty.retrieve_and_rasterise import _service
from weighting.register_ghsl import build_registry

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_example', ROOT / 'tests/fixtures/patent_records.py')
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


class SourcePreparation(unittest.TestCase):
    def test_ghsl_registry_accepts_current_scopes_and_rejects_pilot_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'assets.json'
            for scope in ('full_1975_2030', 'through_2025', 'pilot_2020_only', 'historical_1975_2020'):
                source.write_text(json.dumps({'registry_scope': scope, 'assets': []}))
                message = 'declared epoch scope' if scope in ('full_1975_2030', 'through_2025') else 'registry scope is invalid'
                with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, message):
                    build_registry(source_manifest_path=source,
                                   grid_contract_path=ROOT / 'config/ghsl_grid.json',
                                   output_path=root / 'registry.json')

    def test_patstat_zip_parts_filter_to_regpat_and_index_application_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example.create(root / 'inputs')
            with sqlite3.connect(root / 'inputs/patstat.sqlite') as db:
                rows = db.execute('SELECT ' + ','.join(FIELDS) + ' FROM tls201_appln').fetchall()
            rows.append((1, 1, 1990, '1990-01-01'))
            paths = []
            for number, part in enumerate((rows[:2], rows[2:])):
                source = root / f'part{number}.csv'
                with source.open('w', newline='') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(FIELDS)
                    writer.writerows(part)
                paths.append(source.with_suffix('.zip'))
                with zipfile.ZipFile(paths[-1], 'w') as bundle:
                    bundle.write(source, source.name)
            result = build(paths, root / 'inputs/regpat', root / 'imported.sqlite')
            self.assertEqual(result['imported_applications'], len(rows) - 1)
            with sqlite3.connect(root / 'imported.sqlite') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tls201_appln WHERE appln_id=1').fetchone()[0], 0)
                self.assertIn('PRIMARY KEY', db.execute('EXPLAIN QUERY PLAN SELECT * FROM tls201_appln WHERE appln_id=1').fetchone()[-1])
                expected = sorted((int(aid), int(fid), year, date) for aid, fid, year, date in rows[:-1])
                self.assertEqual(db.execute('SELECT ' + ','.join(FIELDS) + ' FROM tls201_appln ORDER BY appln_id').fetchall(), expected)

    def test_boundary_conversion_uses_supplied_property_and_county_lookup(self):
        import shapefile
        from rasterio.crs import CRS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'regions.shp'
            with shapefile.Writer(str(source)) as writer:
                writer.field('region', 'C')
                writer.poly([[[-1, 0], [-1, 1], [0, 1], [0, 0], [-1, 0]]])
                writer.record('US001')
            source.with_suffix('.prj').write_text(CRS.from_epsg(4326).to_wkt())
            lookup = root / 'REGPAT_REGIONS_US.txt'
            lookup.write_text('reg_code|reg_tl3\nUS01001|US001\n')
            boundary, manifest = prepare(source, root / 'converted', vintage=2024, property='region', us_lookup=lookup)
            self.assertEqual(json.loads(boundary.read_text())['features'][0]['properties'], {'region': 'US001'})
            self.assertEqual(json.loads(manifest.read_text())['boundary_codes'], ['US001'])
            self.assertEqual(json.loads((manifest.parent / 'us_counties.json').read_text())['counties']['01001']['expected_tl3'], ['US001'])
            source.with_suffix('.prj').write_text(CRS.from_epsg(3857).to_wkt())
            with self.assertRaisesRegex(ValueError, 'EPSG:4326'):
                prepare(source, root / 'wrong-crs', vintage=2024, property='region')

    def test_service_template_is_complete_and_errors_name_missing_fields(self):
        service = json.loads((ROOT / 'config/geographic_service.template.json').read_text())
        self.assertEqual(_service(service)[1], 30003000)
        service['max_queue'] = None
        with self.assertRaisesRegex(ValueError, 'max_queue'):
            _service(service)
