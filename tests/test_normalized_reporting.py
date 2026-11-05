import unittest
from geocoding.search_and_select import normalized_review_fields

class NormalizedReporting(unittest.TestCase):
    def check(self, *, inside=True, precision='postcode', allocatable=True, candidates=True, region='ready'):
        row=dict(address='Example Road',locality='Example',postal_code='12345',ctry_code='GB',reg_code='TEST',region_resolution_status=region)
        candidate=dict(candidate_id='c1',tl3_match=inside,derived_tl3=['TEST'] if inside else ['OTHER'],
                       assessed_granularity=precision,raw_candidate={'address':{'postcode':'12345','country_code':'gb'}},country_match=True)
        selection=dict(precision=precision,allocation_precision=precision if allocatable else None,
                       selected_candidate_ids=['c1'] if inside and candidates else [],
                       resolution_status='postcode_extent_required' if allocatable else 'no_in_tl3_candidate')
        decision=dict(status='matched' if inside and candidates else 'abstained',reason='candidate_inside_expected_tl3' if inside else 'all_candidates_outside_expected_tl3',granularity=precision,selection=selection)
        return normalized_review_fields(row,[candidate] if candidates else [],decision)

    def test_precision_and_inclusion_follow_saved_selection(self):
        result=self.check()
        self.assertEqual(result['nominatim_granularity'],'postcode')
        self.assertEqual(result['tl3_match_y_n'],'Y')
        self.assertEqual(result['included'],'yes')
        self.assertEqual(result['nominatim_postcode_selected'],'12345')

    def test_outside_candidates_are_disagreement_not_missing(self):
        result=self.check(inside=False,allocatable=False)
        self.assertEqual(result['tl3_match_y_n'],'N')
        self.assertEqual(result['included'],'no')

    def test_no_candidates_or_unknown_source_region_remain_unresolved(self):
        for kwargs in [dict(candidates=False,allocatable=False),dict(region='source_region_unknown',allocatable=False)]:
            self.assertEqual(self.check(**kwargs)['tl3_match_y_n'],'')

    def test_regional_match_does_not_imply_allocation(self):
        result=self.check(precision='region',allocatable=False)
        self.assertEqual(result['tl3_match_y_n'],'Y')
        self.assertEqual(result['included'],'no')
        self.assertEqual(result['nominatim_correct_detail_y_n'],'')
