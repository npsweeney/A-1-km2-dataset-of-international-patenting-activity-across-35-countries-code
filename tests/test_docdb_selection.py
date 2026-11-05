"""DOCDB representative selection before address processing"""
from contextlib import closing
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from ingest.import_regpat import prepare_cohort


def inputs(root, applications):
    release = root / 'regpat'
    release.mkdir()
    database = root / 'patstat.sqlite'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE tls201_appln(appln_id INTEGER PRIMARY KEY,docdb_family_id INTEGER,appln_filing_year INTEGER,appln_filing_date TEXT)')
        db.executemany('INSERT INTO tls201_appln VALUES(?,?,?,?)', applications)
    for office in ('EPO', 'PCT'):
        for code in ('App', 'Inv'):
            name = 'app_name' if code == 'App' else 'inv_name'
            fields = ['appln_id','address','city','postal_code','reg_code','ctry_code',name]
            if office == 'EPO': fields.append('person_id')
            with (release / f'test_{office}_{code}_reg.txt').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter='|')
                writer.writeheader()
                for aid, *_ in reversed(applications):
                    row = dict(appln_id=str(aid),address=f'Road {aid}',city='Example',postal_code='12345',reg_code='TEST',ctry_code='GB')
                    row[name] = 'Example person'
                    if office == 'EPO': row['person_id'] = f'p{aid}'
                    writer.writerow(row)
    return release, database


class DOCDBSelectionTests(unittest.TestCase):
    def test_exact_date_then_numeric_id_selects_representative_before_year_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, database = inputs(root, [(20,100,1999,'1999-12-31'),(21,100,2001,'2001-01-01'),
                (10,200,2020,'2020-02-01'),(2,200,2020,'2020-02-01'),(1,200,2020,'2020-12-01'),
                (30,300,2025,'2025-01-01')])
            counts = prepare_cohort(release, root/'addresses.csv', patstat_db=database)
            with (root/'addresses.links.csv').open() as handle:
                links = list(csv.DictReader(handle))
            self.assertEqual({row['appln_id'] for row in links}, {'2','30'})
            self.assertEqual(counts['selected_families'], 2)
            self.assertEqual(counts['eligible_links'], 8)
            self.assertEqual(counts['outside_years'], 8)
            self.assertEqual(counts['later_application_rows'], 8)
            self.assertTrue(all(row['appln_id']==row['representative_appln_id'] for row in links))
            with (root/'addresses.families.csv').open() as handle:
                families = {row['family_id']:row for row in csv.DictReader(handle)}
            self.assertEqual(families['docdb:100']['representative_appln_id'], '20')
            self.assertEqual(families['docdb:100']['selection_status'], 'outside_years')
            limited = prepare_cohort(release, root/'limited.csv', patstat_db=database, end_year=2020)
            self.assertEqual(limited['selected_families'], 1)
            backbone = root/'backbone.sqlite'
            with closing(sqlite3.connect(backbone)) as db, db:
                db.execute('CREATE TABLE regpat_family_backbone(appln_id TEXT PRIMARY KEY,canonical_family_id TEXT,resolution_status TEXT,family_id_kind TEXT,family_resolution_conflict INTEGER)')
                db.executemany('INSERT INTO regpat_family_backbone VALUES(?,?,?,?,?)', [('2','docdb:200','resolved','docdb',0),('30','docdb:300','resolved','docdb',0)])
            reused = prepare_cohort(release,root/'reused.csv',patstat_db=database,family_backbone=backbone)
            self.assertEqual(reused['selected_families'], 2)
            self.assertEqual(reused['eligible_links'], 8)
            self.assertEqual(reused['outside_years'], 8)


    def test_missing_dates_and_zero_family_are_excluded_before_geocoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, database = inputs(root, [(1,100,2020,'2020-01-01'),(2,100,None,None),
                (3,0,2020,'2020-01-01'),(4,200,2020,None)])
            counts = prepare_cohort(release, root/'addresses.csv', patstat_db=database)
            self.assertEqual(counts['selected_families'], 0)
            self.assertEqual(counts['eligible_links'], 0)
            self.assertEqual(counts['undated_rows'], 4)
            with (root/'addresses.excluded.csv').open() as handle:
                excluded = list(csv.DictReader(handle))
            self.assertEqual({row['selection_status'] for row in excluded}, {'missing_filing_date','unresolved_docdb_family'})
