"""Intake fixtures exercise PATSTAT and exact OPS date resolution"""
import csv
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from aggregation.aggregate_grid_activity import _address_shares
from ingest.import_regpat import prepare_cohort
from ingest.patent_metadata.build_family_backbone import build

path = Path(__file__).resolve().parents[1] / "tests/fixtures/patent_records.py"
spec = importlib.util.spec_from_file_location("example_patents", path)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


class IntakeSourceFixtures(unittest.TestCase):
    def test_all_sources_use_same_cohort_and_real_address_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example.create(root / "inputs")
            counts = prepare_cohort(root / "inputs/regpat", root / "cohort.csv",
                                   patstat_db=root / "inputs/patstat.sqlite", ops_db=root / "inputs/ops.sqlite")
            self.assertEqual(counts["unique_addresses"], 3)
            self.assertEqual(counts["eligible_links"], 11)
            self.assertEqual(counts["undated_rows"], 0)
            self.assertEqual(counts["selected_families"], 3)
            self.assertEqual(counts["later_application_rows"], 3)
            with (root / "cohort.links.csv").open(newline="") as handle:
                links = list(csv.DictReader(handle))
            self.assertEqual({r["patent_year"] for r in links}, {"2020"})
            self.assertEqual({r["reg_code"] for r in links}, {"UKD33"})
            self.assertNotIn("90000000002", {r["appln_id"] for r in links})
            inventors = {}
            for row in links:
                if row["actor"] == "inventor":
                    inventors.setdefault(row["family_id"], set()).add(row["inv_name"])
            self.assertEqual({family: len(names) for family, names in inventors.items()},
                             {"docdb:880000001": 2, "docdb:880000002": 3, "docdb:880000003": 1})
            for aid, expected in (("90000000001", {"124 Oxford Road": Fraction(1, 2), "Stockport Road": Fraction(1, 2)}),
                                  ("90000000003", {"Stockport Road": Fraction(2, 3), "": Fraction(1, 3)}),
                                  ("90000000004", {"": Fraction(1)})):
                addresses = {row["observation_id"]: row["address"] for row in links
                             if row["appln_id"] == aid and row["actor"] == "inventor"}
                shares, *_ = _address_shares(addresses, {}, application_id=aid, actor="inventor", roster=links)
                self.assertEqual({addresses[oid]: share for oid, share in shares.items()}, expected)
            self.assertTrue(any(r["year_source"] == "epo_ops_application_reference" for r in links))
            self.assertTrue(any(r["office"] == "PCT" and not r.get("person_id") for r in links))
            ops = prepare_cohort(root / "inputs/regpat", root / "ops.csv", ops_db=root / "inputs/ops.sqlite")
            self.assertEqual(ops["eligible_links"], 11)
            patstat = prepare_cohort(root / "inputs/regpat", root / "patstat.csv", patstat_db=root / "inputs/patstat.sqlite")
            self.assertEqual(patstat["eligible_links"], 7)
            self.assertEqual(patstat["undated_rows"], 4)
            summary = build(None, root / "inputs/ops.sqlite", root / "families.sqlite")
            self.assertEqual(summary["resolved_applications"], 4)
