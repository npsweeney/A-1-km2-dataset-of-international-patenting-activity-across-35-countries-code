"""Agreement ranking, conflict withholding and explicit first-result provenance"""
from geocoding.select_location import select_candidates

def candidate(i,postcode='missing',locality='missing',precision='address',osm=None):
 return dict(candidate_id=str(i),lat=i,lon=1,within_tl3=True,precision=precision,raw={'osm_type':'way','osm_id':i if osm is None else osm},comparisons={'postcode':{'state':postcode},'locality':{'state':locality}})

def test_exact_location_outranks_conflicting_competitors():
 a=candidate(1,'exact','exact');b=candidate(2,'conflicting','conflicting')
 for cs in [[a,b],[b,a]]:
  d=select_candidates(cs);assert d['selected_candidate_ids']==['1'];assert d['allocation_precision']=='address'

def test_locality_resolves_postcode_conflict():
 a=candidate(1,'conflicting','exact');b=candidate(2,'conflicting','conflicting')
 assert select_candidates([b,a])['selected_candidate_ids']==['1']

def test_dual_conflicts_never_choose_address():
 d=select_candidates([candidate(1,'conflicting','conflicting'),candidate(2,'conflicting','conflicting')]);assert d['allocation_precision'] is None;assert d['resolution_status']=='source_location_conflict'

def test_ties_do_not_use_order_and_duplicate_queries_are_not_objects():
 a=candidate(1,'exact','exact');b=candidate(2,'exact','exact')
 assert select_candidates([a,b])['selected_candidate_ids']==select_candidates([b,a])['selected_candidate_ids']
 assert select_candidates([a,b])['allocation_precision'] is None
 duplicate={**a,'candidate_id':'duplicate'}
 assert select_candidates([a,duplicate])['resolution_status']=='address_selected'

def test_precision_and_geometry_are_separate():
 d=select_candidates([candidate(1,precision='street'),candidate(2,precision='street')]);assert d['allocation_precision']=='street';assert d['selected_coordinate'] is None
 d=select_candidates([candidate(1,'conflicting','conflicting'),candidate(2,'exact',precision='postcode')]);assert d['allocation_precision']=='postcode'


def test_missing_field_cannot_resolve_competing_addresses():
 a=candidate(1,'missing','missing');b=candidate(2,'missing','conflicting')
 d=select_candidates([a,b]);assert d['resolution_status']=='uncorroborated_address_selection';assert d['allocation_precision'] is None


def test_colocated_confirmed_address_objects_select_actual_coordinate():
 a=candidate(1,'exact','exact');b=candidate(2,'exact','exact')
 a.update(lat=48.1813204,lon=16.3241467)
 b.update(lat=48.1813605,lon=16.324388)
 for c in (a,b):
  c['comparisons'].update(house={'state':'exact'},road={'state':'normalised_exact'})
 for cs in ([a,b],[b,a]):
  d=select_candidates(cs)
  assert d['allocation_precision']=='address'
  assert d['selected_coordinate'] in {(str(c['lat']),str(c['lon'])) for c in cs}
  assert d['selected_candidate_ids']==['1','2']
  assert d['coordinate_candidate_id']==cs[0]['candidate_id']
  assert d['coordinate_selection_method']=='first_qualifying_same_address_result'
 assert select_candidates([a,b])['selected_coordinate']==(str(a['lat']),str(a['lon']))
 assert select_candidates([b,a])['selected_coordinate']==(str(b['lat']),str(b['lon']))


def test_same_confirmed_address_takes_first_but_unconfirmed_remains_withheld():
 a=candidate(1,'exact','exact');b=candidate(2,'exact','exact')
 for c in (a,b):
  c['comparisons'].update(house={'state':'exact'},road={'state':'exact'})
 assert select_candidates([a,b])['allocation_precision']=='address'
 assert select_candidates([a,b])['selected_coordinate']==(str(a['lat']),str(a['lon']))
 b.update(lat=a['lat'],lon=a['lon'])
 b['comparisons']['road']['state']='conflicting'
 assert select_candidates([a,b])['allocation_precision'] is None


def test_first_means_first_qualifying_not_first_raw_result():
 outside=candidate(0,'exact','exact');outside['within_tl3']=False
 a=candidate(1,'exact','exact');b=candidate(2,'exact','exact')
 for c in (outside,a,b):
  c['comparisons'].update(house={'state':'exact'},road={'state':'exact'})
 d=select_candidates([outside,b,a])
 assert d['coordinate_candidate_id']=='2'
 assert d['selected_candidate_ids']==['1','2']
 assert len(d['selection_audit'])==3


def test_missing_house_and_uncorroborated_ties_do_not_use_first_result():
 a=candidate(1,'exact','exact');b=candidate(2,'exact','exact')
 for c in (a,b):c['comparisons'].update(house={'state':'missing'},road={'state':'exact'})
 assert select_candidates([a,b])['allocation_precision'] is None
 for c in (a,b):
  c['comparisons'].update(house={'state':'exact'},postcode={'state':'missing'},locality={'state':'missing'})
 d=select_candidates([a,b])
 assert d['allocation_precision'] is None
 assert d['coordinate_selection_method'] is None

import unittest

class AddressSelectionTests(unittest.TestCase):
    pass

for _name, _fn in list(globals().items()):
    if _name.startswith("test_"):
        setattr(AddressSelectionTests, _name, lambda self, fn=_fn: fn())
