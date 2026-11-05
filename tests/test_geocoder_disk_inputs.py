"""Disk-backed inputs preserve source order, deduplication and lossless links"""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from ingest.prepare_observations import read_observations, read_source_links, DiskRows, stable_id, clean_row


def reader(rows):
    out = io.StringIO()
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    out.seek(0)
    return csv.DictReader(out)


class DiskInputs(unittest.TestCase):
    def test_original_and_disk_paths_identical_with_duplicates_and_limits(self):
        rows = [dict(appln_id=str(aid), person_id=str(aid), actor='inventor', patent_year='2024',
                     address=road, city='Example', postal_code='12345', ctry_code='GB', reg_code='TEST')
                for aid, road in [(1,'12 First Road'),(2,'Other Road'),(3,'12 First Road')]]
        links = [dict(row, observation_id=stable_id(clean_row(row))) for row in rows]
        for limit in (None, 1):
            expected = read_observations(reader(rows), max_rows=limit)
            with sqlite3.connect(':memory:') as db:
                actual = read_observations(reader(rows), max_rows=limit, store=db)
                self.assertEqual(list(actual.observations), expected.observations)
                self.assertEqual(list(actual.address_links), expected.address_links)
                old_links = read_source_links(reader(links), expected.observations, allow_unselected=limit is not None)
                new_links = read_source_links(reader(links), actual.observations, allow_unselected=limit is not None, store=db)
                self.assertEqual(list(new_links), old_links)
                # Preflight attaches fields in place; these must survive later passes
                for row in actual.observations: row['region_resolution_status'] = 'ready'
                self.assertTrue(all(row['region_resolution_status']=='ready' for row in actual.observations))
                self.assertEqual(actual.observations.get(expected.observations[0]['observation_id'])['region_resolution_status'], 'ready')

    def test_invalid_and_missing_source_links_still_fail(self):
        row = dict(appln_id='1',person_id='p',actor='inventor',patent_year='2024',address='Road',city='City',postal_code='',ctry_code='GB',reg_code='TEST')
        for bad in (dict(row,observation_id='wrong'),dict(row,observation_id=stable_id(clean_row(row)),patent_year='')):
            with sqlite3.connect(':memory:') as db:
                batch = read_observations(reader([row]),store=db)
                with self.assertRaises(ValueError):read_source_links(reader([bad]),batch.observations,store=db)

    def test_partial_link_checkpoint_preserves_committed_prefix(self):
        from unittest.mock import patch
        row = dict(appln_id='1', person_id='p', actor='inventor', patent_year='2024',
                   address='Road', city='City', postal_code='', ctry_code='GB', reg_code='TEST')
        links = [dict(row, appln_id=str(i), observation_id=stable_id(clean_row(row))) for i in range(10003)]
        expected = read_source_links(reader(links), read_observations(reader([row])).observations)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'index.sqlite'
            db = sqlite3.connect(path)
            batch = read_observations(reader([row]), store=db)
            metadata = dict(count=len(batch.observations), fields=sorted(batch.observations.fields))
            original = DiskRows.append
            def interrupt(rows, value):
                original(rows, value)
                if rows.name == 'source_links' and rows.count == 10000:
                    raise RuntimeError('simulated interruption immediately after commit')
            with patch.object(DiskRows, 'append', interrupt):
                with self.assertRaises(RuntimeError):
                    read_source_links(reader(links), batch.observations, store=db)
            db.close()
            with sqlite3.connect(path) as resumed:
                observations = DiskRows(resumed, 'observations', existing=metadata)
                actual = read_source_links(reader(links), observations, store=resumed, resume=True)
                self.assertEqual(list(actual), expected)
                self.assertEqual(len(actual), 10003)

    def test_resume_appends_transport_evidence_without_resetting_sequence(self):
        from geocoding.geocoder.client import NominatimClient
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root/'events.jsonl'
            first = NominatimClient('http://localhost:8184', root/'cache', 'test', event_ledger_path=event_path)
            first._event('first-run')
            first._cache_connection.close()
            resumed = NominatimClient('http://localhost:8184', root/'cache', 'test', event_ledger_path=event_path, resume_events=True)
            resumed._event('resumed-run')
            resumed._cache_connection.close()
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            self.assertEqual([e['sequence'] for e in events], [1, 2])
            self.assertEqual([e['kind'] for e in events], ['first-run', 'resumed-run'])

    def test_processing_pause_backup_restore_skips_committed_addresses(self):
        from unittest.mock import patch
        from geocoding.search_and_select import run
        from geocoding.audit import RunGuard
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.csv';boundary=root/'boundary.json';stop=root/'stop'
            data=[dict(appln_id=str(i),person_id=str(i),address=f'Example Road {i}',locality='Example',postal_code='',ctry_code='JP',reg_code='TEST') for i in range(500)]
            with source.open('w',newline='') as handle:
                writer=csv.DictWriter(handle,list(data[0]));writer.writeheader();writer.writerows(data)
            boundary.write_text(json.dumps(dict(type='FeatureCollection',features=[dict(type='Feature',properties=dict(code='TEST'),geometry=dict(type='Polygon',coordinates=[[[0,0],[1,0],[1,1],[0,1],[0,0]]]))])))
            preflight=root/'preflight.json';preflight.write_text(json.dumps(dict(boundary_vintage=2024,boundary_codes=['TEST'])))
            common=dict(input_path=source,boundary_path=boundary,region_preflight=preflight,tl3_property='code',endpoint='http://localhost:8184',user_agent='test',cache_dir=root/'cache',workers=1,delay_seconds=0,request_budget=100)
            baseline=run(output_dir=root/'baseline',**common)
            original=DiskRows.append
            def pause_at_commit(rows,value):
                original(rows,value)
                if rows.name=='decisions' and rows.count==256:stop.touch()
            with patch.object(DiskRows,'append',pause_at_commit):
                paused=run(output_dir=root/'continued',stop_file=stop,**common)
            self.assertTrue(paused['paused']);self.assertEqual(paused['counts']['evaluated_addresses'],256)
            self.assertFalse((root/'continued/run_manifest.json').exists())
            index=root/'continued/input-index.sqlite';restored=root/'restored.sqlite'
            with sqlite3.connect(index) as src,sqlite3.connect(restored) as dst:
                src.backup(dst);self.assertEqual(dst.execute('pragma quick_check').fetchone()[0],'ok')
            index.rename(root/'before-restore.sqlite');restored.rename(index);stop.unlink()
            # A resumed client must never prepare/evaluate the already committed prefix
            from geocoding import search_and_select as geocoder
            prepare=geocoder.prepare_address;seen=[]
            def counted(row):seen.append(row['observation_id']);return prepare(row)
            with patch.object(geocoder,'prepare_address',counted):
                resumed=run(output_dir=root/'continued',stop_file=stop,**common)
            self.assertEqual(len(seen),244)
            self.assertEqual(resumed['counts'],baseline['counts'])
            for name in ('decisions.csv','observations.csv','address_links.csv','address_preparation.jsonl','queries.jsonl','candidates.jsonl'):
                self.assertEqual((root/'baseline'/name).read_bytes(),(root/'continued'/name).read_bytes(),name)

