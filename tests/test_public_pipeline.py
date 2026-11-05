"""Run stages 01–08 through the public shell entry point using local synthetic data"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_patents', ROOT/'tests/fixtures/patent_records.py')
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def save(path, data):
    path.write_text(json.dumps(data))
    return path


def table(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


class PublicPipeline(unittest.TestCase):
    def test_local_pipeline_resume_validation_and_figures(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        from weighting.register_ghsl import EPOCHS, stem
        from validation.mask import prepare

        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            example.create(p/'inputs')
            calls = []

            # The service is a synthetic loopback fixture, never a production endpoint
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *unused):
                    pass

                def do_GET(self):
                    url = urlsplit(self.path)
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    calls.append(url.path)
                    if url.path == '/reverse':
                        result = {'address': {'country_code': 'gb', 'postcode': 'OTHER'}}
                    elif url.path == '/lookup':
                        result = [dict(osm_type='way', osm_id=2, address={'country_code': 'gb'},
                                       geojson={'type': 'LineString', 'coordinates': [[-2.24,53.47],[-2.239,53.471]]})]
                    elif url.path == '/search':
                        text = ' '.join(q.values()).lower()
                        postcode = q.get('postalcode', '')
                        if not postcode:
                            postcode = next((pc for pc in ('M13 9RD','M12 6AL','M13 9PL') if pc.lower() in text), 'M13 9PL')
                        road = 'Oxford Road' if 'oxford' in text else 'Stockport Road' if 'stockport' in text else ''
                        direct = road == 'Oxford Road' and '124' in text
                        address = dict(country_code='gb', postcode=postcode, city='Manchester')
                        if road:
                            address['road'] = road
                        if direct:
                            address['house_number'] = '124'
                        result = [dict(place_id=1 if direct else 2 if road else 3,
                                       osm_type='node' if direct else 'way' if road else 'relation',
                                       osm_id=1 if direct else 2 if road else 3,
                                       lat='53.47', lon='-2.24', address=address,
                                       name=road or postcode, display_name=road or postcode,
                                       type='house' if direct else 'residential' if road else 'postcode',
                                       addresstype='house' if direct else 'road' if road else 'postcode')]
                        if 'polygon_geojson' in q and not road:
                            result[0]['geojson'] = ({'type': 'Point', 'coordinates': [-2.24,53.47]}
                                if postcode == 'M13 9PL' else {'type': 'Polygon', 'coordinates': [[
                                    [-2.245,53.465],[-2.235,53.465],[-2.235,53.475],[-2.245,53.475],[-2.245,53.465]]]})
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            endpoint = f'http://127.0.0.1:{server.server_port}'
            boundaries = save(p/'boundaries.geojson', dict(type='FeatureCollection', features=[dict(
                type='Feature', properties=dict(tl3_id='UKD33', iso3='GBR'), geometry=dict(type='Polygon',
                    coordinates=[[[-2.32,53.34],[-2.14,53.34],[-2.14,53.55],[-2.32,53.55],[-2.32,53.34]]]))]))
            preflight = save(p/'preflight.json', dict(boundary_vintage=2024, boundary_codes=['UKD33']))
            service = json.loads((ROOT/'config/geographic_service.template.json').read_text())
            service.update(endpoint=endpoint, database_system_identifier='synthetic', data_revision='fixture-v1',
                           point_tl3_boundaries=dict(path=str(boundaries), code_property='tl3_id'),
                           response_cache_dir=str(p/'geographic-cache'), capacity_manifest=str(p/'capacity.json'))
            capacity = json.loads((ROOT/'config/capacity_manifest.example.json').read_text())
            capacity.update(passed=True, selected_client_concurrency=8,
                            service_identity={k: service[k] for k in ('endpoint','database_system_identifier','data_revision','version')})
            save(p/'capacity.json', capacity)
            service_path = save(p/'service.json', service)
            policy = save(p/'policy.json', json.loads((ROOT/'config/geographic_policy.template.json').read_text()))

            # Tiny constant rasters exposed through a global VRT exercise real raster reads
            constant = p/'constant.tif'
            with rasterio.open(constant, 'w', driver='GTiff', width=1, height=1, count=1,
                               dtype='uint16', crs='ESRI:54009', transform=from_origin(0,100,100,100), nodata=65535) as dst:
                dst.write(np.array([[100]], dtype='uint16'), 1)
            assets = []
            wkt = rasterio.crs.CRS.from_string('ESRI:54009').to_wkt()
            for epoch in EPOCHS:
                raster = p/(stem(epoch)+'.tif')
                raster.write_text(f'''<VRTDataset rasterXSize="360820" rasterYSize="180000">
<SRS>{wkt}</SRS><GeoTransform>-18041000,100,0,9000000,0,-100</GeoTransform>
<VRTRasterBand dataType="UInt16" band="1"><NoDataValue>65535</NoDataValue><SimpleSource>
<SourceFilename relativeToVRT="0">{constant}</SourceFilename><SourceBand>1</SourceBand>
<SrcRect xOff="0" yOff="0" xSize="1" ySize="1"/><DstRect xOff="0" yOff="0" xSize="360820" ySize="180000"/>
</SimpleSource></VRTRasterBand></VRTDataset>''')
                archive = p/(stem(epoch)+'.zip')
                with ZipFile(archive, 'w') as bundle:
                    bundle.write(raster, raster.name)
                assets.append(dict(epoch=epoch, raster_path=str(raster), archive_path=str(archive)))
            ghsl = save(p/'assets.json', dict(assets=assets))
            output = p/'run'
            env = dict(os.environ, PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
                       REGPAT_RELEASE=str(p/'inputs/regpat'), PATSTAT_DB=str(p/'inputs/patstat.sqlite'),
                       OPS_CACHE_DB=str(p/'inputs/ops.sqlite'), APPLICATION_MASTER=str(p/'inputs/application-master.parquet'),
                       APPLICATION_MASTER_RELEASE='synthetic-example-canonical-v1', END_YEAR='2024',
                       RUN_OUTPUT=str(output), BOUNDARIES=str(boundaries), TL3_PROPERTY='tl3_id',
                       REGION_PREFLIGHT=str(preflight), WORKERS='8', MAX_IN_FLIGHT='8', REQUEST_BUDGET='1000',
                       GEOGRAPHIC_POLICY=str(policy), GEOGRAPHIC_SERVICE=str(service_path),
                       GHSL_SOURCE_MANIFEST=str(ghsl), GEOGRAPHIC_BATCH_SIZE='1', GEOGRAPHIC_WORK_DIR=str(p/'work'),
                       GEOGRAPHIC_DURABLE_CACHE=str(p/'durable'))

            def pipeline(*args, environment=None):
                result = subprocess.run(['bash',str(ROOT/'scripts/run_pipeline.sh'),*args], env=environment or env,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
                return result

            pipeline()
            activity = json.loads((output/'06_activity/manifest.json').read_text())
            self.assertEqual(activity['docdb_families'], 3)
            self.assertEqual(float(activity['allocated_mass'])+float(activity['withheld_mass']), activity['family_role_units'])
            self.assertGreater(float(activity['output_totals']['inventors_direct.csv']), 0)
            self.assertGreater(float(activity['output_totals']['inventors_direct_street_pcdb_rg.csv']),
                               float(activity['output_totals']['inventors_direct_street_pcdb.csv']))
            subprocess.run([sys.executable,str(ROOT/'scripts/export_patent_traces.py'),
                '--run',str(output),'--family','docdb:880000001','--output',str(p/'trace.json')],
                env=env,check=True,capture_output=True)
            trace=json.loads((p/'trace.json').read_text())['cases'][0]
            street=next(address for address in trace['addresses'] if address['support']['support_method']=='actual_street_geometry_100m_support')
            self.assertTrue(street['request'])
            self.assertTrue(street['support_outcome'])
            self.assertTrue(street['support_responses'])
            self.assertTrue(street['ghsl_values'])
            before = {f.name: f.read_bytes() for f in (output/'06_activity').glob('*.csv')}
            network_calls = len(calls)
            pipeline('--from-stage','4')
            self.assertEqual(len(calls), network_calls)
            self.assertEqual(before, {f.name:f.read_bytes() for f in (output/'06_activity').glob('*.csv')})

            # Independently query street and postcode masks of one held-out direct fixture
            truth_source = table(p/'direct.csv', [dict(source_observation_id='fixture-direct',house_number='124',
                street='Oxford Road',city='Manchester',postal_code='M13 9RD',ctry_code='GB',reg_code='UKD33',
                true_latitude='53.47',true_longitude='-2.24',ground_truth_precision='address',
                appln_id='90000000001',person_id='example-inventor-1',actor='inventor',patent_year='2020',sample_partition='holdout')])
            prepare(truth_source,p/'masks',p/'hidden',expected_cases=1)
            masked = p/'masked'
            masked.mkdir()
            with (p/'masks/all-masks.csv').open() as incoming:
                rows = list(csv.DictReader(incoming))
            table(masked/'01_addresses.csv',rows)
            table(masked/'01_addresses.links.csv',rows)
            subprocess.run(['patent-code','clean','--input',str(masked/'01_addresses.csv'),'--output',str(masked/'02_cleaned.csv')],
                           env=env,check=True,capture_output=True)
            masked_env = dict(env, RUN_OUTPUT=str(masked), FAMILY_BACKBONE=str(output/'01_family_backbone.sqlite'),
                              GEOGRAPHIC_WORK_DIR=str(p/'masked-work'),GEOGRAPHIC_DURABLE_CACHE=str(p/'masked-durable'))
            pipeline('--from-stage','3',environment=masked_env)

            descriptors = p/'morrison.txt'
            descriptors.write_text('pat|yr|wasComplete\n'+''.join(f'EP{aid}|2020|1\n' for aid,_,_ in example.APPLICATIONS))
            references = {}
            for actor in ('inventor','applicant'):
                morrison = p/(actor+'.txt')
                morrison.write_text(''.join(f'person|EP{aid}|name|53.47,-2.24|address|high\n' for aid,_,_ in example.APPLICATIONS))
                references[actor] = morrison
                table(p/(actor+'.csv'),[dict(appln_id=aid,patent_office='EP',filing_date='2020-03-04',
                    lat='53.47',lng='-2.24',source='1',coord_source='geolocalization') for aid,_,_ in example.APPLICATIONS])
            validation_env = dict(env, MASKED_ALLOCATION_MANIFEST=str(masked/'05_grid/final_grid_manifest.json'),
                MASKED_QUERY_MANIFEST=str(masked/'03_geocoding/run_manifest.json'),MASKED_TRUTH=str(p/'hidden/truth.csv'),
                MORRISON_DESCRIPTORS=str(descriptors),MORRISON_INVENTORS=str(references['inventor']),MORRISON_APPLICANTS=str(references['applicant']),
                DE_RASSENFOSSE_INVENTORS=str(p/'inventor.csv'),DE_RASSENFOSSE_APPLICANTS=str(p/'applicant.csv'))
            pipeline('--from-stage','7',environment=validation_env)
            masking = json.loads((output/'07_validation/01_internal/summary.json').read_text())
            self.assertEqual(sum(row['evaluated'] for row in masking['metrics']), 2)
            figures = save(p/'figures.json',dict(method_flow=True,
                country_coverage=dict(source_roster=str(output/'01_addresses.links.csv'),before_geographic_filter=True,activity_manifest=str(output/'06_activity/manifest.json'),
                    geocoder_manifest=str(output/'03_geocoding/run_manifest.json'),geographic_handoff=str(output/'04_inputs/handoff.json'),
                    postcode_sizes=str(output/'04_inputs/postcode_sizes.csv')),
                within_clustering=dict(compare_tl3=True,activity_manifest=str(output/'06_activity/manifest.json'),
                    boundaries=str(boundaries),tl3_property='tl3_id',width_km=30,filing_years=[2000,2024],tier='direct_street_pcdb_rg',
                    region_label='Fixture region',focus=[-2.24,53.47]),
                applicant_inventor_grid=dict(compare_roles=True,activity_manifest=str(output/'06_activity/manifest.json'),
                    boundaries=str(boundaries),filing_years=[2000,2024],tier='direct_street_pcdb_rg'),
                postcode_sizes=dict(source=str(output/'04_inputs/postcode_sizes.csv'),support_release=str(output/'05_sampled/support_release.json')),
                internal_validation=dict(summary=str(output/'07_validation/01_internal/summary.json')),
                external_comparison=dict(activity_manifest=str(output/'06_activity/manifest.json'),references={
                    'morrison':str(output/'07_validation/02_external/morrison'),
                    'de_rassenfosse':str(output/'07_validation/02_external/de_rassenfosse')})))
            pipeline('--from-stage','8',environment=dict(env,FIGURES_CONFIG=str(figures)))
            self.assertTrue((output/'08_figures_tables/masking-validation.tex').is_file())
            for filename in ('country-coverage.tex','within-clustering.png','applicant-inventor-grid.png','dataset-counts.json'):
                self.assertGreater((output/'08_figures_tables'/filename).stat().st_size,0)
            with (output/'08_figures_tables/postcode-area-histogram.csv').open() as incoming:
                histogram = list(csv.DictReader(incoming))
            self.assertIn('postcode_point_single_cell', {row['construction_method'] for row in histogram})
            self.assertGreater((output/'08_figures_tables/postcode-area-histogram.png').stat().st_size,100)
            self.assertGreater((output/'08_figures_tables/august-regpat-validation.png').stat().st_size,100)



if __name__ == '__main__':
    unittest.main()
