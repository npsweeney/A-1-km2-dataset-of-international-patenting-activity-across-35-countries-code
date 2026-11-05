"""Prepare geographic routing with country and patent-year metadata"""
from __future__ import annotations
from collections.abc import Mapping
COUNTRY_FIELDS = ('observation_id', 'ctry_code', 'country_group')
TEMPORAL_FIELDS = ('family_id', 'location_evidence_id', 'observation_id', 'appln_id', 'person_id', 'actor', 'patent_year', 'patent_year_status')

def temporal_link_key(row: Mapping[str, object]) -> tuple[str, ...]:
    """Read the application and person identity fields"""
    values = tuple((row.get(field) for field in TEMPORAL_FIELDS[:6]))
    if any((not isinstance(value, str) for value in values)):
        raise ValueError('temporal child identity fields must be literal source strings')
    if any((not value for index, value in enumerate(values) if index != 4)):
        raise ValueError('temporal child identity is invalid')
    if values[5] not in {'applicant', 'inventor'}:
        raise ValueError('temporal child actor is invalid')
    return values
