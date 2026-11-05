"""Geocode REGPAT addresses and select matching locations"""
from __future__ import annotations
import csv
import json
import os
import platform
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from geocoding import __version__
from cleaning.clean_addresses import CLEANING_FIELDS, RULES_VERSION, prepare_address
from geocoding.compare_fields import assess as assess_agreement, AGREEMENT_VERSION
from geocoding.select_location import select_candidates, SELECTION_VERSION
from geocoding.validation.region_validation import FIELDS as REGION_FIELDS, attach_preflight, expected_regions
from geocoding.validation.query_bounds import attach_bounds, variants as bounded_variants
from geocoding.audit import RunGuard
from geocoding.validation.address_evidence import assess_source_granularity, evaluate_candidate, expected_house_members, house_range_size, source_admin_area, source_named_premise, source_street_components
from geocoding.geocoder import ADDRESS_KEY_FIELDS as EVIDENCE_ADDRESS_KEY_FIELDS
from geocoding.geocoder import INPUT_FIELDS, candidate_ledger_record, query_ledger_record, read_observations, structured_query_parameters
from geocoding.geocoder import NominatimClient as EvidenceNominatimClient
from geocoding.geocoder import RequestBudgetExceeded
from geocoding.geocoder import NominatimQueryError as EvidenceNominatimQueryError
from geocoding.geocoder import clean_row as evidence_clean_row
from geocoding.geocoder import client as evidence_client
from geocoding.geocoder import stable_id as evidence_stable_id
from geocoding.geocoder.queries import cleaned_query_address
from geocoding.validation.spatial_checks import TL3Index
US_COUNTY_CODE_RE = re.compile('US\\d{5}')
EXCLUDED_COUNTRIES = {'IL': 'israel', 'JP': 'japan', 'KR': 'south_korea'}
MATERIAL_COORDINATE_DIFFERENCE_METRES = 100.0
NominatimClient = EvidenceNominatimClient
NominatimQueryError = EvidenceNominatimQueryError
ADDRESS_KEY_FIELDS = EVIDENCE_ADDRESS_KEY_FIELDS
clean_row = evidence_clean_row
stable_id = evidence_stable_id
urllib = evidence_client.urllib





def evaluate_with_agreement(row, raw, minimum_score):
    """One qualification stage used by both serial and parallel querying"""
    ev = evaluate_candidate(row, raw, minimum_score)
    assessment = assess_agreement(row, dict(raw_candidate=raw, candidate_id='pending', lat=float(raw['lat']), lon=float(raw['lon']), tl3_match=True, assessed_granularity=ev['assessed_granularity']))
    selected = select_candidates([assessment])
    supported = selected['allocation_precision'] in {'address', 'street', 'postcode'}
    return {**ev, 'previous_assessed_granularity': ev['assessed_granularity'], 'assessed_granularity': assessment['precision'], 'agreement': assessment, 'eligible_address_evidence': supported, 'agreement_can_stop': supported}

def candidate_supports_stopping(evaluation: dict[str, Any]) -> bool:
    return bool(evaluation['agreement_can_stop'])


# Stop staged searches only when the acquired evidence meets the stopping rules
def can_stop_staged_queries(variants: list[tuple[str, str]], query_index: int, supported_granularities: set[str]) -> bool:
    """Check remaining precise queries and address-range members"""
    if not supported_granularities:
        return False
    remaining = [name for name, _ in variants[query_index + 1:]]
    if any((name.startswith('address_range_member_') for name in remaining)):
        return False
    if 'address' in supported_granularities:
        return True
    precise_remaining = any((name.startswith(('address', 'named_premise')) for name in remaining))
    if precise_remaining:
        return False
    ranks = {'address': 3, 'street': 2, 'postcode': 1, 'locality': 0}
    best = max((ranks.get(level, -1) for level in supported_granularities))
    pending = {'street_gb_without_postcode': 2, 'street_ca_province_without_postcode': 2, 'street_structured': 2, 'street_type_omitted': 2, 'street_gb_locality_omitted': 2, 'postcode_structured': 1, 'postcode_country': 1, 'postcode_gb_inward_zero_hypothesis': 1, 'neighbourhood_context': 0, 'locality': 0}
    return not any((pending.get(name, 3) > best for name in remaining))

# Fetch bounded groups concurrently, then evaluate in deterministic input/query order
def prefetch_staged_queries(observations: list[dict[str, str]], client: NominatimClient, *, limit: int, workers: int, index: TL3Index, us_county_index: TL3Index | None, minimum_score: float, max_house_range_members: int) -> dict[str, int]:
    """Run each row's complete staged query chain concurrently.

    Workers follow the same stopping rules as serial evaluation.
    Final decisions read the response cache in input order
    """

    def fetch(row: dict[str, str]) -> tuple[int, int, int]:
        country = row['ctry_code'].upper()
        if country in EXCLUDED_COUNTRIES:
            return (0, 0, 0)
        if row.get('region_resolution_status', 'ready') != 'ready':
            return (0, 0, 0)
        expected = expected_regions(row)
        is_us_county = country == 'US' and bool(expected) and all((US_COUNTY_CODE_RE.fullmatch(code) for code in expected))
        expected_us_counties = {code[2:] for code in expected} if is_us_county else set()
        expected_tl3 = expected.intersection(index.codes)
        queries = errors = 0
        variants = bounded_variants(row)
        supported_granularities: set[str] = set()
        for query_index, (variant, query) in enumerate(variants):
            queries += 1
            try:
                results, _ = client.search(query, row['ctry_code'], limit, structured=structured_query_parameters(row, variant, query), **{'viewbox': row['_region_viewbox']})
            except (NominatimQueryError, RequestBudgetExceeded):
                errors += 1
                results = []
            for raw in results:
                try:
                    lat, lon = (float(raw['lat']), float(raw['lon']))
                except (KeyError, TypeError, ValueError):
                    continue
                if is_us_county:
                    derived = us_county_index.locate(lon, lat) if us_county_index else []
                    region_match = bool(expected_us_counties.intersection(derived))
                else:
                    derived = index.locate(lon, lat)
                    region_match = bool(expected_tl3.intersection(derived))
                evaluation = evaluate_with_agreement(row, raw, minimum_score)
                if region_match and candidate_supports_stopping(evaluation):
                    supported_granularities.add(evaluation['assessed_granularity'])
            if can_stop_staged_queries(variants, query_index, supported_granularities):
                break
        stopped = int(bool(supported_granularities) and query_index < len(variants) - 1)
        return (queries, errors, stopped)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        from itertools import islice
        incoming = iter(observations)
        query_count = error_count = stopped_count = 0
        while batch := list(islice(incoming, 256)):
            for queries, errors, stopped in executor.map(fetch, batch):
                query_count += queries
                error_count += errors
                stopped_count += stopped
    return {'rows': len(observations), 'queries': query_count, 'successful': query_count - error_count, 'errors': error_count, 'stopped_after_success': stopped_count}

