import json
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from grid_fixture import _fixture
from weighting.allocate_to_grid import _normalized_weights, build


class WeightingMass(unittest.TestCase):
    def test_old_floodfill_flag_cannot_bypass_current_postcode_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            cross,routes,countries,temporal,release=_fixture(Path(directory))
            postcode=next(s for s in release['supports'] if s['observation_id']=='o-postcode')
            postcode['bounded_flood_fill']=True
            postcode.pop('postcode_construction_method')
            with self.assertRaisesRegex(ValueError,'postcode support'):
                build(cross_stage_release=cross,routing_rows=routes,country_rows=countries,
                      temporal_rows=temporal,support_release=release)

    def test_repeating_weights_conserve_mass_with_zero_cells(self):
        raw = [Decimal(0), Decimal(1), Decimal(2), Decimal(0)]
        weights = _normalized_weights(raw)
        self.assertEqual(sum(map(Fraction, weights)), 1)
        self.assertEqual(weights[0], 0)
        self.assertEqual(weights[-1], 0)
        self.assertGreater(weights[1], 0)

    def test_polygon_and_neighbourhood_reach_weighting_with_exact_mass(self):
        from geographic_uncertainty.retrieve_and_rasterise import acquire, finalize
        from test_postcode_limits import PostcodeLimits, tl3_fixture
        for polygon in (False, True):
            with self.subTest(polygon=polygon), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cross, routes, countries, temporal, release = _fixture(root)
                request = dict(request_id='request-test', observation_id='o-postcode', validation_id='v-postcode',
                    route='postcode_support_request',request_kind='postcode_seed_cell',country_code='GB',postcode='M1 1AA',
                    seed_longitude='-2.24',seed_latitude='53.48',seed_candidate_id='c1',selected_query_id='q1',
                    osm_type='',osm_id='',max_cells=10000,max_attempts=1,retry_backoff_ms=0,query_timeout_seconds=1,
                    postcode_boundary_mode='polygon_first_1km',expected_tl3='TEST')
                service=dict(endpoint='http://localhost:8161',allocated_cpus=16,allocated_memory_gb=64,
                    version='5.3.3',source_label='synthetic',max_queue=10000,max_attempts=1,retry_backoff_ms=0,request_budget=20)
                service['point_tl3_boundaries']=tl3_fixture(root)
                payload=PostcodeLimits().polygon() if polygon else PostcodeLimits().point()
                def getter(url, timeout):
                    return 200,json.dumps([payload] if '/search?' in url else {'address': {'country_code': 'gb', 'postcode': 'OTHER'}}).encode()
                ledger, manifest=acquire([request],service,raw_dir=root/'raw',getter=getter)
                self.assertEqual(manifest['network_requests'],1 if polygon else 9)
                route=next(r for r in routes if r['observation_id']=='o-postcode')
                route['validation_id']='v-postcode'
                outcomes,supports,_=finalize([request],[route],ledger,service_identity=service)
                support=supports[0];support['grid_identity']=release['grid_identity']
                release['supports']=[s for s in release['supports'] if s['observation_id']!='o-postcode']+[support]
                oldroles={'postcode_query_ledger_o_postcode','postcode_accepted_support_o_postcode'}
                if not polygon:
                    release['artifacts'].append({'role':support['source_artifacts']['tl3_boundaries'],'path':service['point_tl3_boundaries']['path']})
                release['artifacts']=[a for a in release['artifacts'] if a['role'] not in oldroles]
                for key,data in [('query_ledger',ledger),('accepted_support',support['accepted_cells'])]:
                    path=root/(key+'.json');path.write_text(json.dumps(data))
                    release['artifacts'].append({'role':support['source_artifacts'][key],'path':str(path)})
                outcome_path=next(Path(a['path']) for a in release['artifacts'] if a['role']=='stage_a_outcomes')
                import csv
                with outcome_path.open() as f:
                    reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
                rows=[outcomes[0] if r['observation_id']=='o-postcode' else r for r in rows]
                with outcome_path.open('w') as f:
                    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
                values_path=next(Path(a['path']) for a in release['artifacts'] if a['role']=='ghsl_values')
                values=json.loads(values_path.read_text());values['values_by_support'].pop('s-postcode')
                values['values_by_support'][support['support_id']]={c['cell_id_100m']:{str(e):str(0 if i%7==0 else i+1) for e in (2010,2015,2020)} for i,c in enumerate(support['accepted_cells'])}
                values_path.write_text(json.dumps(values))
                from unittest.mock import patch
                import weighting.sample_built_surface as sample_module
                original_validate = sample_module._validate_supports
                with patch.object(sample_module, '_validate_supports', wraps=original_validate) as validator:
                    cells,parents,_,audit=build(cross_stage_release=cross,routing_rows=routes,country_rows=countries,temporal_rows=temporal,support_release=release)
                self.assertEqual(validator.call_count, 1)
                # A later invocation must independently validate changed evidence
                with patch.object(sample_module, '_validate_supports', side_effect=ValueError('changed evidence')):
                    with self.assertRaisesRegex(ValueError, 'changed evidence'):
                        build(cross_stage_release=cross,routing_rows=routes,country_rows=countries,temporal_rows=temporal,support_release=release)
                selected=[r for r in parents if r['observation_id']=='o-postcode']
                by_estimate=defaultdict(Fraction)
                for row in selected:by_estimate[row['estimate_id']]+=Fraction(row['normalized_weight'])
                self.assertTrue(by_estimate)
                self.assertTrue(all(v==1 for v in by_estimate.values()))
                self.assertTrue(audit['all_100m_estimate_mass_one'])
                self.assertTrue(audit['all_1km_estimate_mass_one'])
                if not polygon:
                    self.assertEqual({r['evidence_type'] for r in selected},{'postcode_neighbourhood'})
                    self.assertEqual(len({r['grid_cell_id'] for r in selected}),1)
                    self.assertEqual({r['scenario_id'] for r in selected},{'postcode_1km_centre_built_surface_approximation'})

    def test_many_cells_and_parents_conserve_exact_mass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cross, routes, countries, temporal, release = _fixture(root)
            street = next(s for s in release['supports'] if s['observation_id'] == 'o-street')
            cells = [dict(cell_id_100m=f'r201c{201 + index * 2}',
                          parent_cell_id_1km=f'r20c{(201 + index * 2) // 10}') for index in range(150)]
            street.update(accepted_cells=cells, support_cell_count=len(cells), support_area_m2=len(cells) * 10000)
            values_path = next(Path(a['path']) for a in release['artifacts'] if a['role'] == 'ghsl_values')
            values = json.loads(values_path.read_text())
            values['values_by_support']['s-street'] = {
                cell['cell_id_100m']: {str(epoch): str(0 if index % 7 == 0 else index + 1)
                                     for epoch in (2010, 2015, 2020)}
                for index, cell in enumerate(cells)
            }
            values_path.write_text(json.dumps(values))
            rows_100m, rows_1km, _, _ = build(cross_stage_release=cross, routing_rows=routes,
                                            country_rows=countries, temporal_rows=temporal,
                                            support_release=release)
            for rows in (rows_100m, rows_1km):
                masses = defaultdict(Fraction)
                for row in rows:
                    weight = Fraction(row['normalized_weight'])
                    self.assertGreater(weight, 0)
                    masses[row['estimate_id']] += weight
                self.assertTrue(masses)
                self.assertTrue(all(mass == 1 for mass in masses.values()))


