"""Route geocoded locations to address, street or postcode support"""
from __future__ import annotations
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
CONTRACT_VERSION = '0.2.0'
from run import COUNTRIES
ROUTES = {'address', 'street', 'postcode'}
WITHHELD = {'excluded', 'abstained', 'unresolved', 'withheld', 'rejected'}
RUNNER_FIELDS = tuple(['validation_id', 'observation_id', 'candidate_id', 'route', 'source', 'role', 'country_code', 'observed_address', 'observed_house_number', 'observed_road', 'observed_postcode', 'observed_locality', 'observed_region', 'observed_latitude', 'observed_longitude', 'release_id', 'contract_version', 'decision_status', 'decision_reason', 'granularity', 'granularity_reason', 'source_granularity', 'nominatim_granularity', 'precision_limited_by', 'precision_limit_reason', 'component_conflicts', 'coordinate_ambiguity', 'ambiguity_distance_m', 'house_range_coverage', 'expected_tl3', 'derived_tl3', 'validation_method', 'us_county_expected', 'us_county_derived', 'us_county_match', 'source_postcode', 'query_postal_code', 'query_postal_code_method', 'nominatim_postcode_selected', 'postcode_disagreement', 'candidate_country_match', 'candidate_house_match', 'candidate_road_expected', 'candidate_road_candidate', 'candidate_road_match', 'candidate_road_match_method', 'candidate_postcode_match', 'candidate_postcode_match_method', 'candidate_locality_expected', 'candidate_locality_candidate', 'candidate_locality_field', 'candidate_locality_match', 'candidate_locality_match_method', 'candidate_addresstype', 'candidate_admin_area_expected', 'candidate_admin_area_candidate', 'candidate_admin_area_match', 'candidate_premise_expected', 'candidate_premise_candidate', 'candidate_premise_match', 'selected_query_id', 'query_index', 'query_variant', 'query_rank'])
ROUTING_FIELDS = tuple(['validation_id', 'observation_id', 'route', 'status', 'withholding_reason', 'seed_candidate_id', 'seed_method', 'seed_postcode_relation', 'seed_locality_relation', 'seed_tl3_pass', 'seed_county_pass', 'country_gate_pass', 'routed_mass', 'withheld_mass', 'started_at_utc', 'finished_at_utc', 'runtime_ms'])

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f'refusing to overwrite {path}')
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# Write temporal links to the geographic route selected for each observation
def route_rows(rows: Sequence[Mapping[str, object]], run_id: str, *, countries: Sequence[str]=COUNTRIES) -> list[dict[str, object]]:
    now = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
    output = []
    for row in rows:
        vid = str(row.get('validation_id', '')).strip()
        route = str(row.get('route', '')).strip()
        status = str(row.get('decision_status', '')).casefold()
        reason = str(row.get('decision_reason', '')).strip()
        country_ok = row.get('country_code') in countries
        candidate = str(row.get('candidate_id', '')).strip()
        query = str(row.get('selected_query_id', '')).strip()
        accepted = status not in WITHHELD and country_ok and (route in ROUTES) and bool(candidate and query)
        if accepted and route == 'address':
            accepted = bool(str(row.get('observed_latitude', '')).strip() and str(row.get('observed_longitude', '')).strip())
        output.append({'validation_id': vid, 'observation_id': str(row.get('observation_id', '')).strip(), 'route': ('address_passthrough' if route == 'address' else f'{route}_support_request') if accepted else 'withheld', 'status': 'accepted' if accepted else 'withheld', 'withholding_reason': '' if accepted else reason or 'upstream_not_accepted', 'seed_candidate_id': candidate if accepted else '', 'seed_method': str(row.get('validation_method', '')) if accepted else '', 'seed_postcode_relation': str(row.get('candidate_postcode_match_method', '')) if accepted else '', 'seed_locality_relation': str(row.get('candidate_locality_match_method', '')) if accepted else '', 'seed_tl3_pass': '1' if row.get('candidate_admin_area_match') in (True, 'true', '1') else '0', 'seed_county_pass': '1' if str(row.get('us_county_match', '')).casefold() == 'true' else '0', 'country_gate_pass': '1' if country_ok else '0', 'routed_mass': '1' if accepted else '0', 'withheld_mass': '0' if accepted else '1', 'started_at_utc': now, 'finished_at_utc': now, 'runtime_ms': 0})
    if len({row['validation_id'] for row in output}) != len(output):
        raise ValueError('routing IDs must be unique')
    if any((not row['observation_id'] for row in output)):
        raise ValueError('routing observation IDs must be nonblank')
    return output
