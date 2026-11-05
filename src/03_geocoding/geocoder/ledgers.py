"""Record Nominatim queries and returned candidates"""
from __future__ import annotations
import json
from typing import Any

def query_ledger_record(*, observation_id: str, query_index: int, variant: str, query: str, meta: dict[str, Any], result_count: int, error: str | None) -> dict[str, Any]:
    return {'observation_id': observation_id, 'query_index': query_index, 'variant': variant, 'query': query, **meta, 'result_count': result_count, 'error': error}

# Save the returned object and its field agreement with the observation identifiers
def candidate_ledger_record(*, observation_id: str, query_index: int, query_variant: str, query_rank: int, raw_candidate: dict[str, Any]) -> dict[str, Any]:
    """Record a returned candidate with its query and source address"""
    lat = float(raw_candidate['lat'])
    lon = float(raw_candidate['lon'])
    candidate_id = json.dumps([observation_id, query_index, query_rank, raw_candidate.get('place_id')], ensure_ascii=False, separators=(',', ':'))
    return {'observation_id': observation_id, 'candidate_id': candidate_id, 'query_index': query_index, 'query_variant': query_variant, 'query_rank': query_rank, 'place_id': raw_candidate.get('place_id'), 'lat': lat, 'lon': lon, 'display_name': raw_candidate.get('display_name'), 'nominatim_type': raw_candidate.get('type'), 'nominatim_addresstype': raw_candidate.get('addresstype'), 'importance': raw_candidate.get('importance')}