def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str], *, utf8_bom: bool=False) -> None:
    encoding = 'utf-8-sig' if utf8_bom else 'utf-8'
    with path.open('w', newline='', encoding=encoding) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def component_presence(row: dict[str, str], candidate: dict[str, Any] | None) -> dict[str, Any]:
    raw_candidate = (candidate or {}).get('raw_candidate') or {}
    candidate_address = raw_candidate.get('address') or {}
    source_house, source_road = source_street_components(row)
    source_premise = source_named_premise(row)
    source_admin = source_admin_area(row)
    values = {'source_house_present': bool(source_house), 'nominatim_house_present': bool(candidate_address.get('house_number')), 'source_road_present': bool(source_road), 'nominatim_road_present': bool((candidate or {}).get('road_candidate')), 'source_postcode_present': bool(row.get('postal_code')), 'nominatim_postcode_present': bool(candidate_address.get('postcode')), 'source_locality_present': bool(row.get('locality')), 'nominatim_locality_present': bool((candidate or {}).get('locality_candidate')), 'source_named_premise_present': bool(source_premise), 'nominatim_named_premise_present': bool((candidate or {}).get('premise_match')), 'named_premise_unconfirmed': bool(source_premise and (not (candidate or {}).get('premise_match'))), 'source_admin_area_present': bool(source_admin), 'nominatim_admin_area_present': bool((candidate or {}).get('admin_area_candidate'))}
    conflicts: list[str] = []
    if candidate is not None:
        for component in ('house', 'road', 'postcode', 'locality'):
            if values[f'source_{component}_present'] and values[f'nominatim_{component}_present'] and candidate.get(f'{component}_expected') and (not candidate.get(f'{component}_match', False)):
                conflicts.append(f'{component}_mismatch')
        if not candidate.get('country_match', False):
            conflicts.append('country_mismatch')
        if source_admin and candidate.get('admin_area_candidate') and (not candidate.get('admin_area_match')):
            conflicts.append('admin_area_mismatch')
    if candidate is None:
        reason = 'nominatim_no_candidate'
    else:
        granularity = candidate['assessed_granularity']
        if granularity == 'address':
            reason = 'subordinate_unit_coordinate_ambiguity' if candidate.get('house_match_kind') == 'base' else 'none'
        elif granularity == 'street':
            if not values['source_house_present']:
                reason = 'source_house_missing'
            elif not values['nominatim_house_present']:
                reason = 'nominatim_house_missing'
            else:
                reason = 'house_number_mismatch'
        elif granularity == 'postcode':
            if not values['source_road_present']:
                reason = 'source_road_missing'
            elif not values['nominatim_road_present']:
                reason = 'nominatim_road_missing'
            else:
                reason = 'road_component_mismatch'
        elif granularity == 'locality':
            if not values['source_postcode_present']:
                reason = 'source_postcode_missing'
            elif not values['nominatim_postcode_present']:
                reason = 'nominatim_postcode_missing'
            else:
                reason = 'postcode_mismatch'
        elif not values['source_locality_present']:
            reason = 'source_locality_missing'
        elif not values['nominatim_locality_present']:
            reason = 'nominatim_locality_missing'
        else:
            reason = 'locality_component_mismatch'
    return {**values, 'precision_limit_reason': reason, 'component_conflicts': ';'.join(conflicts), 'postcode_disagreement': bool(candidate and candidate.get('postcode_expected') and candidate.get('postcode_candidate') and (not candidate.get('postcode_match')))}

def house_range_audit(row: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, str]:
    members = expected_house_members(row.get('address', ''))
    if len(members) <= 1:
        return {'house_range_expected': '', 'house_range_matched': '', 'house_range_unresolved': '', 'house_range_coverage': 'not_applicable'}
    matched = sorted({str(candidate.get('house_candidate') or '') for candidate in candidates if candidate.get('tl3_match') and candidate.get('eligible_address_evidence') and (candidate.get('house_match_kind') == 'range_member')}, key=int)
    unresolved = [member for member in members if member not in matched]
    coverage = 'full' if not unresolved else 'partial' if matched else 'none'
    return {'house_range_expected': ';'.join(members), 'house_range_matched': ';'.join(matched), 'house_range_unresolved': ';'.join(unresolved), 'house_range_coverage': coverage}

