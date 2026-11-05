"""General deterministic selection after the field agreement stage"""
from __future__ import annotations
from typing import Any
SELECTION_VERSION = 'regpat-candidate-selection-v3'
PRECISION_RANK = {'region': 0, 'locality': 1, 'postcode': 2, 'street': 3, 'address': 4}
CONFIRMED = {'exact', 'normalised_exact', 'documented_country_equivalence', 'explicit_number_list_member'}

def location_state(comparison: dict[str, Any]) -> str:
    if comparison['state'] in CONFIRMED:
        return 'agreement'
    if comparison['state'] == 'conflicting':
        return 'conflict'
    return 'unknown'

def location_support(candidate: dict[str, Any]) -> tuple[int, str, str]:
    postcode = location_state(candidate['comparisons']['postcode'])
    locality = location_state(candidate['comparisons']['locality'])
    agreements = sum((s == 'agreement' for s in (postcode, locality)))
    conflicts = sum((s == 'conflict' for s in (postcode, locality)))
    tier = 4 if agreements == 2 else 3 if agreements == 1 and conflicts == 0 else 2 if conflicts == 0 else 1 if agreements == 1 else 0
    return (tier, postcode, locality)

def object_identity(candidate: dict[str, Any]) -> tuple[str, ...]:
    raw = candidate['raw']
    if raw.get('osm_type') and raw.get('osm_id') is not None:
        return (str(raw['osm_type']), str(raw['osm_id']))
    if raw.get('place_id') is not None:
        return ('place_id', str(raw['place_id']))
    return ('coordinate_object', str(raw.get('addresstype')), str(candidate['lat']), str(candidate['lon']))

# Choose from saved candidate order using field agreement; retain alternatives for audit
def select_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Select location precision and allocation from address evidence"""
    audit = []
    eligible = []
    for c in candidates:
        tier, postcode, locality = location_support(c)
        blocked = c['precision'] in {'address', 'street'} and postcode == locality == 'conflict'
        audit.append(dict(candidate_id=c['candidate_id'], within_tl3=c['within_tl3'], precision=c['precision'], postcode_state=postcode, locality_state=locality, location_tier=tier, blocked_dual_location_conflict=blocked))
        if c['within_tl3'] and (not blocked):
            eligible.append(c)
    inside = [c for c in candidates if c['within_tl3']]
    if not eligible:
        return dict(precision=max((c['precision'] for c in inside), key=PRECISION_RANK.get, default='unresolved'), resolution_status='source_location_conflict' if inside else 'no_in_tl3_candidate', allocation_precision=None, selected_candidate_ids=[], selected_coordinate=None, coordinate_candidate_id=None, coordinate_selection_method=None, selection_audit=audit)
    finest = max((PRECISION_RANK[c['precision']] for c in eligible))
    level = [c for c in eligible if PRECISION_RANK[c['precision']] == finest]
    tier = max((location_support(c)[0] for c in level))
    best = [c for c in level if location_support(c)[0] == tier]
    objects = {}
    for c in best:
        objects.setdefault(object_identity(c), []).append(c)
    precision = best[0]['precision']
    original_objects = {object_identity(c) for c in inside if c['precision'] == precision}
    uncorroborated = precision == 'address' and len(original_objects) > 1 and (not any((location_state(c['comparisons'][f]) == 'agreement' for c in best for f in ('postcode', 'locality'))))
    # Multiple returned objects at the same confirmed source address are not
    # competing addresses. Preserve API/query order and take the first result
    same_address = precision == 'address' and len(objects) > 1 and all(
        all(c['comparisons'].get(f, {}).get('state') in CONFIRMED for f in ('house', 'road'))
        and any(location_state(c['comparisons'][f]) == 'agreement' for f in ('postcode', 'locality'))
        for c in best
    )
    competing = precision == 'address' and len(objects) > 1 and not same_address
    status = 'uncorroborated_address_selection' if uncorroborated else 'competing_address_objects' if competing else 'address_selected' if precision == 'address' else 'street_geometry_required' if precision == 'street' else 'postcode_extent_required' if precision == 'postcode' else 'coarse_context_only'
    coordinates = {(str(c['lat']), str(c['lon'])) for c in best}
    coordinate = next(iter(coordinates)) if precision == 'address' and (not competing) and (not uncorroborated) and (len(coordinates) == 1) else None
    if same_address and not uncorroborated:
        coordinate = (str(best[0]['lat']), str(best[0]['lon']))
        status = 'address_selected'
    if precision == 'address' and (not competing) and (not uncorroborated) and (coordinate is None):
        status = 'same_object_coordinate_variation'
    coordinate_candidate = next((c['candidate_id'] for c in best if coordinate == (str(c['lat']), str(c['lon']))), None)
    allocated = precision if precision in {'address', 'street', 'postcode'} and (not competing) and (not uncorroborated) and (status != 'same_object_coordinate_variation') else None
    return dict(precision=precision, resolution_status=status, allocation_precision=allocated, selected_candidate_ids=sorted({c['candidate_id'] for c in best}), selected_coordinate=coordinate, selected_object_count=len(objects), coordinate_candidate_id=coordinate_candidate, coordinate_selection_method='first_qualifying_same_address_result' if same_address and coordinate is not None else 'single_returned_coordinate' if coordinate is not None else None, selection_tier=tier, selection_audit=audit)
