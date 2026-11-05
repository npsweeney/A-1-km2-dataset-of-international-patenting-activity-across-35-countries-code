"""Selected-family exports retain unlocated roster members and exclude other families"""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts/export_patent_traces.py'
spec = importlib.util.spec_from_file_location('patent_traces', path)
traces = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traces)


class PatentTraces(unittest.TestCase):
    def test_complete_roster_and_family_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(filename, rows):
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)

            def source(family, oid, name):
                return dict(family_id=family, observation_id=oid, inv_name=name,
                            representative_appln_id='10', representative_filing_date='2020-01-01', patent_year='2020')

            write('01_addresses.links.csv', [source('docdb:1', 'shared', 'A'),
                                             source('docdb:1', 'unlocated', 'B'),
                                             source('docdb:2', 'shared', 'Other')])
            facts = [dict(family_id='docdb:1', observation_id='shared', weight='0.5'),
                     dict(family_id='docdb:2', observation_id='shared', weight='1')]
            for name in ['family_grid.csv', 'family_highest_weight_cell_grid.csv']:
                write('06_activity/' + name, facts)
            write('06_activity/withheld_families.csv', [dict(family_id='docdb:1', observation_id='unlocated', withheld_weight='0.5')])
            write('05_grid/estimated_grid_1km.csv', facts)
            result = traces.export(root, ['docdb:1', 'docdb:1'])
            self.assertEqual(len(result['cases']), 1)
            case = result['cases'][0]
            self.assertEqual([r['inv_name'] for r in case['input_rows']], ['A', 'B'])
            self.assertEqual(case['weighted'], facts[:1])
            self.assertEqual(case['withheld'][0]['withheld_weight'], '0.5')
            shared = next(a for a in case['addresses'] if a['observation_id'] == 'shared')
            self.assertEqual(shared['spatial_weights'], facts[:1])
            self.assertNotIn('Other', json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'Families absent'):
                traces.export(root, ['docdb:3'])