def review_fields(row: dict[str, str], candidate: dict[str, Any] | None, decision: dict[str, Any]) -> dict[str, Any]:
    status = str(decision.get('status') or '')
    selected_candidate = candidate if status == 'matched' else None
    address = ((selected_candidate or {}).get('raw_candidate') or {}).get('address') or {}
    granularity = str(decision.get('granularity') or '')
    precision = str(decision.get('precision_limit_reason') or '')
    if status == 'excluded':
        explanation = 'Excluded before querying under the documented country policy.'
    elif decision.get('reason') == 'geocoder_query_error':
        explanation = 'Nominatim query failed; no conclusion about address availability or granularity can be drawn until retry.'
    elif decision.get('reason') in {'higher_detail_query_error', 'lower_granularity_after_higher_detail_query_error'}:
        explanation = 'A safely confirmed lower-granularity candidate was retained after a more detailed query failed; the query error remains flagged for retry.' if status == 'matched' else 'A broader candidate was returned after a more detailed query failed; the row is abstained until the detailed query is retried.'
    elif decision.get('reason') == 'house_range_exceeds_query_limit':
        explanation = 'The numeric house range exceeds the configured expansion limit; no query was made and the row requires explicit review.'
    elif decision.get('reason') == 'all_address_supported_candidates_outside_expected_tl3':
        explanation = "Nominatim returned address-supported candidates, but none lay inside REGPAT's expected region."
    elif decision.get('reason') == 'inside_tl3_but_address_evidence_too_weak':
        explanation = 'A candidate lay inside the expected region, but exact REGPAT-to-Nominatim component evidence was insufficient.'
    elif decision.get('reason') == 'unresolved_same_tier_ambiguity':
        explanation = 'No coordinate was selected because materially distinct candidates had equally supported evidence.'
    elif decision.get('reason') == 'same_road_multiple_segments_house_unconfirmed':
        explanation = 'Street evidence is preserved, but no coordinate was selected because the source house was unconfirmed across multiple segments of the same road.'
    elif decision.get('reason') == 'no_candidates':
        explanation = 'Nominatim returned no candidate for any staged query.'
    elif precision == 'house_range_partially_resolved':
        explanation = 'Only some house-range members resolved exactly inside the expected region; unresolved members are listed separately.'
    elif precision == 'house_range_unresolved':
        explanation = 'No house-range member resolved exactly; the accepted coordinate is only a broader supported fallback.'
    elif status != 'matched':
        explanation = 'No coordinate was accepted: ' + str(decision.get('reason') or 'review required')
    elif decision.get('source_house_present') and (not decision.get('nominatim_house_present')):
        explanation = f'REGPAT contains a house number, but Nominatim did not return one; accepted only at {granularity} level.'
    elif decision.get('source_road_present') and (not decision.get('nominatim_road_present')):
        explanation = f'REGPAT contains a road, but Nominatim did not return one; accepted only at {granularity} level.'
    elif status == 'matched' and granularity == 'address':
        explanation = 'Exact house and road evidence is supported and the coordinate lies inside the expected region.'
    elif status == 'matched':
        explanation = f'Accepted inside the expected region at conservative {granularity} granularity.'
    else:
        explanation = str(decision.get('reason') or 'Review required.')
    conflicts = str(decision.get('component_conflicts') or '')
    ambiguity = str(decision.get('coordinate_ambiguity') or '')
    range_coverage = str(decision.get('house_range_coverage') or '')
    needs_review = bool(status != 'matched' or granularity != 'address' or conflicts or ambiguity or (range_coverage in {'partial', 'none'}))
    missing_from_nominatim = ';'.join((component for component in ('house', 'road', 'postcode', 'locality') if decision.get(f'source_{component}_present') and (not decision.get(f'nominatim_{component}_present'))))
    expected_range = str(decision.get('house_range_expected') or '')
    house_range_result = 'expected=' + expected_range + '; matched=' + str(decision.get('house_range_matched') or 'none') + '; unresolved=' + str(decision.get('house_range_unresolved') or 'none') if expected_range else ''
    reason = str(decision.get('reason') or '')
    if status == 'matched' or reason in {'inside_tl3_but_address_evidence_too_weak', 'same_road_multiple_segments_house_unconfirmed'}:
        tl3_match_y_n = 'Y'
    elif reason in {'all_address_supported_candidates_outside_expected_tl3', 'all_candidates_outside_expected_tl3', 'no_candidate_satisfies_address_and_tl3'}:
        tl3_match_y_n = 'N'
    else:
        tl3_match_y_n = ''
    if status == 'excluded':
        failure_stage = 'excluded_country_policy'
    elif reason == 'geocoder_query_error':
        failure_stage = 'nominatim_query_error'
    elif reason in {'higher_detail_query_error', 'lower_granularity_after_higher_detail_query_error', 'retryable_higher_detail_query_failure'}:
        failure_stage = 'higher_detail_query_error'
    elif reason == 'no_candidates':
        failure_stage = 'nominatim_no_candidate'
    elif reason in {'all_address_supported_candidates_outside_expected_tl3', 'all_candidates_outside_expected_tl3'}:
        failure_stage = 'tl3_mismatch'
    elif reason == 'inside_tl3_but_address_evidence_too_weak':
        failure_stage = 'component_evidence_too_weak'
    elif reason == 'no_candidate_satisfies_address_and_tl3':
        failure_stage = 'candidate_and_tl3_validation'
    elif reason == 'house_range_exceeds_query_limit':
        failure_stage = 'house_range_guardrail'
    elif range_coverage in {'partial', 'none'}:
        failure_stage = 'nominatim_house_range_incomplete'
    elif ambiguity:
        failure_stage = 'candidate_ambiguity'
    elif missing_from_nominatim:
        failure_stage = 'nominatim_missing_detail'
    elif conflicts:
        failure_stage = 'component_comparison_or_candidate_conflict'
    else:
        failure_stage = 'none_detected'
    source_level = assess_source_granularity(row)
    confirmed_level = granularity if status == 'matched' or reason == 'same_road_multiple_segments_house_unconfirmed' else 'unresolved'
    source_rank = {'region': 0, 'locality': 1, 'postcode': 2, 'street': 3, 'address': 4}
    if status != 'matched' and confirmed_level == 'unresolved':
        precision_limited_by = 'unresolved'
    elif source_rank.get(confirmed_level, -1) < source_rank.get(source_level, -1):
        precision_limited_by = 'nominatim'
    elif source_level != 'address':
        precision_limited_by = 'source'
    else:
        precision_limited_by = 'neither'
    return {'included': 'yes' if status == 'matched' else 'no', 'raw_address': row.get('address', ''), 'raw_locality': row.get('locality', ''), 'raw_postal_code': row.get('postal_code', ''), 'raw_country': row.get('ctry_code', ''), 'raw_regpat_region': row.get('reg_code', ''), 'nominatim_display_name': (selected_candidate or {}).get('display_name', ''), 'nominatim_house_number': address.get('house_number', ''), 'nominatim_road': (selected_candidate or {}).get('road_candidate', ''), 'nominatim_postcode': address.get('postcode', ''), 'nominatim_locality': (selected_candidate or {}).get('locality_candidate', ''), 'computer_explanation': explanation, 'tl3_match_y_n': tl3_match_y_n, 'failure_stage': failure_stage, 'needs_human_review': needs_review, 'source_granularity': source_level, 'nominatim_granularity': confirmed_level, 'precision_limited_by': precision_limited_by, 'source_postcode': row.get('postal_code', ''), 'nominatim_postcode_selected': address.get('postcode', ''), 'postcode_disagreement': bool(decision.get('postcode_disagreement')), 'ambiguity_review_note': str(decision.get('ambiguity_review_note') or ''), 'missing_from_nominatim': missing_from_nominatim, 'nominatim_correct_detail_y_n': '' if status != 'matched' else 'N' if missing_from_nominatim else 'Y', 'house_range_result': house_range_result}

