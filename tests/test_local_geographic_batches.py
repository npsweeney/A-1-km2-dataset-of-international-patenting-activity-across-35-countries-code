"""Batching/recovery checks use synthetic sources and recorded-function stand-ins, never a service"""
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace
import csv,json,sqlite3,tempfile,unittest,copy,os,subprocess,sys
import test_geographic_handoff as handoff_fixture
from grid_fixture import _grid
from geographic_uncertainty import batches,batch_io
from geographic_uncertainty import retrieve_and_rasterise as geo
from weighting import sample_built_surface as sample
from aggregation.aggregate_grid_activity import build as aggregate

class LocalGeographicBatches(unittest.TestCase):
    def test_publication_keeps_weight_bytes_and_relocates_only_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);working=p/'work';grid=working/'05_grid';grid.mkdir(parents=True)
            weights=grid/'estimated_grid_1km.csv'
            weights.write_text('observation_id,normalized_weight\n'+str(working)+'/opaque-id,1\n')
            original=weights.read_bytes()
            batch_io.save(grid/'final_grid_manifest.json',dict(artifacts={'estimated_grid_1km.csv':dict(path='estimated_grid_1km.csv',path_basis='manifest_relative')}))
            batch_io.save(working/'metadata.json',dict(path=str(weights)))
            destination=p/'published';batch_io.publish_batch(working,destination,[])
            self.assertEqual(weights.read_bytes(),original)
            self.assertEqual((destination/'05_grid/estimated_grid_1km.csv').read_bytes(),original)
            self.assertEqual(batch_io.load(destination/'metadata.json')['path'],str(destination/'05_grid/estimated_grid_1km.csv'))
            self.assertEqual(weights.stat().st_ino,(p/'.work.canonical-publication/05_grid/estimated_grid_1km.csv').stat().st_ino)
    def fixture(self,p):
        h=handoff_fixture.ReviewHandoffTest();h.setUp();self.addCleanup(h.doCleanups);h.root=p;h.run=p/'geocoder';h.run.mkdir()
        for row in h.observations:row['postal_code']='K1A 0B1'
        for row in h.candidates:row['postcode_candidate']='K1A 0B1'
        for collection in (h.observations,h.selections,h.candidates,h.queries,h.temporal):
            row=copy.deepcopy(collection[2]);row['observation_id']='o4'
            if 'candidate_id' in row:row['candidate_id']='c4'
            if 'selected_candidate_ids' in row:row['selected_candidate_ids']=['c4']
            if 'appln_id' in row:row.update(appln_id='4',family_id='docdb:4',location_evidence_id='o4')
            collection.append(row)
        h.save();links=h.run/'address_links.csv'
        with links.open() as f:reader=csv.DictReader(f);fields=reader.fieldnames+['inv_name'];rows=list(reader)
        with links.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({**r,'inv_name':'Person '+r['observation_id']} for r in rows)
        full=h.run/'full-roster.csv'
        with full.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({**r,'inv_name':'Person '+r['observation_id']} for r in rows);w.writerow({**rows[0],'observation_id':'outside-sample','inv_name':'Other person'})
        (h.run/'run_manifest.json').write_text(json.dumps(dict(partial=False,production_consumable=True,inputs=dict(full_source_links=str(full)))))
        backbone=p/'backbone.sqlite'
        with sqlite3.connect(backbone) as db:
            db.execute('create table regpat_family_backbone(appln_id text primary key,canonical_family_id text,resolution_status text,family_id_kind text,family_resolution_conflict integer)')
            db.executemany('insert into regpat_family_backbone values(?,?,?,?,?)',[(str(i),'docdb:'+str(i),'resolved','docdb',0) for i in range(5)])
        raster=p/'raster.tif';raster.write_text('synthetic fixed built surface')
        registry=dict(complete_temporal_registry=False,grid_identity=_grid(),published_epochs=list(range(1975,2035,5)),schema_version='gpd_ghsl_built_s_100m_registry_v1',product='GHS-BUILT-S',release='R2023A',measure='built_surface_m2',available_epochs=[2020])
        asset=dict(path=str(raster),artifact_role='ghsl_raster_2020');registry_path=p/'registry.json';registry_path.write_text(json.dumps(registry))
        service=dict(endpoint='http://unit.invalid',version='5.3.3',source_label='synthetic',database_system_identifier='synthetic-db',data_revision='unit-v1',max_queue=10000,max_attempts=1,retry_backoff_ms=0,request_budget=100000,client_workers=8,max_in_flight=8,delay_seconds=0,allocated_cpus=16,allocated_memory_gb=64,timeout_version='synthetic-bounded-v1')
        capacity=p/'capacity.json';capacity.write_text(json.dumps(dict(passed=True,selected_client_concurrency=8,delay_seconds=0,service_identity={k:service[k] for k in ('endpoint','version','database_system_identifier','data_revision')})));service['capacity_manifest']=str(capacity)
        service_path=p/'service.json';service_path.write_text(json.dumps(service))
        policy=p/'policy.json';policy.write_text(json.dumps(dict(schema_version='imputation_stage_a_policy_v1',request_budget=100000,max_cells_per_observation=5000,max_postcode_cells_per_observation=5000,max_queue=10000,max_attempts=1,retry_backoff_ms=0,query_timeout_seconds=10)))
        options=dict(geocoder_dir=h.run,output_dir=p/'out',family_backbone=backbone,batch_size=1,working_dir=p/'work',service_identity=service_path,policy=policy,grid_contract=Path(__file__).parents[1]/'config/ghsl_grid.json',ghsl_registry=registry_path,durable_cache=p/'durable')
        return options,registry,asset

    def mocks(self,registry,asset,calls):
        stack=ExitStack();original=geo.acquire;original_sample=sample.build_release
        # Tiny fixtures use explicit disk capacity rather than the test host's free space
        stack.enter_context(patch.object(batches.shutil,'disk_usage',return_value=SimpleNamespace(total=4*1024**3,used=1024**3,free=3*1024**3)))
        def getter(url,timeout):
            calls.append(url)
            if '/lookup?' in url:payload=[dict(osm_type='way',osm_id=2,geojson=dict(type='LineString',coordinates=[[-75,45],[-74.999,45.001]]))]
            else:payload=[dict(type='postcode',name='K1A 0B1',address=dict(country_code='ca',postcode='K1A 0B1'),geojson=dict(type='Polygon',coordinates=[[[-75.002,44.998],[-74.998,44.998],[-74.998,45.002],[-75.002,45.002],[-75.002,44.998]]]))]
            return 200,json.dumps(payload).encode()
        stack.enter_context(patch.object(geo,'acquire',side_effect=lambda requests,service,**kw:original(requests,service,getter=getter,**kw)))
        stack.enter_context(patch.object(sample,'_validate_registry',return_value=(registry,_grid(),{2020:asset})))
        stack.enter_context(patch.object(sample,'build_release',side_effect=lambda **kw:original_sample(value_reader=lambda path,cells:{cell:100 for cell in cells},**kw)))
        return stack

    def test_versioned_execution_budget_reuses_completed_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);calls=[];stop=p/'stop'
            original_mirror=batch_io.mirror_cache;checkpoints=[]
            def pause(*args,**kwargs):
                value=original_mirror(*args,**kwargs);checkpoints.append(value)
                if len(checkpoints)==3:stop.touch()
                return value
            with self.mocks(registry,asset,calls),patch.object(batch_io,'mirror_cache',side_effect=pause):
                batches.run_batches(**options,stop_file=stop)
            old=p/'out/batches/batch-00000000'
            original=(old/'05_grid/estimated_grid_1km.csv').read_bytes()
            policy=p/'higher-budget.json';settings=json.loads(options['policy'].read_text())
            settings['request_budget']*=2;policy.write_text(json.dumps(settings))
            new={**options,'output_dir':p/'continued','working_dir':p/'continued-work',
                 'durable_cache':p/'continued-cache','policy':policy}
            new['durable_cache'].mkdir()
            batch_io.copy_cache(options['durable_cache'],new['durable_cache'])
            with self.mocks(registry,asset,calls):
                self.assertIsNone(batches.run_batches(**new,stop_file=stop))
            destination=new['output_dir']/'batches';destination.mkdir()
            (destination/old.name).symlink_to(old,target_is_directory=True)
            stop.unlink()
            with self.mocks(registry,asset,calls):result=batches.run_batches(**new,resume=True)
            self.assertTrue(result['verified']);self.assertEqual(result['observations'],5)
            self.assertEqual(original,(old/'05_grid/estimated_grid_1km.csv').read_bytes())
            self.assertTrue((destination/old.name).is_symlink())

    def test_pause_resume_cache_and_global_family_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);calls=[];stop=p/'stop';original_mirror=batch_io.mirror_cache
            checkpoints=[]
            def mirror(*args,**kw):
                result=original_mirror(*args,**kw);checkpoints.append(result)
                if len(checkpoints)==3:stop.touch()
                return result
            with self.mocks(registry,asset,calls),patch.object(batch_io,'mirror_cache',side_effect=mirror):
                self.assertIsNone(batches.run_batches(**options,stop_file=stop))
            self.assertFalse((p/'out/05_grid/final_grid_manifest.json').exists());self.assertEqual(len(list((p/'out/batches').glob('*/complete.json'))),1)
            first=(p/'out/batches/batch-00000000/05_grid/estimated_grid_1km.csv').read_bytes();stop.unlink()
            with self.mocks(registry,asset,calls):result=batches.run_batches(**options,resume=True)
            self.assertTrue(result['verified']);self.assertEqual(result['observations'],5);self.assertEqual(result['source_links'],5)
            merged=json.loads((p/'out/05_sampled/support_release.json').read_text())
            self.assertEqual(len(merged['batch_evidence']),5)
            for entry in merged['batch_evidence']:
                for path in entry.values():self.assertTrue(Path(path).is_file())
            self.assertEqual(first,(p/'out/batches/batch-00000000/05_grid/estimated_grid_1km.csv').read_bytes())
            before=len(calls)
            with self.mocks(registry,asset,calls):batches.run_batches(**options,resume=True)
            self.assertEqual(len(calls),before)
            # Compare one batch containing the entire cohort to five resumed batches
            other={**options,'output_dir':p/'single','working_dir':p/'single-work','durable_cache':p/'single-cache','batch_size':5}
            with self.mocks(registry,asset,[]):batches.run_batches(**other)
            # The original unbatched CLI path is an independent reference
            import importlib
            from run import STAGES
            legacy=p/'legacy';legacy.mkdir()
            legacy_service=p/'legacy-service.json';sv=json.loads(options['service_identity'].read_text());sv['response_cache_dir']=str(p/'legacy-cache');legacy_service.write_text(json.dumps(sv))
            def command(stage,*args):importlib.import_module(STAGES[stage]).main(list(map(str,args)))
            with self.mocks(registry,asset,[]):
                command('handoff','--geocoder-dir',options['geocoder_dir'],'--family-backbone',options['family_backbone'],'--output-dir',legacy/'04_inputs')
                common=['--runner',legacy/'04_inputs/runner.csv','--routing',legacy/'04_inputs/routing.csv','--final-evidence',legacy/'04_inputs/handoff.json','--grid-contract',options['grid_contract']]
                command('geographies','prepare',*common,'--support-seed-manifest',legacy/'04_inputs/seeds.json','--policy',options['policy'],'--output-dir',legacy/'04_requests')
                command('geographies','acquire','--requests',legacy/'04_requests/support_requests.csv','--request-manifest',legacy/'04_requests/stage_a_request_manifest.json','--service-identity',legacy_service,'--output-dir',legacy/'04_responses')
                command('geographies','finalize',*common,'--requests',legacy/'04_requests/support_requests.csv','--request-manifest',legacy/'04_requests/stage_a_request_manifest.json','--responses',legacy/'04_responses/response_ledger.csv','--response-manifest',legacy/'04_responses/response_ledger_manifest.json','--service-identity',legacy_service,'--cross-stage-release',legacy/'04_inputs/handoff.json','--output-dir',legacy/'04_support')
                command('sample-ghsl','--cross-stage-release',legacy/'04_inputs/handoff.json','--ghsl-registry',options['ghsl_registry'],'--grid-contract',options['grid_contract'],'--support-inputs',legacy/'04_support/support_inputs.json','--output-dir',legacy/'05_sampled')
                command('allocate','--cross-stage-release',legacy/'04_inputs/handoff.json','--support-release',legacy/'05_sampled/support_release.json','--output-dir',legacy/'05_grid')
            # Reweight the merged saved release without any service or raster acquisition
            from weighting.allocate_to_grid import run as reweight
            reweight(cross_stage_release_path=p/'out/04_inputs/handoff.json',support_release_path=p/'out/05_sampled/support_release.json',output_dir=p/'reweighted')
            aggregate(allocation_manifest=p/'reweighted/final_grid_manifest.json',family_backbone=options['family_backbone'],output_dir=p/'reweighted-activity')
            for folder in ('out','single','legacy'):
                aggregate(allocation_manifest=p/folder/'05_grid/final_grid_manifest.json',family_backbone=options['family_backbone'],output_dir=p/folder/'06_activity')
            for f in (p/'out/06_activity').glob('*.csv'):
                for folder in ('single','legacy'):self.assertEqual(f.read_bytes(),(p/folder/'06_activity'/f.name).read_bytes(),f.name)
            for f in (p/'out/06_activity').glob('*.csv'):
                self.assertEqual(f.read_bytes(),(p/'reweighted-activity'/f.name).read_bytes(),f.name)
            invalid=json.loads((p/'out/05_sampled/support_release.json').read_text());invalid['sealed']=False
            (p/'invalid-support.json').write_text(json.dumps(invalid))
            with self.assertRaisesRegex(ValueError,'identity is invalid'):
                reweight(cross_stage_release_path=p/'out/04_inputs/handoff.json',support_release_path=p/'invalid-support.json',output_dir=p/'invalid-reweight')
            self.assertFalse((p/'invalid-reweight').exists())
            rerun_manifest=json.loads((p/'reweighted/final_grid_manifest.json').read_text())
            self.assertEqual(rerun_manifest['resource_estimate']['network_requests'],0)
            rerun_audit=json.loads((p/'reweighted/audit.json').read_text())
            self.assertTrue(all(Path(path).is_file() for path in rerun_audit['batch_audits']))
            with (p/'out/06_activity/withheld_families.csv').open() as f:self.assertTrue(any(r['reason']=='outside_allocated_cohort_or_unrepresented_person' and r['withheld_weight']=='0.5' for r in csv.DictReader(f)))
            counts=[json.loads(f.read_text()) for f in (p/'out/batches').glob('*/04_responses/response_ledger_manifest.json')]
            self.assertGreater(sum(x.get('cache_hits',0) for x in counts),0)
            self.assertEqual(json.loads((p/'out/04_inputs/handoff.json').read_text())['source_role_links']['path'],str((options['geocoder_dir']/'full-roster.csv').resolve()))

    def test_interrupted_acquisition_reuses_saved_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);calls=[]
            with self.mocks(registry,asset,calls):
                normal=geo.acquire
                def interrupt(requests,service,**kw):
                    result=normal(requests,service,**kw)
                    if any(r['request_kind']=='postcode_seed_cell' for r in requests):raise RuntimeError('controlled interruption')
                    return result
                with patch.object(geo,'acquire',side_effect=interrupt):
                    with self.assertRaisesRegex(RuntimeError,'controlled interruption'):batches.run_batches(**options)
            self.assertEqual(len(list((p/'out/batches').glob('*/complete.json'))),2)
            before=len(calls)
            with self.mocks(registry,asset,calls):result=batches.run_batches(**options,resume=True)
            self.assertTrue(result['verified']);self.assertEqual(len(calls),before)
            self.assertGreater(json.loads((p/'out/geographic-batches-state.json').read_text())['unobserved_attempts_reserved'],0)

    def test_existing_handoff_cli_pauses_without_a_final_marker(self):
        from geographic_uncertainty.prepare_geographic_inputs import main
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);stop=p/'stop';stop.touch()
            args=['--run-batches','--stop-file',str(stop)]
            for key,value in options.items():args.extend(['--'+key.replace('_','-'),str(value)])
            with self.mocks(registry,asset,[]):self.assertEqual(main(args),75)
            self.assertFalse((p/'out/geographic-batches-complete.json').exists())

    def test_pilot_or_unapproved_capacity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);file=options['service_identity'];service=json.loads(file.read_text());service['allocated_cpus']=1;file.write_text(json.dumps(service))
            with self.assertRaisesRegex(ValueError,'16 CPUs'):batches.run_batches(**options)

    def test_changed_settings_are_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);stop=p/'stop';stop.touch()
            with self.mocks(registry,asset,[]):batches.run_batches(**options,stop_file=stop)
            with self.assertRaisesRegex(ValueError,'inputs, settings or code changed'):
                batches.run_batches(**{**options,'batch_size':2},resume=True)


    def test_publication_overlaps_next_batch_without_early_commit(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p)
            started=threading.Event();release=threading.Event()
            original_publish=batch_io.publish_batch;original_build=batches.io.load
            from geographic_uncertainty import prepare_geographic_inputs as handoff
            build=handoff._build
            def publish(working,*args,**kwargs):
                if working.name=='batch-00000000':
                    started.set()
                    if not release.wait(10):raise RuntimeError('Next calculation did not overlap publication')
                return original_publish(working,*args,**kwargs)
            def prepare(**kwargs):
                if kwargs['batch_start']==1:
                    self.assertTrue(started.wait(10))
                    self.assertFalse((p/'out/batches/batch-00000000/complete.json').exists())
                    self.assertEqual(original_build(p/'out/geographic-batches-state.json')['completed_addresses'],0)
                    release.set()
                return build(**kwargs)
            with self.mocks(registry,asset,[]),patch.object(batches,'ProcessPoolExecutor',side_effect=lambda **kw:ThreadPoolExecutor(max_workers=kw['max_workers'])),patch.object(batch_io,'publish_batch',side_effect=publish),patch.object(handoff,'_build',side_effect=prepare):
                result=batches.run_batches(**options)
            self.assertTrue(result['verified']);self.assertTrue(release.is_set())
            self.assertEqual(len(list((p/'out/batches').glob('*/complete.json'))),5)

    def test_failed_publication_keeps_inputs_without_committing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);working=p/'working';working.mkdir();source=working/'source_links.csv';source.write_text('observation_id\no1\n');destination=p/'durable'
            def fail(*args):
                destination.mkdir();(destination/'partial').write_text('unfinished copy')
                raise RuntimeError('verification failed')
            with patch.object(batch_io,'publish_batch',side_effect=fail):
                with self.assertRaisesRegex(RuntimeError,'verification failed'):
                    batches._publish_batch(working,destination,[],dict(start=0,observations=1))
            self.assertTrue(source.exists());self.assertFalse((destination/'complete.json').exists())

    def test_checkpoint_connection_is_reused_and_closed(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p)
            connections=[];original=batches.io.mirror_cache
            def observe(*args,**kwargs):
                if kwargs.get('connection') is not None:connections.append(kwargs['connection'])
                return original(*args,**kwargs)
            with self.mocks(registry,asset,[]),patch.object(batches.io,'mirror_cache',side_effect=observe):
                result=batches.run_batches(**options)
            self.assertTrue(result['verified'])
            self.assertGreater(len(connections),2)
            self.assertTrue(all(c is connections[0] for c in connections))
            with self.assertRaises(sqlite3.ProgrammingError):connections[0].execute('select 1')

    def test_low_disk_space_stops_before_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,registry,asset=self.fixture(p);calls=[]
            with self.mocks(registry,asset,calls),patch.object(batches.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(ValueError,'disk headroom'):
                    batches.run_batches(**options)
            self.assertEqual(calls,[])
            self.assertFalse(list((p/'out/batches').glob('*/complete.json')))

    def test_runner_checks_capacity_before_geocoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);options,_,_=self.fixture(p)
            capacity=p/'capacity.json';record=json.loads(capacity.read_text());record['passed']=False;capacity.write_text(json.dumps(record))
            binary=p/'bin';binary.mkdir();called=p/'geocoder-called'
            command=binary/'patent-code';command.write_text('#!/bin/sh\ntouch "'+str(called)+'"\n');command.chmod(0o755)
            env=dict(os.environ,PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],RUN_OUTPUT=str(p/'run'),BOUNDARIES='unused',REGION_PREFLIGHT='unused',WORKERS='8',MAX_IN_FLIGHT='8',REQUEST_BUDGET='1000',GEOGRAPHIC_POLICY=str(options['policy']),GEOGRAPHIC_SERVICE=str(options['service_identity']))
            result=subprocess.run(['bash',str(Path(__file__).parents[1]/'scripts/run_pipeline.sh'),'--from-stage','3'],env=env,text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('passed capacity gate',result.stderr);self.assertFalse(called.exists())

if __name__=='__main__':unittest.main()
