"""Batched metadata retains exact source rows, date policy and family selection"""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from ingest.import_regpat import prepare_cohort
from test_docdb_selection import inputs


class MetadataCacheTests(unittest.TestCase):
    def test_persisted_index_reuses_matches_and_misses_and_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, db = inputs(root, [(1, 10, 1999, '1999-01-01'), (2, 10, 2024, '2024-01-01'),
                                       (3, 20, 2024, '2024-02-01'), (4, 30, None, None)])
            with sqlite3.connect(db) as source_db:
                source_db.execute('DELETE FROM tls201_appln WHERE appln_id=4')
            cache = root/'metadata.sqlite'
            before = prepare_cohort(source, root/'a.csv', patstat_db=db, end_year=2024)
            after = prepare_cohort(source, root/'b.csv', patstat_db=db, end_year=2024, metadata_cache=cache, metadata_cache_backup=root/'durable.sqlite')
            cache.unlink()  # Simulate a later allocation with no local SSD index
            reused = prepare_cohort(source, root/'c.csv', patstat_db=db, end_year=2024, metadata_cache=cache, metadata_cache_backup=root/'durable.sqlite')
            self.assertEqual(before, after)
            self.assertEqual(after, reused)
            for suffix in ('.csv', '.links.csv', '.undated.csv', '.excluded.csv', '.families.csv'):
                self.assertEqual((root/'a.csv').with_suffix(suffix).read_bytes(), (root/'b.csv').with_suffix(suffix).read_bytes())
                self.assertEqual((root/'a.csv').with_suffix(suffix).read_bytes(), (root/'c.csv').with_suffix(suffix).read_bytes())
            with sqlite3.connect(cache) as index:
                self.assertEqual(index.execute('SELECT count(*) FROM metadata').fetchone()[0], 4)
                self.assertEqual(index.execute("SELECT matches FROM metadata WHERE appln_id='4'").fetchone()[0], '[]')
            with sqlite3.connect(db) as changed:
                changed.execute("UPDATE tls201_appln SET docdb_family_id=99 WHERE appln_id=3")
            with self.assertRaisesRegex(ValueError, 'source identity changed'):
                prepare_cohort(source, root/'d.csv', patstat_db=db, metadata_cache=cache)