def normalized_review_fields(row, candidates, decision):
    """Format the selected candidates and their qualification fields"""
    selection = decision['selection']
    selected = {value for value in selection['selected_candidate_ids']}
    coordinate_id = selection.get('coordinate_candidate_id')
    chosen = next((c for c in candidates if c['candidate_id'] == coordinate_id), None) if coordinate_id else next((c for c in candidates if c['candidate_id'] in selected), None)
    decision.update(component_presence(row, chosen))
    fields = review_fields(row, chosen, decision)
    fields['nominatim_granularity'] = selection['precision']
    fields['tl3_match_y_n'] = ('Y' if any(c['tl3_match'] for c in candidates) else 'N') if candidates and row.get('region_resolution_status') == 'ready' else ''
    allocatable = selection['allocation_precision'] is not None
    fields['included'] = 'yes' if allocatable else 'no'
    fields['needs_human_review'] = fields['needs_human_review'] or not allocatable
    fields['computer_explanation'] = (
        f"Confirmed {selection['precision']} evidence inside the expected TL3; {selection['resolution_status']}."
        if allocatable else f"No allocatable selection: {selection['resolution_status']}.")
    if selection.get('coordinate_selection_method') == 'first_qualifying_same_address_result':
        fields['computer_explanation'] += ' First qualifying returned object supplies the coordinate; alternative objects remain in the audit.'
    if not allocatable:
        fields['failure_stage'] = selection['resolution_status']
        fields['nominatim_correct_detail_y_n'] = ''
    return fields

PROCESSING_AUDITS = ('address_preparation.jsonl', 'queries.jsonl', 'candidates.jsonl', 'retryable_failures.jsonl')


# Restore the committed address prefix before continuing the saved run
def _processing_resume(store, output, identity):
    store.execute('CREATE TABLE IF NOT EXISTS processing_checkpoint(payload TEXT)')
    saved = store.execute('SELECT payload FROM processing_checkpoint').fetchone()
    checkpoint = json.loads(saved[0]) if saved else None
    if checkpoint and checkpoint['input_identity'] != identity:
        raise ValueError('processing checkpoint belongs to different input evidence')
    count = checkpoint['decisions']['count'] if checkpoint else 0
    if checkpoint:
        store.execute('DELETE FROM decisions WHERE seq>?', (count,))
        if store.execute('SELECT count(*) FROM decisions').fetchone()[0] != count:
            raise ValueError('committed decision checkpoint is incomplete')
    else:
        store.execute('DROP TABLE IF EXISTS decisions')
    for name in PROCESSING_AUDITS:
        path, offset = output / name, checkpoint['audit_bytes'][name] if checkpoint else 0
        if offset and (not path.is_file() or path.stat().st_size < offset):
            raise ValueError('committed audit checkpoint is incomplete')
        if path.exists():
            with path.open('r+b') as handle:
                handle.truncate(offset)
    store.commit()
    return checkpoint


