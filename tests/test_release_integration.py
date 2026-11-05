"""Entry-point, resume and archive contracts; no external services or private data"""
import csv,datetime,importlib.util,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pipeline_stage',ROOT/'scripts/pipeline_stage.py');stage=importlib.util.module_from_spec(spec);spec.loader.exec_module(stage)
from ingest.patent_metadata.resolve_ops_families import initialize,iter_saved_application_dates

class PipelineResume(unittest.TestCase):
 def test_unchanged_step_reuses_but_changed_input_or_option_refuses(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);source=p/'input.csv';source.write_text('one');marker=p/'result.json';receipt=p/'receipt.json'
   command=[sys.executable,'-c',"pass",'--input',str(source),'--output',str(marker),'--policy','one']
   def write(*args,**kw):marker.write_text(json.dumps({'artifacts':{'rows':{'path':'rows.csv'}}}));(p/'rows.csv').write_text('one')
   with patch.object(stage.subprocess,'run',side_effect=write) as called:
    stage.execute(command,marker,receipt,reuse=False);stage.execute(command,marker,receipt,reuse=True);self.assertEqual(called.call_count,1)
    with self.assertRaisesRegex(ValueError,'changed'):stage.execute([*command[:-1],'two'],marker,receipt,reuse=True)
    (p/'rows.csv').unlink()
    with self.assertRaisesRegex(ValueError,'changed'):stage.execute(command,marker,receipt,reuse=True)
    (p/'rows.csv').write_text('one')
    source.write_text('changed')
    with self.assertRaisesRegex(ValueError,'changed'):stage.execute(command,marker,receipt,reuse=True)
 def test_missing_output_and_unknown_old_completion_are_not_silently_skipped(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);marker=p/'done.json';marker.write_text('{}')
   with self.assertRaisesRegex(ValueError,'receipt'):stage.execute(['no-call'],marker,p/'missing',reuse=True)
 def test_partial_geocoding_resumes_only_with_unchanged_pending_invocation(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);source=p/'input.csv';source.write_text('saved addresses')
   marker=p/'run_manifest.json';receipt=p/'receipt.json'
   command=['patent-code','geocode','run','--input',str(source),'--output',str(p/'output')]
   def partial(*args,**kwargs):marker.write_text(json.dumps(dict(partial=True,production_consumable=False)))
   with patch.object(stage.subprocess,'run',side_effect=partial):
    with self.assertRaisesRegex(ValueError,'Incomplete geocoding'):stage.execute(command,marker,receipt,reuse=False)
   self.assertFalse(receipt.exists());self.assertTrue(receipt.with_suffix('.pending.json').exists())
   with patch.object(stage.subprocess,'run') as called:
    with self.assertRaisesRegex(ValueError,'changed'):stage.execute(command+['--workers','4'],marker,receipt,reuse=True)
    called.assert_not_called()
   def complete(*args,**kwargs):marker.write_text(json.dumps(dict(partial=False,production_consumable=True)))
   with patch.object(stage.subprocess,'run',side_effect=complete) as called:
    stage.execute(command,marker,receipt,reuse=True);stage.execute(command,marker,receipt,reuse=True)
    self.assertEqual(called.call_count,1)
   self.assertTrue(receipt.exists());self.assertFalse(receipt.with_suffix('.pending.json').exists())

 def test_stage6_does_not_require_regpat_or_an_endpoint(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'bin').mkdir();fake=p/'bin/patent-code';fake.write_text('#!/bin/sh\necho CALLED_AGGREGATION\nexit 9\n');fake.chmod(0o755)
   result=subprocess.run(['bash',str(ROOT/'scripts/run_pipeline.sh'),'--from-stage','6'],env={'PATH':str(p/'bin')+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+'/usr/bin:/bin','RUN_OUTPUT':str(p/'run')},capture_output=True,text=True)
   self.assertIn('CALLED_AGGREGATION',result.stdout);self.assertNotIn('REGPAT_RELEASE',result.stderr)

 def test_non_batched_postcode_sizes_reach_figures_and_resume_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);run=p/'run';binary=p/'bin';binary.mkdir()
   for name in ('04_inputs','04_support'):(run/name).mkdir(parents=True)
   (run/'04_inputs/handoff.json').write_text('{}')
   (run/'04_support/support_inputs.json').write_text('{}')
   sizes=run/'04_support/postcode_sizes.csv'
   sizes.write_text('country_code,allocated_area_km2\nGB,1.0\n')
   before=sizes.read_bytes();source_stat=sizes.stat()
   config=p/'figures.json';config.write_text(json.dumps({'postcode_sizes':{'source':str(run/'04_inputs/postcode_sizes.csv')}}))
   fake=binary/'patent-code'
   fake.write_text('#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
args=sys.argv[1:];command=args[0]
if command=='figures-tables':
 config=json.loads(Path(args[args.index('--config')+1]).read_text())
 assert Path(config['postcode_sizes']['source']).read_text()=='country_code,allocated_area_km2\\nGB,1.0\\n'
if '--output-dir' in args:
 folder=Path(args[args.index('--output-dir')+1]);folder.mkdir(parents=True)
 name={'sample-ghsl':'support_release.json','allocate':'final_grid_manifest.json'}.get(command,'manifest.json')
 target=folder/name
else:target=Path(args[args.index('--output')+1])
target.write_text('{}')
''');fake.chmod(0o755)
   env=dict(PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+'/usr/bin:/bin',PYTHONPATH=os.environ.get('PYTHONPATH',''),RUN_OUTPUT=str(run),GHSL_SOURCE_MANIFEST=str(p/'assets.json'),FIGURES_CONFIG=str(config))
   command=['bash',str(ROOT/'scripts/run_pipeline.sh'),'--from-stage','5']
   for _ in range(2):
    result=subprocess.run(command,env=env,capture_output=True,text=True)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertEqual((run/'04_inputs/postcode_sizes.csv').read_bytes(),before)
   self.assertEqual(sizes.read_bytes(),before)
   self.assertEqual(sizes.stat().st_mtime_ns,source_stat.st_mtime_ns)
   self.assertTrue((run/'08_figures_tables/manifest.json').is_file())

class MetadataRefresh(unittest.TestCase):
 def fixture(self,p):
  import duckdb
  (p/'old').mkdir();(p/'old/release.json').write_text(json.dumps(dict(validated=True,release_id='before')))
  master=p/'old/regpat2026-application-master.parquet'
  with duckdb.connect() as d:
   d.execute("CREATE TABLE m AS SELECT * FROM (VALUES ('1',NULL::DATE,NULL::INTEGER,false,'missing',['base'],'A'),('2',DATE '2010-01-01',2010,false,'valid',['base'],'B'),('3',NULL::DATE,NULL::INTEGER,false,'missing',['base'],'C')) AS t(appln_id,filing_date,filing_year,date_conflict,date_status,provenance,classification)")
   d.execute('COPY m TO ? (FORMAT PARQUET)',[str(master)])
  with duckdb.connect(str(p/'source.duckdb')) as d:
   d.execute('CREATE TABLE source(appln_id VARCHAR,source_row_id VARCHAR,person_id VARCHAR,actor VARCHAR,office VARCHAR,application_number VARCHAR,publication_number VARCHAR,address VARCHAR,city VARCHAR,postcode VARCHAR,region VARCHAR,country VARCHAR)')
   d.executemany('INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(aid,str(n),str(n),'inventor','EPO',aid,aid,'Example Road '+aid,'Example','AB1','UK1','GB') for n,aid in enumerate(('1','1','2','3'),1)])
  queue=p/'queue.csv';queue.write_text('appln_id\n')
  with sqlite3.connect(p/'ops.sqlite') as db:
   initialize(db,queue)
   db.executemany('INSERT INTO application VALUES (?,?,?,?,?,?,?,?)',[(a,'EPO_APP',a,a,'EP.'+a,'',None,None) for a in ('1','2','3','99')])
   db.executemany('INSERT INTO application_date_evidence VALUES (?,?,?,?,?,?,?)',[(ref,country,aid,'epodoc',date,str(n),'<source/>') for n,(ref,country,aid,date) in enumerate((('EP.1','EP','1','20090102'),('EP.1','EP','1','20090102'),('EP.2','EP','2','20120101'),('EP.3','EP','wrong-number','20080101'),('EP.3','WO','3','20080101'),('EP.99','EP','99','20090101')))])
  return master
 def test_public_ops_to_validated_master_preserves_ids_conflicts_and_links(self):
  import duckdb
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);master=self.fixture(p);before=(p/'ops.sqlite').read_bytes()
   cmd=[sys.executable,str(ROOT/'src/01_ingest/patent_metadata/05_refresh_saved_ops.py'),'--master',str(master),'--ops-cache',str(p/'ops.sqlite'),'--source-db',str(p/'source.duckdb'),'--output',str(p/'new'),'--release-id','after']
   result=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)
   release=json.loads((p/'new/release.json').read_text());self.assertTrue(release['validated']);self.assertEqual(release['release_id'],'after')
   with duckdb.connect() as d:
    records=d.execute('SELECT appln_id,filing_date,date_conflict,classification FROM read_parquet(?) ORDER BY appln_id',[str(p/'new/regpat2026-application-master.parquet')]).fetchall()
   self.assertEqual(records,[('1',datetime.date(2009,1,2),False,'A'),('2',None,True,'B'),('3',None,False,'C')])
   self.assertEqual(release['counts']['unique_address_inputs'],1);self.assertEqual(release['counts']['eligible_source_rows'],2)
   with (p/'new/geocoding-input-links.csv').open() as f:self.assertEqual(len(list(csv.DictReader(f))),2)
   self.assertEqual((p/'ops.sqlite').read_bytes(),before)
   again=subprocess.run(cmd+['--resume-export'],capture_output=True,text=True);self.assertEqual(again.returncode,0,again.stderr)
 def test_payload_cache_needs_no_private_dated_marker(self):
  with sqlite3.connect(':memory:') as db:
   db.execute('CREATE TABLE application(appln_id TEXT,publication_key TEXT,payload TEXT)');db.execute('INSERT INTO application VALUES (?,?,?)',('1','EP.1',json.dumps({'filing_dates':['2009-01-02','2010-01-02']})))
   self.assertEqual(list(iter_saved_application_dates(db)),[('1','EP.1','2009-01-02'),('1','EP.1','2010-01-02')])
 def test_canonical_master_without_release_is_rejected(self):
  import test_intake_cohort
  # The production gate is also explicit in the direct intake implementation
  from ingest.import_regpat import prepare_cohort
  import importlib.util
  spec=importlib.util.spec_from_file_location('demo_patents',ROOT/'tests/fixtures/patent_records.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);module.create(p/'inputs');master=p/'inputs/patents/application-master.parquet'
   # Supply a real fixture master if the example's generated layout differs
   masters=list((p/'inputs').rglob('*master.parquet'));self.assertTrue(masters);master=masters[0];(master.parent/'release.json').unlink()
   with self.assertRaisesRegex(ValueError,'release.json'):prepare_cohort(p/'inputs/regpat',p/'out.csv',patstat_db=p/'inputs/patstat.sqlite',application_master=master)


class BatchReceiptRegression(unittest.TestCase):
 def test_resume_ignores_operational_flag_and_mutable_work_but_checks_policy(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);work=p/'work';durable=p/'cache';policy=p/'policy.json';policy.write_text('{"limit":1}')
   marker=p/'complete.json';receipt=p/'receipt.json'
   command=['patent-code','handoff','--working-dir',str(work),'--durable-cache',str(durable),'--policy',str(policy),'--output-dir',str(p/'out')]
   def complete(*args,**kw):
    work.mkdir();durable.mkdir();(work/'service.json').write_text('{}');(durable/'mirror-state.json').write_text('{}');marker.write_text('{}')
   with patch.object(stage.subprocess,'run',side_effect=complete) as called:
    stage.execute(command,marker,receipt,reuse=False)
    stage.execute(command+['--resume'],marker,receipt,reuse=True)
    self.assertEqual(called.call_count,1)
    policy.write_text('{"limit":2}')
    with self.assertRaisesRegex(ValueError,'changed'):stage.execute(command+['--resume'],marker,receipt,reuse=True)

class ForwardCacheIdentity(unittest.TestCase):
 def test_database_identity_is_required_for_reuse_without_rehashing_responses(self):
  from geocoding.geocoder.client import NominatimClient
  with tempfile.TemporaryDirectory() as tmp:
   identity=dict(database_system_identifier='fixture',data_revision='one',version='5.3.3')
   def client(data):return NominatimClient('http://localhost:8080',Path(tmp),'test',data_identity=data)
   first=client(identity);first._store_cached('query','saved-url',[{'lat':'1'}]);first._cache_connection.close()
   same=client(identity);self.assertEqual(same._get_cached('query')[0],[{'lat':'1'}]);same._cache_connection.close()
   for data in (None,{**identity,'data_revision':'two'}):
    with self.assertRaisesRegex(ValueError,'database identity'):client(data)

 def test_unidentified_existing_cache_is_preserved(self):
  from geocoding.geocoder.client import NominatimClient
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);first=NominatimClient('http://localhost:8080',root,'test')
   first._store_cached('query','saved-url',[{'lat':'1'}]);first._cache_connection.close()
   with self.assertRaisesRegex(ValueError,'no verified database identity'):
    NominatimClient('http://localhost:8080',root,'test',data_identity={'data_revision':'one'})
   with sqlite3.connect(root/'nominatim-cache.sqlite3') as db:
    self.assertEqual(db.execute('SELECT count(*) FROM responses').fetchone()[0],1)

if __name__ == '__main__':
    unittest.main()
