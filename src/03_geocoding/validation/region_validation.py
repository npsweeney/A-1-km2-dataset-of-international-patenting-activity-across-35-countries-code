"""Resolve source geography once, before Nominatim queries are constructed"""
from __future__ import annotations
import json
import re
from pathlib import Path
FIELDS = ('resolved_expected_tl3', 'region_resolution_status', 'region_resolution_method', 'region_resolution_evidence')

def expected_regions(row):
    value = row.get('resolved_expected_tl3', row.get('reg_code', ''))
    return {c.strip() for c in value.split(';') if c.strip()}

# Resolve the source region against the recorded boundary release
def resolve_source(row, boundary_codes, counties, *, boundary_vintage=2020):
    if boundary_vintage not in (2020, 2024):
        raise ValueError('Unsupported boundary vintage')
    source_codes = sorted({c.strip() for c in row.get('reg_code', '').split(';') if c.strip()})
    expected, issues, mapping = (set(), [], {})
    for code in source_codes:
        if code.endswith('ZZZ'):
            issues.append('source_region_unknown')
        elif row.get('ctry_code', '').upper() == 'US' and re.fullmatch('US\\d{5}', code):
            record = counties.get(code[2:], {})
            targets = record.get('expected_tl3', [])
            if record.get('status') != 'mapped' or not targets:
                issues.append('source_county_to_tl3_unresolved')
            elif not all((re.fullmatch('US\\d{3}', c) for c in targets)):
                raise ValueError('County crosswalk target is not an OECD US TL3 code')
            else:
                expected.update(targets)
            mapping[code] = record
        else:
            expected.add(code)
    if not source_codes:
        issues.append('source_region_unknown')
    missing = sorted(expected - set(boundary_codes))
    if missing:
        issues.append(f'expected_tl3_absent_from_{boundary_vintage}_boundary')
    status = 'ready' if not issues else ';'.join(sorted(set(issues)))
    evidence = {'source_codes': source_codes, 'expected_tl3': sorted(expected), 'missing_boundary_codes': missing, 'issues': sorted(set(issues)), 'county_mapping': mapping, 'boundary_vintage': boundary_vintage}
    return dict(zip(FIELDS, (';'.join(sorted(expected)), status, 'source_fips_to_oecd_tl3' if mapping else 'source_oecd_tl3', json.dumps(evidence, sort_keys=True))))

def attach_preflight(observations, manifest_path: Path, boundary_path: Path, boundary_codes):
    """Use the supplied boundary vintage and county lookup before searching"""
    manifest = json.loads(manifest_path.read_text())
    schemas = {'gpd_region_preflight_2020_v1': 2020, 'gpd_region_preflight_2024_v1': 2024}
    vintage = schemas.get(manifest.get('schema_version'), manifest.get('boundary_vintage'))
    if vintage not in (2020, 2024) or manifest.get('full_shapefile_verified', True) is not True or manifest.get('boundary_codes', sorted(boundary_codes)) != sorted(boundary_codes):
        raise ValueError('Region preflight does not match the full verified versioned boundary')
    crosswalk_file = manifest.get('county_crosswalk_file')
    counties = {}
    if crosswalk_file:
        crosswalk_path = manifest_path.parent / crosswalk_file
        payload = crosswalk_path.read_bytes()
        counties = json.loads(payload)['counties']
    if vintage == 2024 and manifest.get('source_bindings'):
        _verify_2024_lookup(manifest, counties)
    for row in observations:
        row.update(resolve_source(row, boundary_codes, counties, boundary_vintage=vintage))

def _verify_2024_lookup(manifest, counties):
    import csv
    bindings = manifest.get('source_bindings', {})
    for name in ('REGPAT_REGIONS.txt', 'REGPAT_REGIONS_US.txt'):
        binding = bindings.get(name, {})
        path = Path(binding.get('path', ''))
        if not path.is_file() or path.stat().st_size != binding.get('bytes', path.stat().st_size if path.is_file() else 0):
            raise ValueError('Missing or changed TL2024 source lookup')
    allowed = set()
    with Path(bindings['REGPAT_REGIONS_US.txt']['path']).open(encoding='utf-8-sig') as stream:
        for source in csv.DictReader(stream, delimiter='|'):
            code, target = (source['reg_code'], source['reg_tl3'])
            if not re.fullmatch('US\\d{5}', code):
                continue
            allowed.add(code[2:])
            record = counties.get(code[2:])
            expected = [target] if re.fullmatch('US\\d{3}', target) else []
            if record is None or record.get('expected_tl3') != expected:
                raise ValueError('TL2024 county mapping differs from supplied REGPAT lookup')
            if record.get('status') != ('mapped' if expected else 'unmapped'):
                raise ValueError('TL2024 county mapping status differs from supplied lookup')
    if set(counties) != allowed:
        raise ValueError('TL2024 crosswalk includes counties absent from the supplied lookup')
