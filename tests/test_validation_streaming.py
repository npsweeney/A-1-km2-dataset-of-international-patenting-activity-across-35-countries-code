"""Memory bounds and qualification controls for the existing validation inputs"""
import csv
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from validation import validate
import test_validation


class ValidationStreaming(unittest.TestCase):
    def test_large_unmatched_inputs_are_streamed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = test_validation.ValidationTests().fixture(root)
            with (root / 'source.csv').open() as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                extra = dict(next(reader), appln_id='99', pub_nbr='999', observation_id='unused')
            with (root / 'source.csv').open('a', newline='') as handle:
                writer = csv.DictWriter(handle, fields)
                for _ in range(100000):
                    writer.writerow(extra)
            support = json.loads((root / 'support.json').read_text())
            with (root / 'support.json').open('w') as handle:
                handle.write(json.dumps({k:v for k,v in support.items() if k != 'supports'})[:-1])
                handle.write(',"supports":[')
                for item in support['supports']:
                    handle.write(json.dumps(item) + ',')
                for n in range(1000):
                    item = dict(observation_id=f'unused-{n}', accepted_cells=[
                        dict(cell_id_100m=f'r0c{cell}') for cell in range(100)])
                    handle.write(json.dumps(item) + (',' if n < 999 else ']}'))
            tracemalloc.start()
            try:
                validate.run(args)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 20 * 1024**2)
            summary = json.loads((args.output / 'summary.json').read_text())
            self.assertEqual(summary['matching']['matched_address_pairs'], 3)
            self.assertEqual(summary['coverage'][1]['compared_mass'], 3)

    def test_support_identity_is_checked_independently_of_field_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'support.json'
            support = dict(observation_id='one', accepted_cells=[])
            path.write_text(json.dumps(dict(supports=[support], run_id='run', grid_identity={'resolution':100})))
            self.assertEqual(list(validate.support_items(path, {'resolution':100}, 'run')), [support])
            with self.assertRaisesRegex(ValueError, 'differ'):
                list(validate.support_items(path, {'resolution':1000}, 'run'))
            with self.assertRaisesRegex(ValueError, 'differ'):
                list(validate.support_items(path, {'resolution':100}, 'other'))
            path.write_text(json.dumps(dict(run_id='run', grid_identity={'resolution':100}, supports={})))
            with self.assertRaisesRegex(ValueError, 'support list'):
                list(validate.support_items(path, {'resolution':100}, 'run'))

    def test_shared_coordinate_qualification_keeps_boundaries_and_zero_axes(self):
        for point in [(90, 180), (-90, -180), (0, 1), (1, 0), (51.5, -.1)]:
            with self.subTest(point=point):
                self.assertTrue(validate.valid_coordinate(*point))
        for point in [(0, 0), (90.01, 1), (1, -180.01), (float('nan'), 1),
                      (1, float('inf')), (float('-inf'), 1)]:
            with self.subTest(point=point):
                self.assertFalse(validate.valid_coordinate(*point))


if __name__ == '__main__':
    unittest.main()
