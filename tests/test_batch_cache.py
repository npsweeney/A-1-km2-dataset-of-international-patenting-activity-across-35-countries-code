"""Copied cache entries and weights remain exact; conflicts fail before a saved receipt"""
import unittest,json,sqlite3,tempfile,csv,filecmp
from pathlib import Path
from geographic_uncertainty import batch_io as io

class BatchCache(unittest.TestCase):
    def test_append_only_checkpoint_and_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
         p=Path(tmp);old=p/'original';old.mkdir();name='nominatim-cache.sqlite3'
         with sqlite3.connect(old/name) as c:
          c.execute('create table responses(cache_key text primary key,url text,payload text,created_at_utc text)');c.execute('create table geographic_bodies(file_id integer primary key,cache_key text unique)');c.execute('insert into responses values(?,?,?,?)',('one','http://owned/one','[{"body":"{}"}]','original-time'));c.execute('insert into geographic_bodies values(1,?)',('one',))
         before=(old/name).read_bytes();local=p/'ssd';io.copy_cache(old,local);durable=p/'durable'
         for n in [2,3]:
          key=str(n);body=json.dumps({'value':n})
          with sqlite3.connect(local/name) as c:c.execute('insert into responses values(?,?,?,?)',(key,'http://owned/'+key,json.dumps([{'body':body}]),'time-'+key));c.execute('insert into geographic_bodies values(?,?)',(n,key))
          raw=local/'raw/0'/f'response-{n}.json';raw.parent.mkdir(parents=True,exist_ok=True);raw.write_text(body);ledger=p/'ledger.csv'
          with ledger.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=['response_path']);w.writeheader();w.writerow(dict(response_path=str(raw)))
          io.mirror_cache(local,durable,response_ledger=ledger)
         with sqlite3.connect(local/name) as a,sqlite3.connect(durable/name) as b:
          for table in ['responses','geographic_bodies']:assert a.execute('select * from '+table+' order by 1').fetchall()==b.execute('select * from '+table+' order by 1').fetchall()
         for n in [2,3]:assert filecmp.cmp(local/'raw/0'/f'response-{n}.json',durable/'raw/0'/f'response-{n}.json',shallow=False)
         assert (old/name).read_bytes()==before
         print('PASS: incremental cache mirror preserves rows, URLs, payloads, timestamps, body IDs and original cache bytes')
         working=p/'working'
         for folder in ['03','04_inputs','05_grid','05_sampled']:(working/folder).mkdir(parents=True)
         (working/'03/input.csv').write_bytes(b'observation_id,value\r\na,1\r\n');(working/'04_inputs/handoff.json').write_text(json.dumps({'source':str(working/'03/input.csv')}));(working/'05_grid/weights.csv').write_bytes(b'observation_id,normalized_weight\r\na,0.75\r\nb,0.25\r\n');(working/'05_sampled/ghsl_values.json').write_text('{"values":{"a":"3","b":"1"}}')
         from geographic_uncertainty.retrieve_and_rasterise import _seal_completed,_verify_completed,_file_identity
         _seal_completed(working/'04_inputs',dict(input=_file_identity(working/'03/input.csv')))
         target=p/'published';io.publish_batch(working,target,[(local,durable)])
         assert filecmp.cmp(working/'05_grid/weights.csv',target/'05_grid/weights.csv',shallow=False);assert io.load(target/'04_inputs/handoff.json')['source']==str(target/'03/input.csv');_verify_completed(target/'04_inputs',io.load(target/'04_inputs/stage_complete.json')['invocation'])
         print('PASS: publication preserves CSV bytes/epoch values, relocates paths and reseals valid durable receipts')
    def test_corrupt_unsealed_body_preserves_original_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
         root=Path(tmp);local=root/'local';local.mkdir();durable=root/'durable';name='nominatim-cache.sqlite3'
         with sqlite3.connect(local/name) as db:
          db.execute('create table responses(cache_key text primary key,url text,payload text,created_at_utc text)');db.execute('create table geographic_bodies(file_id integer primary key,cache_key text unique)');db.execute('insert into responses values(?,?,?,?)',('one','saved-url','saved-payload','saved-time'));db.execute('insert into geographic_bodies values(1,?)',('one',))
         io.mirror_cache(local,durable);before=(durable/'mirror-state.json').read_bytes();source=local/'raw/0/response-1.json';source.parent.mkdir(parents=True);source.write_text('original body');bad=durable/'raw/0/response-1.json';bad.parent.mkdir(parents=True);bad.write_text('conflicting body')
         ledger=root/'ledger.csv'
         with ledger.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=['response_path']);w.writeheader();w.writerow(dict(response_path=str(source)))
         try:io.mirror_cache(local,durable,response_ledger=ledger)
         except ValueError:pass
         else:raise AssertionError('conflict was not rejected')
         assert source.read_text()=='original body' and bad.read_text()=='conflicting body';assert (durable/'mirror-state.json').read_bytes()==before
         with sqlite3.connect(durable/name) as db:assert db.execute('select count(*) from mirrored_files').fetchone()==(0,)
         print('PASS: conflicting unsealed body rejected; original files and checkpoint preserved; no false durable receipt')


