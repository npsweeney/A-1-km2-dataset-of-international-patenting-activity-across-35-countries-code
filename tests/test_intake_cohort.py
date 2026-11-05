"""Offline synthetic controls for cohort preparation and lossless source links"""
from contextlib import closing
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from ingest.import_regpat import prepare_cohort
from cleaning.clean_addresses import clean_addresses
from ingest.prepare_observations import read_observations, read_source_links
from geocoding.validation.query_bounds import variants


class IntakeTests(unittest.TestCase):
    def test_year_selection_deduplication_preserves_every_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); release=root/'release'; release.mkdir(); patstat=root/'patstat.db'
            with closing(sqlite3.connect(patstat)) as db:
                db.execute('CREATE TABLE tls201_appln(appln_id TEXT PRIMARY KEY,appln_filing_year INTEGER,appln_filing_date TEXT,docdb_family_id INTEGER)')
                db.executemany('INSERT INTO tls201_appln VALUES(?,?,?,?)',[('1',2025,'2025-01-01',11),('2',2001,'2001-01-01',12),('3',1999,'1999-01-01',13),('4',None,None,14)])
                db.commit()
            for office in ('EPO','PCT'):
                for code in ('App','Inv'):
                    name='app_name' if code=='App' else 'inv_name'
                    share='app_share' if code=='App' else 'inv_share'
                    fields=['appln_id','address','city','postal_code','reg_code','ctry_code',name,share]
                    if office=='EPO': fields.append('person_id')
                    with (release/f'202608_{office}_{code}_reg.txt').open('w',newline='',encoding='utf-8-sig') as handle:
                        writer=csv.DictWriter(handle,fieldnames=fields,delimiter='|'); writer.writeheader()
                        for aid in ('1','2','3','4'):
                            row=dict(appln_id=aid,address='Synthetic Example Road 12',city='Example',postal_code='12345',reg_code='TEST',ctry_code='US')
                            row.update({name:'Synthetic person',share:'0.5'})
                            if office=='EPO': row['person_id']='p'+aid
                            writer.writerow(row)
            output=root/'addresses.csv'
            counts=prepare_cohort(release,output,patstat_db=patstat)
            self.assertEqual({key:counts[key] for key in ('source_rows','eligible_links','unique_addresses','undated_rows','outside_years')},
                dict(source_rows=16,eligible_links=8,unique_addresses=1,undated_rows=4,outside_years=4))
            cleaned=root/'clean.csv'; self.assertEqual(clean_addresses(output,cleaned),1)
            with cleaned.open(newline='') as handle: batch=read_observations(csv.DictReader(handle))
            with output.with_suffix('.links.csv').open(newline='') as handle: links=read_source_links(csv.DictReader(handle),batch.observations)
            self.assertEqual(len(links),8)
            self.assertEqual({row['year'] for row in links},{'2001','2025'})
            self.assertEqual(links[0]['app_share'],'0.5')
            self.assertTrue(any(row['office']=='PCT' and not row['person_id'] for row in links))
            self.assertTrue(variants(batch.observations[0]))
            with self.assertRaises(ValueError): prepare_cohort(release,output,patstat_db=patstat)
            limited=prepare_cohort(release,root/'limited.csv',patstat_db=patstat,end_year=2001)
            self.assertEqual(limited['eligible_links'],4)
            # The canonical refresh overrides stale dates, but never fills a missing
            # or conflicting canonical record with an older PATSTAT date
            import duckdb
            master = root / 'master.parquet'
            with duckdb.connect() as d:
                d.execute("CREATE TABLE dates(appln_id VARCHAR, filing_date DATE, filing_year INTEGER, date_conflict BOOLEAN)")
                d.executemany("INSERT INTO dates VALUES(?,?,?,?)", [('1','2024-12-31',2024,False),('2',None,None,False),('3','1999-01-01',1999,True),('4','2023-01-01',2023,False)])
                d.execute('COPY dates TO ? (FORMAT PARQUET)', [str(master)])
            (root / 'release.json').write_text('{"validated": true, "release_id": "test-master-release"}')
            ops = root / 'saved-ops.sqlite'
            with closing(sqlite3.connect(ops)) as db:
                db.execute('CREATE TABLE application(appln_id TEXT, publication_key TEXT, payload TEXT)')
                db.execute("INSERT INTO application VALUES('1','EP.example','{}')")
                db.commit()
            refreshed = root / 'refreshed.csv'
            refreshed_counts = prepare_cohort(release,refreshed,patstat_db=patstat,ops_db=ops,application_master=master,end_year=2024)
            import json
            intake_manifest = json.loads(refreshed.with_suffix('.manifest.json').read_text())
            self.assertEqual(intake_manifest['application_master_release'], 'test-master-release')
            self.assertEqual(intake_manifest['end_year'], 2024)
            self.assertEqual(refreshed_counts['eligible_links'],8)
            self.assertEqual(refreshed_counts['undated_rows'],8)
            with refreshed.with_suffix('.links.csv').open() as handle:
                refreshed_links = list(csv.DictReader(handle))
            self.assertEqual({row['patent_year'] for row in refreshed_links},{'2023','2024'})
            self.assertEqual({row['year_source'] for row in refreshed_links},{'canonical_application_master'})
            missing=prepare_cohort(release,root/'missing.csv')
            self.assertEqual(missing['undated_rows'],16)
            bad=dict(links[0]);bad['address']='Changed'
            with self.assertRaises(ValueError):
                class Reader(list): fieldnames=list(bad)
                read_source_links(Reader([bad]),batch.observations)


if __name__=='__main__': unittest.main()
