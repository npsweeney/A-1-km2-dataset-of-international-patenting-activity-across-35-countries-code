"""Synthetic contract controls; these are not empirical geocoding results"""
from contextlib import closing
import csv
import json
import tempfile
import sqlite3
import unittest
from pathlib import Path
from geographic_uncertainty.prepare_geographic_inputs import build, SCHEMA
from geographic_uncertainty.retrieve_and_rasterise import prepare
from geographic_uncertainty.contracts import TEMPORAL_FIELDS

class ReviewHandoffTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.observations = []
        self.selections = []
        self.candidates = []
        self.queries = []
        self.temporal = []
        for i, precision in enumerate(('address', 'street', 'postcode', 'locality')):
            oid, cid = (f'o{i}', f'c{i}')
            self.observations.append(dict(observation_id=oid, address='Example Road 1, AB1 2CD', postal_code='AB1 2CD', locality='Example', ctry_code='CA', reg_code='CA01', region_resolution_status='ready', resolved_expected_tl3='CA01'))
            self.selections.append(dict(observation_id=oid, precision=precision, allocation_precision=precision if precision != 'locality' else None, selected_candidate_ids=[cid], selected_coordinate=[45, -75] if precision == 'address' else None, resolution_status='synthetic_control'))
            self.candidates.append(dict(observation_id=oid, candidate_id=cid, lat=45, lon=-75, tl3_match=True, postcode_match=True, postcode_candidate="AB1 2CD", query_index=0, query_rank=0, query_variant='address', road_expected='Example Road', road_candidate='Example Road', road_match=True, raw_candidate=dict(osm_type='way', osm_id=i + 1, address=dict(country_code='ca'))))
            self.queries.append(dict(observation_id=oid, query_index=0, query='synthetic_control'))
            self.temporal.append(dict(family_id=f'docdb:{i}', location_evidence_id=oid, observation_id=oid, appln_id=str(i), person_id='', actor='inventor', patent_year='2020', patent_year_status='valid'))

    def save(self):
        for name, rows in (('observations.csv', self.observations), ('temporal.csv', self.temporal)):
            path = self.run / name
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=TEMPORAL_FIELDS if name == 'temporal.csv' else list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        with (self.run / 'address_links.csv').open('w', newline='') as handle:
            fields = ['observation_id', 'appln_id', 'person_id', 'actor', 'patent_year', 'patent_year_status']
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.temporal)
        for name, rows in (('selections.jsonl', self.selections), ('candidates.jsonl', self.candidates), ('queries.jsonl', self.queries)):
            (self.run / name).write_text(''.join((json.dumps(row) + '\n' for row in rows)))

    def build(self):
        self.save()
        return build(geocoder_dir=self.run, temporal_path=self.run / 'temporal.csv', output_dir=self.root / 'out')

    def test_routes_prepare_without_sample_count_and_preserve_links(self):
        manifest = self.build()
        self.assertEqual(manifest['schema_version'], SCHEMA)
        self.assertEqual(manifest['temporal_child']['rows'], 4)
        out = self.root / 'out'
        policy = dict(schema_version='imputation_stage_a_policy_v1', request_budget=100, max_cells_per_observation=9, max_postcode_cells_per_observation=25, max_queue=25, max_attempts=1, retry_backoff_ms=0, query_timeout_seconds=10)
        requests, prepared = prepare(runner_path=out / 'runner.csv', routing_path=out / 'routing.csv', support_seed_manifest_path=out / 'seeds.json', final_evidence_manifest_path=out / 'handoff.json', grid_contract_path=Path(__file__).resolve().parents[1] / 'config/ghsl_grid.json', policy=policy)
        self.assertEqual({r['request_kind'] for r in requests}, {'observed_coordinate_map', 'osm_street_geometry', 'postcode_seed_cell'})
        self.assertEqual({r['request_kind']: r['max_cells'] for r in requests}, {'observed_coordinate_map': 9, 'osm_street_geometry': 9, 'postcode_seed_cell': 25})
        self.assertEqual(next(r for r in requests if r['request_kind']=='postcode_seed_cell')['postcode_boundary_mode'], 'polygon_first_1km')
        self.assertEqual(prepared['maximum_network_attempts'], 27)
        self.assertEqual(prepared['network_requests'], 0)
        self.assertEqual((out / 'temporal.csv').read_bytes(), (self.run / 'temporal.csv').read_bytes())
        with self.assertRaises(FileExistsError):
            self.build()

    def test_country_metadata_is_iso_not_old_regional_buckets(self):
        self.build()
        output = self.root / 'out'
        with (output / 'countries.csv').open() as handle:
            self.assertEqual({r['country_group'] for r in csv.DictReader(handle)}, {'CA'})

    def test_postcode_centroid_without_osm_object_is_a_valid_seed(self):
        self.candidates[2]['raw_candidate'].pop('osm_type')
        self.candidates[2]['raw_candidate'].pop('osm_id')
        self.build()
        with (self.root / 'out/routing.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[2]['route'], 'postcode_support_request')
        self.assertEqual(rows[2]['status'], 'accepted')

    def test_same_postcode_objects_with_different_coordinates_reach_postcode_lookup(self):
        extra = {**self.candidates[2], 'candidate_id': 'other-postcode', 'lat': 45.1}
        self.candidates.append(extra)
        self.selections[2]['selected_candidate_ids'].append('other-postcode')
        self.build()
        with (self.root / 'out/routing.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[2]['status'], 'accepted')
        self.assertEqual(rows[2]['route'], 'postcode_support_request')

    def test_conflicting_postcode_country_or_tl3_stays_withheld(self):
        import copy
        original = copy.deepcopy(self.candidates[2])
        for change in ({'postcode_candidate': 'DIFFERENT'}, {'postcode_match': False}, {'tl3_match': False}, {'raw_candidate': {'address': {'country_code': 'gb'}}}):
            with self.subTest(change=change):
                self.candidates[2] = {**original, **change}
                self.build()
                with (self.root / 'out/routing.csv').open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[2]['status'], 'withheld')
                self.assertEqual(rows[2]['withholding_reason'], 'postcode_seed_ambiguous_or_unconfirmed')
                import shutil
                shutil.rmtree(self.root / 'out')

    def test_first_address_coordinate_provenance_survives_handoff(self):
        extra = {**self.candidates[0], 'candidate_id': 'other-address', 'lat': 45.1,
                 'raw_candidate': dict(osm_type='way', osm_id=99, address=dict(country_code='ca'))}
        self.candidates.append(extra)
        self.selections[0].update(selected_candidate_ids=['c0', 'other-address'],
            selected_coordinate=[45.1, -75], coordinate_candidate_id='other-address',
            coordinate_selection_method='first_qualifying_same_address_result')
        self.build()
        with (self.root/'out/runner.csv').open() as handle:
            row=list(csv.DictReader(handle))[0]
        self.assertEqual(row['candidate_id'], 'other-address')
        self.assertEqual(float(row['observed_latitude']), 45.1)
        self.assertEqual(row['decision_status'], 'matched')
        self.assertEqual(self.selections[0]['selected_candidate_ids'], ['c0', 'other-address'])

    def test_confirmed_street_segments_reach_geometry_lookup(self):
        import copy
        extra = copy.deepcopy(self.candidates[1])
        extra.update(candidate_id='other-segment', raw_candidate=dict(osm_type='way', osm_id=99, address=dict(country_code='ca')))
        duplicate = {**extra, 'candidate_id': 'duplicate-query'}
        self.candidates.extend([extra, duplicate])
        self.selections[1]['selected_candidate_ids'].extend(['other-segment', 'duplicate-query'])
        self.build()
        out=self.root/'out'
        manifest=json.loads((out/'seeds.json').read_text())
        self.assertEqual(len(manifest['street_objects']['o1']), 2)
        with (out/'routing.csv').open() as handle:
            row=list(csv.DictReader(handle))[1]
        self.assertEqual(row['status'], 'accepted')
        policy=dict(schema_version='imputation_stage_a_policy_v1',request_budget=100,max_cells_per_observation=9,max_postcode_cells_per_observation=25,max_queue=25,max_attempts=1,retry_backoff_ms=0,query_timeout_seconds=10)
        requests,_=prepare(runner_path=out/'runner.csv',routing_path=out/'routing.csv',support_seed_manifest_path=out/'seeds.json',final_evidence_manifest_path=out/'handoff.json',grid_contract_path=Path(__file__).resolve().parents[1]/'config/ghsl_grid.json',policy=policy)
        request=next(r for r in requests if r['request_kind']=='osm_street_geometry')
        self.assertEqual(len(json.loads(request['osm_objects'])),2)

    def test_unconfirmed_street_object_is_withheld(self):
        extra = dict(self.candidates[1], candidate_id='other', road_match=False, raw_candidate=dict(osm_type='way', osm_id=99))
        self.candidates.append(extra)
        self.selections[1]['selected_candidate_ids'].append('other')
        self.build()
        with (self.root / 'out' / 'routing.csv').open() as handle:
            row = list(csv.DictReader(handle))[1]
        self.assertEqual(row['status'], 'withheld')
        self.assertEqual(row['withholding_reason'], 'street_objects_unconfirmed')

    def test_wrong_candidate_and_missing_year_are_explicit(self):
        self.selections[0]['selected_candidate_ids'] = ['c1']
        with self.assertRaisesRegex(ValueError, 'belong'):
            self.build()
        self.selections[0]['selected_candidate_ids'] = ['c0']
        self.temporal[0].update(patent_year='', patent_year_status='missing')
        self.build()

    def test_existing_backbone_identity_is_preserved(self):
        self.save()
        backbone = self.root / 'backbone.sqlite'
        with closing(sqlite3.connect(backbone)) as db, db:
            db.execute('CREATE TABLE regpat_family_backbone(appln_id TEXT PRIMARY KEY, canonical_family_id TEXT)')
            db.executemany('INSERT INTO regpat_family_backbone VALUES (?, ?)', [(r['appln_id'], r['family_id']) for r in self.temporal])
        manifest = build(geocoder_dir=self.run, family_backbone=backbone, output_dir=self.root / 'out')
        self.assertEqual(manifest['temporal_child']['rows'], 4)

    def test_missing_application_and_backbone_records_are_retained(self):
        self.temporal[0]['appln_id'] = ''
        self.save()
        backbone = self.root / 'backbone.sqlite'
        with closing(sqlite3.connect(backbone)) as db, db:
            db.execute('CREATE TABLE regpat_family_backbone(appln_id TEXT PRIMARY KEY, canonical_family_id TEXT)')
        manifest = build(geocoder_dir=self.run, family_backbone=backbone, output_dir=self.root / 'out')
        self.assertEqual(manifest['population_rows'], 4)
        self.assertEqual(manifest['source_link_rows'], 4)
        self.assertEqual(manifest['unlinked_source_rows']['rows'], 1)
        self.assertEqual(manifest['temporal_child']['rows'], 3)
        with (self.root / 'out' / 'temporal.csv').open() as handle:
            self.assertTrue(all((r['family_id'].startswith('unmatched:') for r in csv.DictReader(handle))))

    def test_disjoint_batches_keep_all_children_and_resume_only_unchanged_files(self):
        self.save()
        import copy
        extra = copy.deepcopy(self.temporal[0])
        extra.update(appln_id='second-application',family_id='docdb:second',patent_year='2024')
        self.temporal.append(extra)
        self.save()
        # One observation may have several recovered application/year links
        result=[]
        for start in (0,2):
            out=self.root/f'batch-{start}'
            kwargs=dict(geocoder_dir=self.run,temporal_path=self.run/'temporal.csv',output_dir=out,batch_start=start,batch_size=2,input_index=self.root/'handoff-index.sqlite')
            manifest=build(**kwargs)
            self.assertEqual(manifest['population_rows'],2)
            self.assertEqual(build(**kwargs,resume=True),manifest)
            with (out/'temporal.csv').open() as handle:result.extend(csv.DictReader(handle))
            self.assertEqual(manifest['batch']['start_row'],start)
            with self.assertRaisesRegex(ValueError,'inputs or options changed'):
                build(**{**kwargs,'batch_size':1},resume=True)
        self.assertEqual(sorted(result,key=lambda r:r['appln_id']),sorted(self.temporal,key=lambda r:r['appln_id']))
        (self.root/'batch-0/runner.csv').write_text('changed')
        with self.assertRaisesRegex(ValueError,'artifacts changed'):
            build(geocoder_dir=self.run,temporal_path=self.run/'temporal.csv',output_dir=self.root/'batch-0',batch_size=2,resume=True,input_index=self.root/'handoff-index.sqlite')

    def test_row_index_reopens_resumes_and_handles_multiline_utf8(self):
        from geographic_uncertainty.prepare_geographic_inputs import _RowIndex, _csv
        path=self.root/'multiline.csv'
        with path.open('w',newline='') as handle:
            writer=csv.DictWriter(handle,['observation_id','value']);writer.writeheader()
            writer.writerows([dict(observation_id='a',value='ü\nquoted,"value"'),dict(observation_id='b',value='second'),dict(observation_id='a',value='third')])
        index_path=self.root/'rows.sqlite'
        expected=_csv(path)
        with _RowIndex(index_path) as index:
            self.assertEqual(_csv(path,index=index),expected)
            self.assertEqual(_csv(path,index=index,observation_ids={'a'}),[expected[0],expected[2]])
            offset,length=index.db.execute('select offset,length from records where seq=1').fetchone()
            index.db.execute('delete from records where seq>1')
            index.db.execute('update files set complete=0,rows=1,offset=?',(offset+length,));index.db.commit()
        with _RowIndex(index_path) as index:
            self.assertEqual(_csv(path,index=index),expected)
            self.assertEqual(_csv(path,index=index,start=1,limit=1),[expected[1]])
        path.write_text('changed')
        with _RowIndex(index_path) as index:
            with self.assertRaisesRegex(ValueError,'source changed'):_csv(path,index=index)

    def test_bounded_id_queries_force_lookup_index_and_keep_source_order(self):
        from geographic_uncertainty.prepare_geographic_inputs import _RowIndex
        path=self.root/'evidence.jsonl'
        rows=[{'observation_id':f'obs-{i}','candidate_id':f'candidate-{i}'} for i in range(1200)]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        selected={r['observation_id'] for r in rows[::2]}
        with _RowIndex(self.root/'batched-index.sqlite') as index:
            index.read(path,jsonl=True)
            statements=[];index.db.set_trace_callback(statements.append)
            self.assertEqual(index.read(path,jsonl=True,observation_ids=selected),rows[::2])
            lookups=[sql for sql in statements if sql.startswith('SELECT offset,length')]
            self.assertEqual(len(lookups),2)
            self.assertTrue(all('INDEXED BY records_observation' in sql and ' IN (' in sql for sql in lookups))

    def test_row_index_reads_verified_local_copy_without_remote_data_reads(self):
        import shutil
        from unittest.mock import patch
        from geographic_uncertainty.prepare_geographic_inputs import _RowIndex, _csv
        source=self.root/'remote-evidence.csv'
        with source.open('w',newline='') as f:
            writer=csv.DictWriter(f,['observation_id','value']);writer.writeheader()
            writer.writerows([{'observation_id':'a','value':'ü\nquoted,"value"'},{'observation_id':'b','value':'second'}])
        expected=_csv(source);index_path=self.root/'local-index.sqlite'
        with _RowIndex(index_path) as index:self.assertEqual(_csv(source,index=index),expected)
        local=self.root/'ssd-evidence.csv';shutil.copy2(source,local)
        original_open=Path.open
        def local_only(path,*args,**kwargs):
            if path.resolve()==source.resolve():raise AssertionError('Remote evidence must not be read again')
            return original_open(path,*args,**kwargs)
        with _RowIndex(index_path,source_copies={str(source):str(local)}) as index:
            with patch.object(Path,'open',local_only):
                self.assertEqual(_csv(source,index=index),expected)
                self.assertEqual(_csv(source,index=index,observation_ids={'b'}),[expected[1]])
            self.assertEqual(index.db.execute('SELECT path FROM files').fetchone()[0],str(source.resolve()))
        local.write_text('changed')
        with _RowIndex(index_path,source_copies={str(source):str(local)}) as index:
            with self.assertRaisesRegex(ValueError,'copy identity'):_csv(source,index=index)

    def test_completed_acquisition_rejects_missing_external_cache_body(self):
        from geographic_uncertainty.retrieve_and_rasterise import _seal_completed, _verify_completed
        output=self.root/'output';output.mkdir()
        body=self.root/'cache-response.json';body.write_text('{}')
        with (output/'response_ledger.csv').open('w',newline='') as handle:
            writer=csv.DictWriter(handle,['response_path']);writer.writeheader();writer.writerow(dict(response_path=str(body)))
        _seal_completed(output,{'mode':'acquire'})
        _verify_completed(output,{'mode':'acquire'})
        body.unlink()
        with self.assertRaisesRegex(ValueError,'cached response bodies'):
            _verify_completed(output,{'mode':'acquire'})

if __name__ == '__main__':
    unittest.main()
