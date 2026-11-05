import json
import tempfile
import unittest
from pathlib import Path

from geographic_uncertainty.retrieve_and_rasterise import finalize


class StreetSupport(unittest.TestCase):
    def run_geometry(self, geometry, max_cells=225, extra_geometry=None, missing_segment=False):
        with tempfile.TemporaryDirectory() as directory:
            response = Path(directory) / 'lookup.json'
            response.write_text(json.dumps([dict(osm_type='way', osm_id=1,
                                                address={'country_code': 'gb'}, geojson=geometry)]))
            if extra_geometry is not None and not missing_segment:
                data=json.loads(response.read_text());data.append(dict(osm_type='way',osm_id=2,address={'country_code':'gb'},geojson=extra_geometry));response.write_text(json.dumps(data))
            request = dict(request_id='r1', observation_id='o1', validation_id='v1',
                           route='street_support_request', request_kind='osm_street_geometry',
                           country_code='GB', osm_type='way', osm_id='1', max_cells=max_cells,
                           seed_candidate_id='c1', selected_query_id='q1',
                           seed_latitude='53.48', seed_longitude='-2.24')
            if extra_geometry is not None:
                request['osm_objects']=json.dumps([dict(osm_type='way',osm_id='1'),dict(osm_type='way',osm_id='2')])
            route = dict(observation_id='o1', validation_id='v1', route='street_support_request',
                         status='accepted', seed_tl3_pass='1')
            row = dict(request_id='r1', observation_id='o1', subrequest_id='lookup-a1',
                       request_kind='lookup', cell_id_100m='', attempt=1, retryable='false',
                       error_class='', response_path=str(response), response_id=str(response))
            service = dict(endpoint='http://localhost:8161', allocated_cpus=16, allocated_memory_gb=64,
                           version='5.3.2', source_label='test', max_queue=225, max_attempts=1,
                           retry_backoff_ms=0, request_budget=100)
            return finalize([request], [route], [row], service_identity=service)

    def test_non_line_geometry_is_withheld(self):
        for geometry in (dict(type='Point', coordinates=[-2.24, 53.48]),
                         dict(type='Polygon', coordinates=[]), None,
                         dict(type='LineString', coordinates=[['invalid', 53.48], [-2.24, 53.48]])):
            with self.subTest(geometry=geometry):
                outcomes, supports, _ = self.run_geometry(geometry)
                self.assertEqual(outcomes[0]['status'], 'withheld')
                self.assertEqual(outcomes[0]['reason'], 'street_geometry_invalid_or_unsupported')
                self.assertEqual(supports, [])

    def test_line_geometry_produces_support(self):
        outcomes, supports, _ = self.run_geometry(dict(type='LineString',
                                                      coordinates=[[-2.24, 53.48], [-2.239, 53.481]]))
        self.assertEqual(outcomes[0]['status'], 'support_ready')
        self.assertGreater(supports[0]['street_length_m'], 0)

    def test_segment_geometries_are_unioned_without_duplicate_cells(self):
        first=dict(type='LineString',coordinates=[[-2.24,53.48],[-2.239,53.481]])
        other=dict(type='LineString',coordinates=[[-2.239,53.481],[-2.238,53.482]])
        _,single,_=self.run_geometry(first)
        outcomes,supports,_=self.run_geometry(first,extra_geometry=other)
        self.assertEqual(outcomes[0]['status'],'support_ready')
        cells=supports[0]['accepted_cells']
        self.assertEqual(len(cells),len({c['cell_id_100m'] for c in cells}))
        self.assertGreaterEqual(len(cells),len(single[0]['accepted_cells']))
        self.assertEqual(supports[0]['selected_osm_objects'],[['way','1'],['way','2']])
        outcomes,supports,_=self.run_geometry(first,extra_geometry=other,missing_segment=True)
        self.assertEqual(outcomes[0]['status'],'withheld')
        self.assertEqual(supports,[])

    def test_street_cell_limit_is_withheld(self):
        outcomes, supports, _ = self.run_geometry(dict(type='LineString',
                                                      coordinates=[[-2.24, 53.48], [-2.23, 53.49]]), max_cells=1)
        self.assertEqual(outcomes[0]['status'], 'withheld')
        self.assertEqual(outcomes[0]['reason'], 'street_cell_cap_reached')
        self.assertEqual(supports, [])


if __name__ == '__main__':
    unittest.main()
