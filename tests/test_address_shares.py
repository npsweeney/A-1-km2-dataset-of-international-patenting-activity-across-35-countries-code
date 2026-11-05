import unittest
from fractions import Fraction
from aggregation.aggregate_grid_activity import _address_shares


def source_row(name, observation, *, application='1', role='inventor', address=None, region='R1'):
    return dict(appln_id=application, actor=role, person_id='reused-source-id',
                inv_name=name, app_name=name, observation_id=observation,
                address=address or observation, city='Paris', postal_code='75001',
                ctry_code='FR', reg_code=region)


def weights(rows, selected, *, application='1', role='inventor'):
    return _address_shares({oid: [] for oid in selected}, {},
                           application_id=application, actor=role, roster=rows)


class FamilyAddressWeights(unittest.TestCase):
    def test_outside_sample_keeps_half(self):
        rows = [source_row('Alice', 'paris'), source_row('Bob', 'outside')]
        shares, count, _, withheld, _ = weights(rows, ['paris'])
        self.assertEqual(count, 2)
        self.assertEqual(shares['paris'], Fraction(1, 2))
        self.assertEqual(withheld, Fraction(1, 2))

    def test_two_inventors_sharing_paris_keep_two_thirds(self):
        rows = [source_row('Alice', 'paris'), source_row('Bob', 'paris'),
                source_row('Carol', 'outside')]
        shares, count, _, withheld, _ = weights(rows, ['paris'])
        self.assertEqual(count, 3)
        self.assertEqual(shares['paris'], Fraction(2, 3))
        self.assertEqual(withheld, Fraction(1, 3))

    def test_names_are_scoped_to_patent_and_role(self):
        rows = [source_row('Alice', 'paris'), source_row('Bob', 'outside'),
                source_row('Alice', 'elsewhere', application='2'),
                source_row('Company', 'office', role='applicant')]
        self.assertEqual(weights(rows, ['paris'])[0]['paris'], Fraction(1, 2))
        self.assertEqual(weights(rows, ['elsewhere'], application='2')[0]['elsewhere'], 1)
        self.assertEqual(weights(rows, ['office'], role='applicant')[0]['office'], 1)

    def test_duplicate_rows_and_regions_do_not_add_addresses(self):
        rows = [source_row('Alice', 'paris', address='One address'),
                source_row('Alice', 'paris2', address='One address', region='R2'),
                source_row('Bob', 'outside')]
        result = weights(rows, ['paris', 'paris2'])
        self.assertEqual(sum(result[0].values()), Fraction(1, 2))
        self.assertEqual(result, weights(rows + [rows[0]], ['paris', 'paris2']))

    def test_share_splits_across_unique_addresses(self):
        rows = [source_row('Alice', 'a'), source_row('Alice', 'b'), source_row('Bob', 'b')]
        self.assertEqual(weights(rows, ['a', 'b'])[0], {'a': Fraction(1, 4), 'b': Fraction(3, 4)})

    def test_names_trim_whitespace_and_missing_names_withhold(self):
        rows = [source_row('Alice', 'paris'), source_row(' Alice ', 'paris')]
        self.assertEqual(weights(rows, ['paris'])[1], 1)
        self.assertIsNone(weights(rows + [source_row('', 'outside')], ['paris']))