if __name__ == '__main__':
    unittest.main()


class StreamingAllocation(unittest.TestCase):
    def test_chunked_csvs_and_audit_match_single_build(self):
        import csv
        import itertools
        from types import SimpleNamespace
        from unittest.mock import patch
        import weighting.allocate_to_grid as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cross, routes, countries, temporal, support = _fixture(root)
            for name, rows, fields, key in [('routing', routes, module.ROUTING_FIELDS, 'routing'), ('countries', countries, module.COUNTRY_FIELDS, 'country_groups'), ('temporal', temporal, module.TEMPORAL_FIELDS, 'temporal_child')]:
                path = root / (name + '.csv')
                with path.open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, fields)
                    writer.writeheader()
                    writer.writerows(rows)
                cross[key] = {'path': str(path)}
            (root / 'cross.json').write_text(json.dumps(cross))
            (root / 'support.json').write_text(json.dumps(support))
            def ids():
                counter = itertools.count()
                return lambda: SimpleNamespace(hex=f'{next(counter):032x}')
            with patch.object(module, 'uuid4', side_effect=ids()):
                fine, coarse, held, audit = module.build(cross_stage_release=cross, routing_rows=routes, country_rows=countries, temporal_rows=temporal, support_release=support)
            with patch.object(module, 'uuid4', side_effect=ids()), patch.object(module, 'MAX_EXPANDED_CELL_ROWS', 2):
                module.run(cross_stage_release_path=root / 'cross.json', support_release_path=root / 'support.json', output_dir=root / 'out')
            for name, fields, rows in [('support_weights_100m.csv', module.CELL_FIELDS, fine), ('estimated_grid_1km.csv', tuple(f for f in module.CELL_FIELDS if f != 'support_id'), coarse), ('withheld.csv', module.WITHHELD_FIELDS, held)]:
                self.assertEqual((root / 'out' / name).read_bytes(), module._csv_bytes(fields, rows), name)
            self.assertEqual(json.loads((root / 'out/audit.json').read_text()), audit)

    def test_reused_vectors_keep_link_ids_years_roles_and_withholding(self):
        import itertools
        from collections import OrderedDict
        from types import SimpleNamespace
        from unittest.mock import patch
        import weighting.allocate_to_grid as module
        with tempfile.TemporaryDirectory() as directory:
            cross, routes, countries, temporal, support = _fixture(Path(directory))
            temporal = temporal + [{**r, 'appln_id': r['appln_id']+'-second', 'person_id': 'second-person', 'actor': 'applicant' if r['actor']=='inventor' else 'inventor'} for r in temporal]
            temporal += [{**temporal[0], 'appln_id':'missing-year', 'patent_year_status':'missing'}]
            temporal += [{**r,'appln_id':r['appln_id']+'-same-epoch','patent_year':str(int(r['patent_year'])+1 if int(r['patent_year'])%5<4 else int(r['patent_year'])-1)} for r in temporal[:3]]
            def ids():
                counter=itertools.count()
                return lambda: SimpleNamespace(hex=f'{next(counter):032x}')
            args=dict(cross_stage_release=cross,routing_rows=routes,country_rows=countries,temporal_rows=temporal,support_release=support)
            with patch.object(module,'uuid4',side_effect=ids()):
                expected=module.build(**args)
            original=module.build
            with patch.object(module,'uuid4',side_effect=ids()), patch.object(module,'build',wraps=original) as calls:
                actual=module._build_reusing_vectors(**args,cache=OrderedDict(),validated_postcodes=set(),prepared={})
            self.assertEqual(actual,expected)
            self.assertLess(calls.call_count,len(temporal))