class GridArchive(unittest.TestCase):
    def fixture(self, p):
        import gzip
        data = b'observation_id,normalized_weight\n' + b'o,0.1234567890123456789012345678\n' * 1000
        paths = {}
        for name in (*io.ARCHIVAL_GRID_FILES, 'estimated_grid_1km.csv'):
            (p / name).write_bytes(data)
            paths[name] = dict(path=name, path_basis='manifest_relative', rows=1000)
        io.save(p / 'final_grid_manifest.json', dict(artifacts=paths, resource_estimate=dict(output_bytes=4*len(data))))
        return data

    def test_lossless_archive_resume_and_main_input_unchanged(self):
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);data=self.fixture(p)
            receipt=io.compress_archival_outputs(p, threshold_bytes=0)
            self.assertEqual(len(receipt['files']),3)
            for name in io.ARCHIVAL_GRID_FILES:
                self.assertFalse((p/name).exists())
                with gzip.open(p/(name+'.gz'),'rb') as handle:self.assertEqual(handle.read(),data)
            self.assertEqual((p/'estimated_grid_1km.csv').read_bytes(),data)
            old=(p/'final_grid_manifest.json').read_bytes()
            io.compress_archival_outputs(p,threshold_bytes=0)
            self.assertEqual((p/'final_grid_manifest.json').read_bytes(),old)
            # A crash after receipt/manifest commit but before raw retirement resumes
            name=io.ARCHIVAL_GRID_FILES[0];(p/name).write_bytes(data)
            io.compress_archival_outputs(p,threshold_bytes=0)
            self.assertFalse((p/name).exists())

    def test_publication_compresses_before_copy_and_reseals_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);working=p/'working';grid=working/'05_grid';grid.mkdir(parents=True)
            data=self.fixture(grid)
            # Exercise the normal one-MiB production threshold
            for name in io.ARCHIVAL_GRID_FILES:(grid/name).write_bytes(data*40)
            target=p/'published';io.publish_batch(working,target,[])
            receipt=io.load(target/'05_grid/storage-receipt.json')
            for name in io.ARCHIVAL_GRID_FILES:
                stored=target/'05_grid'/(name+'.gz')
                self.assertTrue(io.decoded_equal(grid/name,stored))
                self.assertEqual(receipt['files'][name]['stored_identity'],io.identity(stored))
            self.assertEqual((target/'05_grid/estimated_grid_1km.csv').read_bytes(),data)
            io.compress_archival_outputs(target/'05_grid')

    def test_bad_archive_keeps_raw_and_original_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);data=self.fixture(p);name=io.ARCHIVAL_GRID_FILES[0]
            before=(p/'final_grid_manifest.json').read_bytes()
            (p/(name+'.gz')).write_bytes(b'bad compressed bytes')
            with self.assertRaises((ValueError,OSError,EOFError)):
                io.compress_archival_outputs(p,threshold_bytes=0)
            self.assertEqual((p/name).read_bytes(),data)
            self.assertEqual((p/'final_grid_manifest.json').read_bytes(),before)

if __name__=='__main__':unittest.main()