class FatalCacheErrors(unittest.TestCase):
    def test_cache_corruption_stops_run_without_completion_manifest(self):
        from unittest.mock import patch
        from geocoding.search_and_select import run, NominatimClient
        for workers in (1, 8):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'input.csv';boundary=root/'boundary.json'
                row=dict(appln_id='1',person_id='p',address='12 Example Road',locality='Example',
                         postal_code='',ctry_code='GB',reg_code='TEST')
                with source.open('w',newline='') as handle:
                    writer=csv.DictWriter(handle,list(row));writer.writeheader();writer.writerow(row)
                boundary.write_text(json.dumps(dict(type='FeatureCollection',features=[dict(type='Feature',
                    properties=dict(code='TEST'),geometry=dict(type='Polygon',coordinates=[[[0,0],[1,0],[1,1],[0,1],[0,0]]]))])))
                preflight=root/'preflight.json';preflight.write_text(json.dumps(dict(boundary_vintage=2024,boundary_codes=['TEST'])))
                with patch.object(NominatimClient,'search',side_effect=sqlite3.DatabaseError('database disk image is malformed')):
                    with self.assertRaises(sqlite3.DatabaseError):
                        run(input_path=source,boundary_path=boundary,region_preflight=preflight,output_dir=root/'out',
                            tl3_property='code',endpoint='http://localhost:8080',cache_dir=root/'cache',
                            user_agent='test',workers=workers,request_budget=10)
                self.assertFalse((root/'out/run_manifest.json').exists())
                self.assertTrue((root/'out/run_state.json').exists())

if __name__ == '__main__':
    unittest.main()
