"""Classify filing years and summarise application-year links"""
from __future__ import annotations
CANONICAL_YEAR_FIELD = 'patent_year'
MISSING_STATUS = 'missing'
VALID_STATUS = 'valid'
INVALID_STATUS = 'invalid'

def classify_patent_year(value: object) -> tuple[int | None, str]:
    """Classify the supplied filing year"""
    if value is None or value == '':
        return (None, MISSING_STATUS)
    if isinstance(value, bool):
        return (None, INVALID_STATUS)
    if isinstance(value, int):
        year = value
    elif isinstance(value, str) and len(value) == 4 and value.isascii() and value.isdigit():
        year = int(value)
    else:
        return (None, INVALID_STATUS)
    if year < 1000 or year == 9999:
        return (None, INVALID_STATUS)
    return (year, VALID_STATUS)


# Resolve the filing year while retaining missing and conflicting date evidence
def resolve_filing_year(patstat_value=None, ops_evidence=None):
    """Prefer a valid PATSTAT filing year; otherwise use exact OPS application evidence"""
    year, status = classify_patent_year(patstat_value)
    ops_evidence = ops_evidence or {}
    ops_year, ops_status = classify_patent_year(ops_evidence.get('filing_year'))
    ops_valid = ops_evidence.get('status') == 'valid' and ops_status == VALID_STATUS
    if status == VALID_STATUS:
        return {'patent_year': year, 'patent_year_status': VALID_STATUS, 'year_source': 'patstat_tls201_appln', 'ops_year_conflict': bool(ops_valid and ops_year != year)}
    if ops_valid:
        return {'patent_year': ops_year, 'patent_year_status': VALID_STATUS, 'year_source': 'epo_ops_application_reference', 'date_evidence': ops_evidence, 'patstat_year_status': status}
    return {'patent_year': None, 'patent_year_status': status, 'year_source': None, 'ops_date_status': ops_evidence.get('status', 'missing')}