# Reuse compatible responses and checkpoints; record all candidates before selecting locations
def run(*, input_path: Path, source_links_path: Path | None=None, boundary_path: Path, output_dir: Path, tl3_property: str, endpoint: str, cache_dir: Path, user_agent: str, limit: int=5, minimum_score: float=0.35, delay_seconds: float=0.0, workers: int=8, timeout_seconds: float=31.0, max_attempts: int=2, retry_backoff_seconds: float=2.0, max_in_flight: int=8, request_budget: int | None=None, max_house_range_members: int=10, max_rows: int | None=None, us_county_boundary_path: Path | None=None, us_county_property: str='GEOID', region_preflight: Path | None=None, regional_only: bool=True, normalized_agreement: bool=True, prepare_inputs_only: bool=False, stop_file: Path | None=None, service_identity_path: Path | None=None) -> dict[str, Any]:
    if not regional_only or not normalized_agreement:
        raise ValueError('Only the source-region bounded, normalized-agreement pipeline is supported')
    data_identity = None
    if service_identity_path is not None:
        service = json.loads(service_identity_path.read_text())
        if str(service.get('endpoint', '')).rstrip('/') != endpoint.rstrip('/'):
            raise ValueError('Geocoder endpoint differs from the supplied service identity')
        data_identity = {field: service.get(field) for field in ('database_system_identifier', 'data_revision', 'version')}
        if not all(str(value or '').strip() for value in data_identity.values()):
            raise ValueError('Geocoder service requires database identity, data revision and version')
    started = datetime.now(UTC)
    if region_preflight is None:
        raise ValueError('Bounded TL3-only mode requires a verified region preflight manifest')
    run_guard = RunGuard(output_dir)
    state_identity = {'schema_version': 2, 'input_path': str(input_path.resolve(strict=True)), 'source_links_path': str(source_links_path.resolve(strict=True)) if source_links_path else None, 'boundary_path': str(boundary_path.resolve(strict=True)), 'region_preflight_path': str(region_preflight.resolve(strict=True)) if region_preflight else None, 'county_boundary_path': str(us_county_boundary_path.resolve(strict=True)) if us_county_boundary_path else None, 'regional_only': regional_only, 'normalized_agreement': normalized_agreement, 'agreement_version': AGREEMENT_VERSION, 'selection_version': SELECTION_VERSION, 'address_rules_version': RULES_VERSION, 'endpoint': endpoint, 'tl3_property': tl3_property, 'us_county_property': us_county_property if us_county_boundary_path else None, 'user_agent': user_agent, 'candidate_limit': limit, 'minimum_score': minimum_score, 'delay_seconds': delay_seconds, 'workers': workers, 'timeout_seconds': timeout_seconds, 'max_attempts': max_attempts, 'retry_backoff_seconds': retry_backoff_seconds, 'max_in_flight': max_in_flight, 'request_budget': request_budget, 'max_house_range_members': max_house_range_members, 'max_rows': max_rows}
    state_identity['service_data_identity'] = data_identity
    run_guard.prepare(state_identity, started.isoformat())
    index = TL3Index.from_geojson(boundary_path, tl3_property)
    us_county_index = TL3Index.from_geojson(us_county_boundary_path, us_county_property) if us_county_boundary_path else None
    import sqlite3
    from ingest.prepare_observations import DiskRows
    state_path = output_dir / 'input-index.sqlite'
    input_identity = dict(schema_version=1, rules=RULES_VERSION, max_rows=max_rows,
                          regional_only=regional_only, sources={})
    for name, path in [('addresses', input_path), ('links', source_links_path),
                       ('boundaries', boundary_path), ('preflight', region_preflight)]:
        if path is not None:
            stat = path.stat()
            input_identity['sources'][name] = [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
    store = sqlite3.connect(state_path)
    store.execute('PRAGMA cache_size=-16384')
    store.execute('PRAGMA temp_store=FILE')
    store.execute('CREATE TABLE IF NOT EXISTS input_state(payload TEXT)')
    previous = store.execute('SELECT payload FROM input_state').fetchone()
    if previous:
        prepared = json.loads(previous[0])
        if prepared['identity'] != input_identity:
            raise ValueError('Saved input index belongs to changed sources or rules; use a new output')
        observations = DiskRows(store, 'observations', existing=prepared['observations'])
        source_rows = DiskRows(store, prepared['links_table'], existing=prepared['links'])
    else:
        store.execute('CREATE TABLE IF NOT EXISTS input_checkpoint(payload TEXT)')
        checkpoint = store.execute('SELECT payload FROM input_checkpoint').fetchone()
        checkpoint = json.loads(checkpoint[0]) if checkpoint else None
        if checkpoint:
            if checkpoint['identity'] != input_identity or source_links_path is None or max_rows is not None:
                raise ValueError('Partial input checkpoint belongs to different sources or settings')
            observations = DiskRows(store, 'observations', existing=checkpoint['observations'])
        else:
            # Only incomplete, derived index tables are rebuilt; never touch response caches
            for table in ('observations', 'address_links', 'source_links', 'decisions', 'linked'):
                store.execute(f'DROP TABLE IF EXISTS {table}')
            with input_path.open(newline='', encoding='utf-8-sig') as handle:
                reader = csv.DictReader(handle)
                if {'year_evidence', 'observation_id'}.issubset(reader.fieldnames or []) and source_links_path is None:
                    raise ValueError('Deduplicated intake requires --source-links to retain application links')
                observation_batch = read_observations(reader, max_rows=max_rows, store=store, retain_links=source_links_path is None)
            observations = observation_batch.observations
            source_rows = observation_batch.address_links
            if source_links_path is not None and max_rows is None:
                checkpoint = dict(identity=input_identity,
                                  observations=dict(count=len(observations), fields=sorted(observations.fields)))
                store.execute('INSERT INTO input_checkpoint VALUES(?)', (json.dumps(checkpoint),))
                store.commit()
        if source_links_path is not None:
            from ingest.prepare_observations import read_source_links
            with source_links_path.open(newline='', encoding='utf-8-sig') as handle:
                source_rows = read_source_links(csv.DictReader(handle), observations, allow_unselected=max_rows is not None,
                                               store=store, resume=checkpoint is not None)
        if region_preflight is not None:
            attach_preflight(observations, region_preflight, boundary_path, index.codes)
            attach_bounds(observations, index)
        prepared = dict(identity=input_identity,
                        observations=dict(count=len(observations), fields=sorted(observations.fields)),
                        links=dict(count=len(source_rows), fields=sorted(source_rows.fields)),
                        links_table=source_rows.name)
        store.execute('INSERT INTO input_state VALUES(?)', (json.dumps(prepared),))
        store.commit()
    preparation = dict(state='inputs_prepared', counts=dict(observations=len(observations), source_rows=len(source_rows)),
                       source_paths_unchanged=True, input_identity=input_identity)
    (output_dir / 'input_preparation.json').write_text(json.dumps(preparation, indent=2) + '\n')
    if prepare_inputs_only:
        store.close()
        return preparation
    if region_preflight is not None:
        write_csv(output_dir / 'region_preflight.csv', observations, ['observation_id', 'ctry_code', 'reg_code', *REGION_FIELDS])
    client = NominatimClient(endpoint, cache_dir, user_agent, delay_seconds, timeout_seconds=timeout_seconds, max_attempts=max_attempts, retry_backoff_seconds=retry_backoff_seconds, max_in_flight=max_in_flight, request_budget=request_budget, event_ledger_path=output_dir / 'transport_events.jsonl', resume_events=True, data_identity=data_identity)
    processing = _processing_resume(store, output_dir, input_identity)
    prefetch = dict(processing['prefetch']) if processing else dict(rows=0, queries=0, successful=0, errors=0, stopped_after_success=0)
    if processing:
        client._telemetry.update(processing['telemetry'])
        events = output_dir / 'transport_events.jsonl'
        with events.open('rb') as handle:
            handle.seek(processing['transport_bytes'])
            # Preserve physical attempts made beyond the last committed address batch
            for line in handle:
                event = json.loads(line)
                if event['kind'] == 'network_attempt':
                    client._telemetry['attempted_requests'] += 1
                    client._telemetry['response_bytes'] += event.get('response_bytes', 0)
                    client._telemetry['http_503_count'] += event.get('status_code') == 503
                    client._telemetry['retry_count'] += event.get('attempt', 1) > 1
                elif event['kind'] in {'cache_hit', 'cache_write'}:
                    client._telemetry['cache_hits' if event['kind'] == 'cache_hit' else 'cache_writes'] += 1
    paused = False
    def staged_rows():
        from itertools import islice
        nonlocal paused
        completed = processing['decisions']['count'] if processing else 0
        incoming = (json.loads(payload) for payload, in store.execute('SELECT payload FROM observations WHERE seq>? ORDER BY seq', (completed,)))
        while True:
            if stop_file is not None and stop_file.exists():
                paused = True
                return
            batch = list(islice(incoming, 256))
            if not batch:
                return
            if workers > 1:
                result = prefetch_staged_queries(batch, client, limit=limit, workers=workers, index=index,
                    us_county_index=us_county_index, minimum_score=minimum_score,
                    max_house_range_members=max_house_range_members)
                for key in prefetch:
                    prefetch[key] += result[key]
            yield from batch
            for handle in (ah, qh, ch, rh):
                handle.flush()
                os.fsync(handle.fileno())
            checkpoint = dict(input_identity=input_identity,
                decisions=dict(count=len(decisions), fields=sorted(decisions.fields)),
                prefetch=dict(prefetch), candidate_count=candidate_count,
                stopped_after_success_count=stopped_after_success_count,
                telemetry=client.telemetry(),
                transport_bytes=(output_dir/'transport_events.jsonl').stat().st_size,
                audit_bytes={name:(output_dir/name).stat().st_size for name in PROCESSING_AUDITS})
            store.execute('DELETE FROM processing_checkpoint')
            store.execute('INSERT INTO processing_checkpoint VALUES(?)', (json.dumps(checkpoint),))
            store.commit()
            progress = dict(prefetch=prefetch, evaluated_addresses=len(decisions),
                            total_addresses=len(observations), telemetry=client.telemetry())
            temporary = output_dir / 'progress.tmp'
            temporary.write_text(json.dumps(progress, indent=2) + '\n')
            temporary.replace(output_dir / 'progress.json')

    if not processing:
        write_csv(output_dir / 'observations.csv', observations, ['observation_id', *INPUT_FIELDS, 'query_postal_code', 'query_postal_code_method', *(REGION_FIELDS if region_preflight else ()), *(CLEANING_FIELDS if any(('cleaned_address' in row for row in observations)) else ()), 'raw_input_json'])
        write_csv(output_dir / 'address_links.csv', source_rows, ['source_row_number', 'appln_id', 'person_id', 'observation_id', 'actor', 'year'] + sorted(source_rows.fields - {'source_row_number', 'appln_id', 'person_id', 'observation_id', 'actor', 'year'}))
    decisions = DiskRows(store, 'decisions', unique=True, existing=processing['decisions'] if processing else None)
    candidate_count = processing['candidate_count'] if processing else 0
    stopped_after_success_count = processing['stopped_after_success_count'] if processing else 0
    with (output_dir / 'address_preparation.jsonl').open('a') as ah, (output_dir / 'queries.jsonl').open('a') as qh, (output_dir / 'candidates.jsonl').open('a') as ch, (output_dir / 'retryable_failures.jsonl').open('a') as rh:
        for row in staged_rows():
            prepared = prepare_address(row)
            cleaned, rules = cleaned_query_address(row)
            houses, road = source_street_components(row)
            ah.write(json.dumps({'observation_id': row['observation_id'], 'rules_version': RULES_VERSION, 'cleaned_address': cleaned, 'street_parser_text': prepared.street_text, 'prepared_city': prepared.city, 'applied_rules': rules, 'house_members': houses, 'road': road}, ensure_ascii=False) + '\n')
            expected = expected_regions(row)
            country = row['ctry_code'].upper()
            if country in EXCLUDED_COUNTRIES:
                decision = {'observation_id': row['observation_id'], 'status': 'excluded', 'reason': 'excluded_country_policy', 'candidate_id': '', 'lat': '', 'lon': '', 'expected_tl3': ';'.join(sorted(expected)), 'derived_tl3': '', 'validation_method': 'excluded_country_policy', 'us_county_expected': '', 'us_county_derived': '', 'us_county_match': '', 'granularity': '', 'granularity_reason': '', 'coordinate_ambiguity': '', **house_range_audit(row, []), **component_presence(row, None), 'evidence_score': '', 'candidate_count': 0}
                decision.update(review_fields(row, None, decision))
                decisions.append(decision)
                continue
            range_size = house_range_size(prepare_address(row).street_text)
            is_us_county = country == 'US' and bool(expected) and all((US_COUNTY_CODE_RE.fullmatch(code) for code in expected))
            expected_us_counties = {code[2:] for code in row['reg_code'].split(';') if US_COUNTY_CODE_RE.fullmatch(code)} if region_preflight and country == 'US' else {code[2:] for code in expected} if is_us_county else set()
            validation_method = 'us_county_fips' if is_us_county else 'source_us_county_unknown' if country == 'US' and expected == {'USZZZ'} else 'oecd_tl3'
            expected_codes_in_boundary = expected_us_counties.intersection(us_county_index.codes) if is_us_county and us_county_index else set() if is_us_county else expected.intersection(index.codes)
            candidates: list[dict[str, Any]] = []
            last_error = ''
            query_errors: list[dict[str, Any]] = []
            variants = (bounded_variants(row)) if row.get('region_resolution_status', 'ready') == 'ready' else []
            for query_index, (variant, query) in enumerate(variants):
                try:
                    results, meta = client.search(query, row['ctry_code'], limit, structured=structured_query_parameters(row, variant, query), **{'viewbox': row['_region_viewbox']})
                    query_record = query_ledger_record(observation_id=row['observation_id'], query_index=query_index, variant=variant, query=query, meta=meta, result_count=len(results), error=None)
                except (NominatimQueryError, RequestBudgetExceeded) as exc:
                    results = []
                    last_error = f'{type(exc).__name__}: {exc}'
                    error_meta = getattr(exc, 'meta', {'url': None, 'cache_key': None, 'cache_hit': False, 'attempts': 1, 'attempt_errors': [last_error], 'retryable': True, 'status_code': None})
                    query_record = query_ledger_record(observation_id=row['observation_id'], query_index=query_index, variant=variant, query=query, meta=error_meta, result_count=0, error=last_error)
                    query_errors.append({'query_index': query_index, 'variant': variant, 'error': last_error, 'retryable': bool(error_meta.get('retryable'))})
                    if error_meta.get('retryable'):
                        rh.write(json.dumps({'observation_id': row['observation_id'], 'query_index': query_index, 'variant': variant, 'query': query, **error_meta, 'error': last_error}, ensure_ascii=False, sort_keys=True) + '\n')
                qh.write(json.dumps(query_record, ensure_ascii=False, sort_keys=True) + '\n')
                for rank, raw in enumerate(results):
                    try:
                        lat, lon = (float(raw['lat']), float(raw['lon']))
                    except (KeyError, TypeError, ValueError):
                        continue
                    derived = index.locate(lon, lat)
                    derived_us_counties = us_county_index.locate(lon, lat) if country == 'US' and us_county_index else []
                    region_match = bool(expected_us_counties.intersection(derived_us_counties)) if is_us_county else bool(expected_codes_in_boundary.intersection(derived))
                    evaluation = evaluate_with_agreement(row, raw, minimum_score)
                    acquired = candidate_ledger_record(observation_id=row['observation_id'], query_index=query_index, query_variant=variant, query_rank=rank, raw_candidate=raw)
                    record = {**acquired, 'expected_tl3': sorted(expected), 'source_region_codes': row['reg_code'].split(';'), 'region_resolution_method': row.get('region_resolution_method'), 'region_resolution_status': row.get('region_resolution_status'), 'derived_tl3': derived, 'expected_tl3_present_in_boundary': bool(expected_codes_in_boundary), 'tl3_match': region_match, 'validation_method': validation_method, 'us_county_expected': sorted(expected_us_counties), 'us_county_derived': derived_us_counties, 'us_county_match': region_match if is_us_county else bool(expected_us_counties.intersection(derived_us_counties)) if country == 'US' and us_county_index else None, **evaluation, 'raw_candidate': raw}
                    record['agreement'].update(candidate_id=record['candidate_id'], within_tl3=region_match)
                    ch.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
                    candidates.append(record)
                    candidate_count += 1
                supported_granularities = {candidate['assessed_granularity'] for candidate in candidates if candidate['tl3_match'] and candidate_supports_stopping(candidate)}
                if can_stop_staged_queries(variants, query_index, supported_granularities):
                    if query_index < len(variants) - 1:
                        stopped_after_success_count += 1
                    break
            regional = [c for c in candidates if c['tl3_match']]
            reason = 'candidate_inside_expected_tl3' if regional else row['region_resolution_status'] if row['region_resolution_status'] != 'ready' else 'geocoder_query_error' if last_error and (not candidates) else 'no_candidates' if not candidates else 'all_candidates_outside_expected_tl3'
            decision = {'observation_id': row['observation_id'], 'status': 'matched' if regional else 'abstained', 'reason': reason, 'candidate_id': ';'.join((c['candidate_id'] for c in regional)), 'lat': '', 'lon': '', 'expected_tl3': ';'.join(sorted(expected)), 'derived_tl3': ';'.join(sorted({t for c in regional for t in c['derived_tl3']})), 'validation_method': 'oecd_tl3', 'us_county_expected': ';'.join(sorted(expected_us_counties)), 'granularity': 'region' if regional else '', 'candidate_count': len(candidates)}
            decision.update(review_fields(row, None, decision))
            selected = select_candidates([c['agreement'] for c in candidates])
            if row.get('region_resolution_status') != 'ready':
                selected.update(precision='REGPAT unclassified' if 'unknown' in row.get('region_resolution_status', '') else 'Source region unavailable', allocation_precision=None, resolution_status='source_region_unavailable')
            decision['selection'] = selected
            decision['granularity'] = selected['precision']
            decision['candidate_id'] = ';'.join(selected['selected_candidate_ids'])
            if selected['selected_coordinate'] is not None:
                decision['lat'], decision['lon'] = selected['selected_coordinate']
            decision['allocation_precision'] = selected['allocation_precision'] or ''
            decision['resolution_status'] = selected['resolution_status']
            decision['derived_tl3'] = ';'.join(sorted({t for c in candidates if c['candidate_id'] in selected['selected_candidate_ids'] for t in c['derived_tl3']}))
            decision.update(normalized_review_fields(row, candidates, decision))
            decisions.append(decision)
            continue
    if paused:
        result = dict(paused=True, counts=dict(observations=len(observations), evaluated_addresses=len(decisions), source_rows=len(source_rows)), checkpoint='input-index.sqlite:processing_checkpoint')
        (output_dir/'pause_state.json').write_text(json.dumps(result, indent=2)+'\n')
        store.close()
        client._cache_connection.close()
        return result
    fields = ['observation_id', 'status', 'reason', 'candidate_id', 'lat', 'lon', 'expected_tl3', 'derived_tl3', 'validation_method', 'us_county_expected', 'us_county_derived', 'us_county_match', 'granularity', 'granularity_reason', 'coordinate_ambiguity', 'ambiguity_review_note', 'house_range_expected', 'house_range_matched', 'house_range_unresolved', 'house_range_coverage', 'source_house_present', 'nominatim_house_present', 'source_road_present', 'nominatim_road_present', 'source_postcode_present', 'nominatim_postcode_present', 'source_locality_present', 'nominatim_locality_present', 'source_named_premise_present', 'nominatim_named_premise_present', 'named_premise_unconfirmed', 'source_admin_area_present', 'nominatim_admin_area_present', 'precision_limit_reason', 'source_granularity', 'nominatim_granularity', 'precision_limited_by', 'source_postcode', 'nominatim_postcode_selected', 'postcode_disagreement', 'component_conflicts', 'evidence_score', 'candidate_count', 'raw_address', 'raw_locality', 'raw_postal_code', 'raw_country', 'raw_regpat_region', 'nominatim_display_name', 'nominatim_house_number', 'nominatim_road', 'nominatim_postcode', 'nominatim_locality', 'computer_explanation', 'tl3_match_y_n', 'failure_stage', 'included', 'nominatim_correct_detail_y_n', 'needs_human_review']
    fields.extend(['allocation_precision', 'resolution_status'])
    source_by_id = observations
    with (output_dir / 'selections.jsonl').open('w', encoding='utf-8') as handle:
        for decision in decisions:
            source = source_by_id.get(decision['observation_id'])
            selected = decision.get('selection')
            if selected is None:
                selected = select_candidates([])
            if source.get('region_resolution_status') != 'ready':
                selected.update(precision='REGPAT unclassified' if 'unknown' in source.get('region_resolution_status', '') else 'Source region unavailable', allocation_precision=None, resolution_status='source_region_unavailable')
            handle.write(json.dumps({'observation_id': decision['observation_id'], **selected}, ensure_ascii=False, sort_keys=True) + '\n')
    write_csv(output_dir / 'decisions.csv', decisions, fields)
    validation = ({**d, 'human_verdict_y_or_n_granularity': '', 'human_notes': ''} for d in decisions if d['status'] != 'excluded')
    review_csv_fields = ['raw_address', 'nominatim_display_name', 'source_granularity', 'nominatim_granularity', 'tl3_match_y_n', 'precision_limited_by', 'status', 'human_verdict_y_or_n_granularity', 'failure_stage', 'raw_regpat_region', 'derived_tl3', 'computer_explanation', 'nominatim_correct_detail_y_n', 'source_postcode', 'nominatim_postcode_selected', 'postcode_disagreement', 'component_conflicts', 'lat', 'lon', 'coordinate_ambiguity', 'ambiguity_review_note', 'house_range_result']
    write_csv(output_dir / 'human_validation.csv', validation, review_csv_fields + ['human_notes'], utf8_bom=True)
    counts = {'source_rows': len(source_rows), 'observations': len(observations), 'candidates': candidate_count, 'matched': sum((d['status'] == 'matched' for d in decisions)), 'abstained': sum((d['status'] == 'abstained' for d in decisions)), 'excluded': sum((d['status'] == 'excluded' for d in decisions)), 'expected_tl3_absent_from_boundary': sum((d['reason'] == 'expected_tl3_absent_from_boundary_version' for d in decisions)), 'house_range_exceeds_query_limit': sum((d['reason'] == 'house_range_exceeds_query_limit' for d in decisions))}
    telemetry = {**getattr(client, 'telemetry', lambda: {})(), 'stopped_after_success_count': stopped_after_success_count}
    telemetry_path = output_dir / 'client_telemetry.json'
    telemetry_path.write_text(json.dumps(telemetry, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    budget_exhausted = bool(telemetry.get('request_budget_exhausted'))
    manifest = {'schema_version': '1.0', 'decision_contract_version': '0.2.0', 'address_rules_version': RULES_VERSION, 'software_version': __version__, 'started_at_utc': started.isoformat(), 'finished_at_utc': datetime.now(UTC).isoformat(), 'python_version': platform.python_version(), 'inputs': {'full_source_links': str(source_links_path.resolve()) if source_links_path else None, 'regpat_csv': str(input_path.resolve()), 'region_preflight_manifest': str(region_preflight.resolve()) if region_preflight else None, 'tl3_boundaries': str(boundary_path.resolve()), 'tl3_property': tl3_property, 'us_county_boundaries': str(us_county_boundary_path.resolve()) if us_county_boundary_path else None, 'us_county_property': us_county_property if us_county_boundary_path else None}, 'configuration': {'selection_version': SELECTION_VERSION, 'address_object_tie_policy': 'first_qualifying_same_address_result', 'regional_only': regional_only, 'nominatim_region_bounded': regional_only, 'endpoint': endpoint, 'candidate_limit_per_query': limit, 'minimum_evidence_score': minimum_score, 'delay_seconds': delay_seconds, 'workers': workers, 'parallelism': 'bounded_256_row_prefetch_then_serial_deterministic_evaluation', 'timeout_seconds': timeout_seconds, 'request_attempts': max_attempts, 'retry_backoff_seconds': retry_backoff_seconds, 'max_in_flight': max_in_flight, 'request_budget': request_budget, 'max_house_range_members': max_house_range_members, 'material_coordinate_difference_metres': MATERIAL_COORDINATE_DIFFERENCE_METRES, 'max_rows': max_rows, 'query_strategy': 'staged_stop_after_supported_candidate', 'deduplication': 'exact_unique_address_locality_postcode_country_tl3', 'excluded_country_policy': {'country_codes': sorted(EXCLUDED_COUNTRIES), 'stage': 'before_candidate_and_query_generation', 'nominatim_requests': 0}}, 'field_meanings': {'source_granularity': 'detail present in the raw REGPAT source only', 'nominatim_granularity': 'detail confirmed jointly by Nominatim and exact source-component evidence', 'postcode_disagreement': 'source and Nominatim postcodes differ; neither value is overwritten', 'precision_limited_by': 'source, nominatim, neither, or unresolved', 'tl3_match_y_n': 'selected coordinate passes the existing REGPAT TL3 gate'}, 'counts': counts, 'partial': budget_exhausted, 'stop_reason': 'request_budget_exhausted' if budget_exhausted else None, 'production_consumable': not budget_exhausted, 'prefetch': prefetch, 'telemetry': telemetry}
    manifest['configuration']['service_data_identity'] = data_identity
    manifest['configuration']['query_variants'] = 'ordered variants recorded per observation in queries.jsonl'
    manifest['configuration']['stopping_evidence'] = 'in_tl3_supported_precision_with_no_finer_stage_pending'
    manifest['field_meanings'].update(
        status='matched means at least one candidate inside the expected TL3; inspect allocation_precision for allocatable evidence',
        granularity='precision from the saved normalized selection',
        nominatim_granularity='precision from the saved normalized selection, including unresolved source-region labels',
        candidate_id='semicolon-separated selected candidate IDs from selections.jsonl',
        included='yes when the saved selection has allocation_precision; geographic area validation can still withhold it',
        tl3_match_y_n='Y: at least one candidate inside expected TL3; N: returned candidates all outside; blank: no candidates or unavailable source region')
    manifest['decision_rules'] = ['resolve the source region before bounded queries',
        'compare source components using the recorded normalization rules',
        'select precision and allocation eligibility using the saved normalized selection audit',
        'retain regional membership separately from allocation eligibility',
        'street and postcode selections require downstream geographic area validation']
    manifest['output_files'] = sorted((p.name for p in output_dir.iterdir() if p.is_file() and p.name not in {'run_state.json', 'run_manifest.json'}))
    store.commit()
    store.close()
    run_guard.complete(manifest)
    return manifest
