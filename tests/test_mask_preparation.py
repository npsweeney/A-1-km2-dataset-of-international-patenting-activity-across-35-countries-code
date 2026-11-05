import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from validation import mask


def case(number):
    return dict(
        source_observation_id=f'source-{number}', house_number=str(number),
        street='Example Street', city='Example', postal_code='AB1', ctry_code='GB',
        reg_code='UK1', true_latitude='51', true_longitude='0.1',
        ground_truth_precision='address', appln_id=str(number), person_id=str(number),
        actor='inventor', patent_year='2010', sample_partition='holdout',
    )


def save(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class MaskPreparationTests(unittest.TestCase):
    def test_command_requires_10000_distinct_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, truth = root/'source.csv', root/'masks', root/'truth'
            arguments = ['mask', '--input', str(source), '--output-dir', str(output),
                         '--truth-output-dir', str(truth)]
            save(source, [case(1)])
            with patch.object(sys, 'argv', arguments), self.assertRaisesRegex(ValueError, '10000'):
                mask.main()
            self.assertFalse(output.exists())
            save(source, [case(n) for n in range(1, 10001)])
            with patch.object(sys, 'argv', arguments):
                mask.main()
            manifest = json.loads((output/'manifest.json').read_text())
            self.assertEqual(manifest['source_cases'], 10000)
            self.assertEqual(manifest['cases_per_mask'], {'street': 10000, 'postcode': 10000})
            with (output/'all-masks.csv').open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 20000)
            self.assertTrue(all(row['appln_id'] and row['person_id'] for row in rows))
            self.assertNotIn('true_latitude', rows[0])
            self.assertTrue(all(row['patent_year_status'] == 'valid' for row in rows))

    def test_missing_ids_leave_no_output_directories(self):
        for field in ('source_observation_id', 'appln_id', 'person_id'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                save(root/'source.csv', [case(1), {**case(2), field: ''}])
                with self.assertRaisesRegex(ValueError, 'complete IDs'):
                    mask.prepare(root/'source.csv', root/'masks', root/'truth')
                self.assertFalse((root/'masks').exists())
                self.assertFalse((root/'truth').exists())

    def test_address_years_do_not_count_as_distinct_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root/'source.csv', [case(1), {**case(1), 'patent_year': '2011'}])
            with self.assertRaisesRegex(ValueError, 'distinct source addresses'):
                mask.prepare(root/'source.csv', root/'masks', root/'truth', expected_cases=2)

    def test_invalid_year_leaves_no_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root/'source.csv', [{**case(1), 'patent_year': '99999'}])
            with self.assertRaisesRegex(ValueError, 'Invalid role, year'):
                mask.prepare(root/'source.csv', root/'masks', root/'truth')
            self.assertFalse((root/'masks').exists())

    def test_construction_cannot_be_inside_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root/'source.csv', [case(1)])
            with self.assertRaisesRegex(ValueError, 'outside'):
                mask.prepare(root/'source.csv', root/'truth/masks', root/'truth')


if __name__ == '__main__':
    unittest.main()
