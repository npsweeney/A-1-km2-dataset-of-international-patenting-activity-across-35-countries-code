import json
import tempfile
import unittest
from pathlib import Path

from geographic_uncertainty.retrieve_and_rasterise import acquire, finalize, grid_cell, cell_centroid, _postcode_polygon_cells
from urllib.parse import urlparse, parse_qs


def tl3_fixture(directory, lon=-2.24, lat=53.48):
    path=Path(directory)/'tl3.geojson'
    path.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
        'properties':{'tl3_id':'TEST'},'geometry':{'type':'Polygon','coordinates':[[
        [lon-1,lat-1],[lon+1,lat-1],[lon+1,lat+1],[lon-1,lat+1],[lon-1,lat-1]]]}}]}))
    return {'path':str(path),'code_property':'tl3_id'}

class PostcodeLimits(unittest.TestCase):

    def test_geographic_responses_reuse_existing_store_and_keep_every_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = dict(endpoint='http://localhost:8184',version='5.3.3',source_label='synthetic',max_queue=10000,request_budget=100,max_attempts=1,retry_backoff_ms=0,client_workers=4,point_tl3_boundaries=tl3_fixture(root),response_cache_dir=str(root/'cache'),database_system_identifier='synthetic-db',data_revision='revision-one')
            request = dict(request_id='a',observation_id='a',validation_id='a',request_kind='postcode_seed_cell',country_code='GB',postcode='M1 1AA',expected_tl3='TEST',seed_longitude='-2.24',seed_latitude='53.48',max_cells=5000,max_attempts=1,retry_backoff_ms=0,query_timeout_seconds=10,postcode_boundary_mode='polygon_first_1km')
            requests = [request,{**request,'request_id':'b','observation_id':'b','seed_longitude':'-2.241'}]
            count = []
            def getter(url, timeout):
                count.append(url)
                payload = [dict(type='postcode',name='M1 1AA',address=dict(country_code='gb',postcode='M1 1AA'),geojson=dict(type='Point',coordinates=[-2.24,53.48]))] if '/search?' in url else dict(address=dict(country_code='gb',postcode='OTHER'))
                return 200,json.dumps(payload).encode()
            ledger,manifest = acquire(requests,service,raw_dir=root/'cold',getter=getter)
            self.assertEqual(len(count),9)
            self.assertEqual(manifest['network_requests'],9)
            self.assertEqual(manifest['cache_hits'],9)
            self.assertEqual(len({x['response_path'] for x in ledger}),9)
            self.assertTrue(all(str(root/'cache') in x['response_path'] for x in ledger))
            self.assertEqual({x['observation_id'] for x in ledger},{'a','b'})
            import sqlite3
            with sqlite3.connect(root/'cache/nominatim-cache.sqlite3') as db:
                db.execute('VACUUM')
            def forbidden(*args):raise AssertionError('warm replay made a network request')
            warm,manifest = acquire(requests,service,raw_dir=root/'warm',getter=forbidden)
            self.assertEqual(manifest['network_requests'],0)
            self.assertEqual(manifest['cache_hits'],18)
            self.assertEqual({(x['observation_id'],x['url']):x['response_path'] for x in ledger},{(x['observation_id'],x['url']):x['response_path'] for x in warm})
            self.assertEqual({(x['observation_id'],x['url']):Path(x['response_path']).read_bytes() for x in ledger},{(x['observation_id'],x['url']):Path(x['response_path']).read_bytes() for x in warm})
            acquire(requests,{**service,'data_revision':'revision-two'},raw_dir=root/'changed',getter=getter)
            self.assertEqual(len(count),18)
            with self.assertRaisesRegex(ValueError,'data_revision'):
                acquire(requests,{**service,'data_revision':''},raw_dir=root/'invalid',getter=getter)

    def test_polygon_area_reuse_does_not_reuse_seed_qualification(self):
        from geographic_uncertainty.retrieve_and_rasterise import _rasterise_postcode_polygon
        _rasterise_postcode_polygon.cache_clear()
        request=dict(country_code='GB',postcode='M1 1AA',seed_longitude='-2.24',seed_latitude='53.48',max_cells=5000)
        first=_postcode_polygon_cells([self.polygon()],request)
        second=_postcode_polygon_cells([self.polygon()],{**request,'seed_longitude':'-2.239'})
        self.assertEqual(first['cells'],second['cells'])
        self.assertEqual(_rasterise_postcode_polygon.cache_info().hits,1)
        self.assertIsNone(_postcode_polygon_cells([self.polygon()],{**request,'seed_longitude':'0'}))
        self.assertIsNone(_postcode_polygon_cells([self.polygon()],{**request,'country_code':'FR'}))

    def test_derived_polygon_area_survives_restart_without_skipping_seed_guard(self):
        from unittest.mock import patch
        from geographic_uncertainty.retrieve_and_rasterise import _geographic_cache
        with tempfile.TemporaryDirectory() as directory:
            service=dict(endpoint='http://localhost:8184',response_cache_dir=directory,database_system_identifier='synthetic-db',data_revision='one',version='5.3.3')
            request=dict(country_code='GB',postcode='M1 1AA',seed_longitude='-2.24',seed_latitude='53.48',max_cells=5000)
            with _geographic_cache(service) as (cache,identity):
                first=_postcode_polygon_cells([self.polygon()],request,cache,identity)
            with _geographic_cache(service) as (cache,identity), patch('geographic_uncertainty.retrieve_and_rasterise._rasterise_postcode_polygon',side_effect=AssertionError('repeated rasterization')):
                second=_postcode_polygon_cells([self.polygon()],request,cache,identity)
                self.assertEqual([list(x) for x in first['cells']],second['cells'])
                self.assertEqual(cache.telemetry()['cache_hits'],1)
                self.assertIsNone(_postcode_polygon_cells([self.polygon()],{**request,'seed_longitude':'0'},cache,identity))

    def test_transient_geographic_failures_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);service=dict(endpoint='http://localhost:8184',version='5.3.3',source_label='synthetic',max_queue=10000,request_budget=10,max_attempts=1,retry_backoff_ms=0)
            request=dict(request_id='a',observation_id='a',request_kind='osm_street_geometry',osm_type='way',osm_id='1',query_timeout_seconds=10,max_attempts=1,retry_backoff_ms=0)
            calls=[]
            def getter(url,timeout):calls.append(url);return 503,b'{}'
            ledger,manifest=acquire([request,{**request,'request_id':'b','observation_id':'b'}],service,raw_dir=root/'failed',getter=getter)
            self.assertEqual(len(calls),2)
            self.assertEqual(manifest['cache_hits'],0)

    def test_postcode_size_export_distinguishes_unknown_extent_and_withheld(self):
        from geographic_uncertainty.retrieve_and_rasterise import postcode_size_rows
        requests = [dict(observation_id=x, request_kind='postcode_seed_cell', country_code='GB', postcode=x, max_cells=5000) for x in ('kept','withheld')]
        outcomes = [dict(observation_id='kept',status='support_ready',reason=''), dict(observation_id='withheld',status='withheld',reason='postcode_area_limit_exceeded')]
        supports = [dict(observation_id='kept',support_area_m2=1000000,postcode_construction_method='postcode_point_single_cell',postcode_extent_status='unresolved_missing_neighbour_postcode')]
        rows = postcode_size_rows(requests,outcomes,supports)
        self.assertEqual(rows[0]['allocated_area_km2'],1)
        self.assertEqual(rows[0]['extent_status'],'unresolved_missing_neighbour_postcode')
        self.assertEqual(rows[0]['area_cap_km2'],50)
        self.assertEqual(rows[1]['allocated_area_km2'],'')
        self.assertEqual(rows[1]['reason'],'postcode_area_limit_exceeded')

    def run_bounded_search(self, max_cells, matching_extent=None, country="gb", boundary_payload=None, boundary_mode="polygon_first_1km", validate_polygon=False, expected_tl3="TEST"):
        service = dict(endpoint='http://localhost:8161', allocated_cpus=16, allocated_memory_gb=64,
                       version='5.3.2', source_label='test', max_queue=25, max_attempts=1,
                       retry_backoff_ms=0, request_budget=1000)
        request = dict(request_id='r1', observation_id='o1', validation_id='v1',
                       route='postcode_support_request', request_kind='postcode_seed_cell',
                       country_code='GB', postcode='M1 1AA', osm_type='', osm_id='', postcode_boundary_mode=boundary_mode,
                       max_cells=max_cells, max_attempts=1, retry_backoff_ms=0,
                       query_timeout_seconds=1, seed_candidate_id='c1', selected_query_id='q1',
                       seed_latitude='53.48', seed_longitude='-2.24', expected_tl3=expected_tl3)
        if boundary_mode is None:
            request.pop('postcode_boundary_mode')
        route = dict(observation_id='o1', validation_id='v1', route='postcode_support_request',
                     status='accepted')
        seed_row, seed_column = grid_cell(-2.24, 53.48)
        def getter(url, timeout):
            if '/search?' in url:
                return 200, json.dumps(boundary_payload or []).encode()
            query = parse_qs(urlparse(url).query)
            row, column = grid_cell(float(query['lon'][0]), float(query['lat'][0]))
            inside = matching_extent is None or (row == seed_row and abs(column - seed_column) <= matching_extent)
            return 200, json.dumps({'address': {'country_code': country, 'postcode': 'M1 1AA' if inside else 'M2 2BB'}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            service['point_tl3_boundaries']=tl3_fixture(directory)
            rows, _ = acquire([request], service, raw_dir=Path(directory) / 'raw',
                              getter=getter)
            results = finalize([request], [route], rows, service_identity=service)
            if validate_polygon:
                from weighting.sample_built_surface import _validate_supports
                import copy
                ledger = Path(directory) / 'query-ledger.json'
                accepted = Path(directory) / 'accepted.json'
                ledger.write_text(json.dumps(rows))
                accepted.write_text(json.dumps(results[1][0]['accepted_cells']))
                sources = {'postcode_query_ledger_r1': ledger, 'postcode_support_r1': accepted}
                if results[1][0].get('point_provenance'):
                    sources['postcode_tl3_boundaries_r1']=Path(service['point_tl3_boundaries']['path'])
                _validate_supports({'supports': results[1]}, {'o1':route}, {'o1':results[0][0]}, sources)
                changed = copy.deepcopy(results[1])
                changed[0]['accepted_cells'] = changed[0]['accepted_cells'][1:]
                with self.assertRaisesRegex(ValueError, 'raster differs'):
                    _validate_supports({'supports': changed}, {'o1':route}, {'o1':results[0][0]}, sources)
            return results


    def polygon(self):
        return dict(type='postal_code', osm_type='relation', osm_id=101,
                    address={'country_code': 'gb', 'postcode': 'M1 1AA'},
                    geojson={'type': 'Polygon', 'coordinates': [
                        [[-2.25,53.47],[-2.23,53.47],[-2.23,53.49],[-2.25,53.49],[-2.25,53.47]],
                        [[-2.247,53.472],[-2.243,53.472],[-2.243,53.476],[-2.247,53.476],[-2.247,53.472]] ]})


    def test_superseded_postcode_modes_are_rejected(self):
        for mode in (None, 'flood_fill', 'polygon_first', 'polygon_first_neighbourhood_3x3'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'polygon_first_1km'):
                self.run_bounded_search(5000, boundary_mode=mode)

    def test_default_policy_budget_includes_boundary_search(self):
        root = Path(__file__).resolve().parents[1]
        policy = json.loads((root/'config/geographic_policy.template.json').read_text())
        service = json.loads((root/'config/geographic_service.template.json').read_text())
        self.assertEqual(policy['postcode_boundary_mode'], 'polygon_first_1km')
        self.assertEqual(policy['max_cells_per_observation'], 225)
        required = 1000 * (1 + min(1000,policy['max_postcode_cells_per_observation'])) * policy['max_attempts']
        self.assertGreaterEqual(policy['request_budget'], required)
        self.assertGreaterEqual(service['request_budget'], required)

    def test_wrong_country_postcode_and_ambiguous_polygons_do_not_qualify(self):
        request=dict(country_code='GB',postcode='M1 1AA',seed_longitude='-2.24',seed_latitude='53.48',max_cells=10000)
        country=self.polygon();country['address']['country_code']='fr'
        postcode=self.polygon();postcode['address']['postcode']='M2 2BB'
        point=self.polygon();point['geojson']={'type':'Point','coordinates':[-2.24,53.48]}
        second=self.polygon();second['osm_id']=102
        for payload in [[country],[postcode],[point],[self.polygon(),second]]:
            self.assertIsNone(_postcode_polygon_cells(payload,request))


    def point(self, lon=-2.24, lat=53.48, postcode='M1 1AA', country='gb'):
        return dict(type='postcode', address={'country_code': country, 'postcode': postcode},
                    geojson={'type': 'Point', 'coordinates': [lon, lat]})



class CoarsePostcodeSupport(unittest.TestCase):
    point = PostcodeLimits.point
    polygon = PostcodeLimits.polygon
    def coarse(self, *, mismatch=False, fail=False, cap=1000, payload=None, country='gb', matching_radius=0, reverse_payload=None, missing_neighbour=False, missing_corner=False):
        from geographic_uncertainty.retrieve_and_rasterise import _coarse_postcode_cells
        with tempfile.TemporaryDirectory() as directory:
            request=dict(request_id='r1',observation_id='o1',validation_id='v1',route='postcode_support_request',request_kind='postcode_seed_cell',country_code='GB',postcode='M1 1AA',osm_type='',osm_id='',postcode_boundary_mode='polygon_first_1km',max_cells=cap,max_attempts=1,retry_backoff_ms=0,query_timeout_seconds=1,seed_candidate_id='c1',selected_query_id='q1',seed_latitude='53.48',seed_longitude='-2.24',expected_tl3='TEST')
            service=dict(endpoint='http://localhost:8161',allocated_cpus=16,allocated_memory_gb=64,version='5.3.3',source_label='fixture',max_queue=1000,max_attempts=1,retry_backoff_ms=0,request_budget=2000,point_tl3_boundaries=tl3_fixture(directory))
            fine=grid_cell(-2.24,53.48);start=(fine[0]//10,fine[1]//10)
            def getter(url, timeout):
                if '/search?' in url:return 200,json.dumps(payload if payload is not None else [self.point()]).encode()
                if fail:return 503,b'{}'
                q=parse_qs(urlparse(url).query);r,c=grid_cell(float(q['lon'][0]),float(q['lat'][0]))
                self.assertNotEqual((r//10,c//10),start, 'Validated seed cell must not be reverse queried')
                same=abs(r//10-start[0])<=matching_radius and abs(c//10-start[1])<=matching_radius
                if reverse_payload is not None:return 200,json.dumps(reverse_payload).encode()
                if missing_neighbour and (r//10,c//10)!=start:return 200,json.dumps({'address':{'country_code':'gb'}}).encode()
                if missing_corner and (r//10,c//10)==(start[0]-1,start[1]-1):return 200,json.dumps({'address':{'country_code':'gb'}}).encode()
                return 200,json.dumps({'address':{'country_code':country,'postcode':'M1 1AA' if same and not mismatch else 'other'}}).encode()
            ledger,manifest=acquire([request],service,raw_dir=Path(directory)/'raw',getter=getter)
            route=dict(observation_id='o1',validation_id='v1',route='postcode_support_request',status='accepted')
            outcomes,supports,_=finalize([request],[route],ledger,service_identity=service)
            if supports:
                from weighting.sample_built_surface import _validate_supports
                query=Path(directory)/'ledger.json';query.write_text(json.dumps(ledger))
                accepted=Path(directory)/'accepted.json';accepted.write_text(json.dumps(supports[0]['accepted_cells']))
                sources={'postcode_query_ledger_r1':query,'postcode_support_r1':accepted}
                if supports[0].get('point_provenance'): sources['postcode_tl3_boundaries_r1']=Path(service['point_tl3_boundaries']['path'])
                _validate_supports({'supports':supports},{'o1':route},{'o1':outcomes[0]},sources)
            return outcomes,supports,manifest

    def test_coarse_component_closes_and_revalidates(self):
        outcomes,supports,manifest=self.coarse(matching_radius=1)
        self.assertEqual(outcomes[0]['status'],'support_ready')
        self.assertEqual(supports[0]['postcode_construction_method'],'postcode_1km_centre_flood_fill')
        self.assertEqual(supports[0]['support_cell_count'],900)
        self.assertEqual(manifest['reverse_queries'],24)
        self.assertEqual(supports[0]['postcode_seed_rule'],'validated_point_containing_cell_v1')

    def test_validated_point_keeps_seed_when_all_neighbour_centres_differ(self):
        outcomes,supports,manifest=self.coarse(mismatch=True)
        self.assertEqual(supports[0]['postcode_construction_method'],'postcode_point_single_cell')
        self.assertEqual(supports[0]['support_cell_count'],100)
        self.assertEqual(manifest['reverse_queries'],8)

    def test_seed_cell_needs_no_centre_confirmation(self):
        outcomes,supports,manifest=self.coarse()
        self.assertEqual(supports[0]['postcode_construction_method'],'postcode_point_single_cell')
        self.assertEqual(supports[0]['support_cell_count'],100)
        self.assertEqual(manifest['reverse_queries'],8)

    def test_missing_neighbour_fields_keep_only_validated_seed_with_unknown_extent(self):
        for payload in [{}, {'address':{}}, {'address':{'country_code':'gb'}},
                        {'address':{'country_code':'gb','postcode':'   '}},
                        {'address':{'postcode':'other'}}]:
            outcomes,supports,_=self.coarse(reverse_payload=payload)
            self.assertEqual(outcomes[0]['status'],'support_ready')
            self.assertEqual(supports[0]['support_cell_count'],100)
            self.assertEqual(supports[0]['postcode_extent_status'],'unresolved_missing_neighbour_postcode')

    def test_missing_neighbour_is_not_a_closed_boundary(self):
        outcomes,supports,_=self.coarse(missing_neighbour=True)
        self.assertEqual(outcomes[0]['status'],'support_ready')
        self.assertEqual(supports[0]['support_cell_count'],100)
        self.assertEqual(supports[0]['postcode_extent_status'],'unresolved_missing_neighbour_postcode')

    def test_unknown_frontier_retains_confirmed_neighbour_cells(self):
        outcomes,supports,_=self.coarse(matching_radius=1,missing_corner=True)
        self.assertGreater(supports[0]['support_cell_count'],100)
        self.assertEqual(supports[0]['known_matching_cells_not_allocated'],0)
        self.assertEqual(supports[0]['postcode_extent_status'],'unresolved_missing_neighbour_postcode')

    def test_known_oversized_component_stays_excluded_despite_missing_fields(self):
        outcomes,supports,_=self.coarse(matching_radius=3,missing_corner=True,cap=2500)
        self.assertEqual(supports,[])
        self.assertEqual(outcomes[0]['reason'],'postcode_area_limit_exceeded')

    def test_failed_start_or_unclosed_cap_cannot_become_single_cell(self):
        for kwargs in [dict(fail=True),dict(cap=1)]:
            outcomes,supports,_=self.coarse(**kwargs)
            self.assertEqual(supports,[])
            self.assertNotEqual(outcomes[0]['status'],'support_ready')

    def test_unqualified_point_cannot_become_single_cell(self):
        for payload in [[],[self.point(country='fr')],[self.point(postcode='other')],[self.point(lon=2.35,lat=48.86)],[self.point(),self.point(lon=-2.241)]]:
            outcomes,supports,manifest=self.coarse(payload=payload)
            self.assertEqual(supports,[])
            self.assertEqual(manifest['reverse_queries'],0)

    def test_polygon_precedes_coarse_and_single_cell(self):
        outcomes,supports,manifest=self.coarse(payload=[self.polygon()])
        self.assertEqual(supports[0]['postcode_construction_method'],'qualified_osm_postcode_polygon')
        self.assertEqual(manifest['reverse_queries'],0)

    def test_1km_centres_roundtrip_without_changing_100m_default(self):
        for lon,lat in [(-2.24,53.48),(19.04,47.52),(2.35,48.86),(-75.7,45.4)]:
            row,col=grid_cell(lon,lat)
            centre=cell_centroid(row//10,col//10,resolution_m=1000)
            fine=grid_cell(*centre)
            self.assertEqual((fine[0]//10,fine[1]//10),(row//10,col//10))
            self.assertEqual(cell_centroid(row,col),cell_centroid(row,col,resolution_m=100))
        with self.assertRaises(ValueError):cell_centroid(10,10,resolution_m=500)

    def test_coarse_area_above_limit_is_withheld_not_truncated_or_point_fallback(self):
        outcomes,supports,manifest=self.coarse(cap=100,matching_radius=1)
        self.assertEqual(supports,[])
        self.assertEqual(outcomes[0]['reason'],'postcode_area_limit_exceeded')
        self.assertLess(manifest['reverse_queries'],25)

    def test_exactly_at_area_limit_is_retained(self):
        outcomes,supports,_=self.coarse(cap=900,matching_radius=1)
        self.assertEqual(outcomes[0]['status'],'support_ready')
        self.assertEqual(supports[0]['support_area_m2'],9000000)

if __name__ == '__main__':
    unittest.main()
