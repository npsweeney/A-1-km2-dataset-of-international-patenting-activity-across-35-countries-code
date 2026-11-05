"""Offline controls for identifier sources and application-date lineage"""
from contextlib import closing
import csv
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from ingest.patent_metadata.build_family_backbone import build
from ingest.patent_metadata.resolve_ops_families import parse_application_dates, application_filing_date
from ingest.patent_metadata.application_years import resolve_filing_year


class SourceTests(unittest.TestCase):
    def test_native_patstat_zero_families_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patstat = root / "patstat.sqlite"
            with closing(sqlite3.connect(patstat)) as db:
                db.execute("CREATE TABLE tls201_appln(appln_id INTEGER PRIMARY KEY, docdb_family_id INTEGER, inpadoc_family_id INTEGER)")
                db.executemany("INSERT INTO tls201_appln VALUES(?,?,?)", [(1, 0, 0), (2, 10, 20)])
                db.commit()
            ids = root / "ids.txt"
            ids.write_text("1\n2\n")
            mapping = root / "mapping.sqlite"
            subprocess.run([sys.executable, "-m", "run", "map-families", "--regpat-ids", str(ids), "--patstat-db", str(patstat), "--output-db", str(mapping), "--summary", str(root / "mapping.json")], check=True, capture_output=True)
            with closing(sqlite3.connect(mapping)) as db:
                self.assertEqual(db.execute("SELECT docdb_family_id,inpadoc_family_id FROM application_family WHERE appln_id='1'").fetchone(), (None, None))
            backbone = root / "backbone.sqlite"
            result = build(mapping, None, backbone)
            self.assertEqual(result["resolved_applications"], 1)
            self.assertEqual(result["sources"]["patstat"], {"path": str(mapping.resolve())})
            with closing(sqlite3.connect(backbone)) as db:
                self.assertNotEqual(db.execute("SELECT canonical_family_id FROM regpat_family_backbone WHERE appln_id='1'").fetchone()[0], "docdb:0")
            legacy = root / "legacy.sqlite"
            with closing(sqlite3.connect(legacy)) as db:
                db.execute("CREATE TABLE application_family(appln_id TEXT,docdb_family_id INTEGER,inpadoc_family_id INTEGER)")
                db.executemany("INSERT INTO application_family VALUES(?,?,?)", [("1",0,0),("2",10,20)])
                db.commit()
            self.assertEqual(build(legacy, None, root / "legacy_backbone.sqlite")["resolved_applications"], 1)

    def test_backbone_sources_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); base = root/'base.db'; ops = root/'ops.db'
            with closing(sqlite3.connect(base)) as db:
                db.execute('CREATE TABLE application_family(appln_id TEXT,docdb_family_id TEXT,inpadoc_family_id TEXT)')
                db.executemany('INSERT INTO application_family VALUES(?,?,?)', [('1','10',None),('2',None,None)])
                db.commit()
            with closing(sqlite3.connect(ops)) as db:
                db.execute('CREATE TABLE application(appln_id TEXT,family_id TEXT,primary_reference TEXT)')
                db.executemany('INSERT INTO application VALUES(?,?,?)', [('1','11','EP.1'),('2','20','EP.2')])
                db.execute('CREATE TABLE publication(reference TEXT,status TEXT)')
                db.commit()
            self.assertEqual(build(base,None,root/'patstat.db')['resolved_applications'],1)
            self.assertEqual(build(None,ops,root/'ops-only.db')['resolved_applications'],2)
            self.assertEqual(build(base,ops,root/'both.db')['family_resolution_conflicts'],1)

    def test_dates_use_application_identity_not_publication_date(self):
        xml = b'<root><exchange-document country="EP" doc-number="123"><publication-reference><document-id><date>20260101</date></document-id></publication-reference><application-reference><document-id document-id-type="docdb"><country>EP</country><doc-number>456</doc-number><date>20200304</date></document-id></application-reference></exchange-document></root>'
        records = parse_application_dates(xml)
        self.assertEqual(len(records),1)
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE application_date_evidence(reference,application_country,application_number,document_id_type,filing_date,record_id,application_reference_xml)')
        db.executemany('INSERT INTO application_date_evidence VALUES(?,?,?,?,?,?,?)',records)
        evidence = application_filing_date(db,references=['EP.123'],application_number='456',application_country='EP')
        self.assertEqual(evidence['filing_year'],2020)
        self.assertEqual(resolve_filing_year(None,evidence)['patent_year'],2020)
        self.assertEqual(resolve_filing_year('2021',evidence)['patent_year'],2021)
        self.assertTrue(resolve_filing_year('2021',evidence)['ops_year_conflict'])
        self.assertEqual(application_filing_date(db,references=['EP.123'],application_number='other',application_country='EP')['status'],'missing')
        changed=list(records[0]);changed[4]='20210304'
        db.execute('INSERT INTO application_date_evidence VALUES(?,?,?,?,?,?,?)',changed)
        self.assertEqual(application_filing_date(db,references=['EP.123'],application_number='456',application_country='EP')['status'],'conflict')
        db.close()


    def test_queue_selects_all_missing_or_explicit_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flags = []
            for office, actor in [('EPO','App'),('EPO','Inv'),('PCT','App'),('PCT','Inv')]:
                path = root / (office + '_' + actor + '.txt')
                with path.open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=['appln_id','app_nbr','pub_nbr','pct_nbr','internat_appln_nr'], delimiter='|')
                    writer.writeheader()
                    for identifier in ['1','2','3']:
                        writer.writerow(dict(appln_id=identifier, app_nbr='application'+identifier, pub_nbr=identifier, pct_nbr='202600000'+identifier, internat_appln_nr='pct'+identifier))
                flags += ['--'+office.lower()+'-'+actor.lower(), str(path)]
            base = root / 'patstat.db'
            with closing(sqlite3.connect(base)) as db:
                db.execute('CREATE TABLE application_family(appln_id TEXT,docdb_family_id TEXT)')
                db.executemany('INSERT INTO application_family VALUES(?,?)', [('1','10'),('2',None)])
                db.commit()
            selected = root / 'selected.txt'; selected.write_text('3\n')
            for label, extra, expected in [('all', [], {'1','2','3'}), ('missing', ['--family-db', str(base)], {'2','3'}), ('selected', ['--application-ids', str(selected)], {'3'})]:
                output = root / (label+'.csv')
                subprocess.run([sys.executable, '-m', 'run', 'ops-queue', *flags, *extra, '--output', str(output)], check=True, capture_output=True)
                with output.open() as handle:
                    self.assertEqual({row['appln_id'] for row in csv.DictReader(handle)}, expected)

if __name__ == '__main__':
    unittest.main()
