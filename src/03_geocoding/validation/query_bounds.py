"""Construct region-bounded Nominatim queries"""
from geocoding.geocoder.queries import cleaned_query_address, structured_city, country_fallback_variants, austrian_base_house_variants
import re
from cleaning.clean_addresses import prepare_address
from geocoding.validation.address_evidence import source_street_components

def variants(row):
    cleaned, _ = cleaned_query_address(row)
    country = row['ctry_code']
    prepared = prepare_address(row)
    city = structured_city(row)
    houses, road = source_street_components(row)
    result = []
    if cleaned.strip():
        result.append(('address', ', '.join((p for p in [cleaned, country] if p))))
    if road and houses:
        for house in houses:
            result.append((f'address_range_member_{house}' if len(houses) > 1 else 'address_structured', ', '.join((p for p in [f'{road} {house}', prepared.postcode, city, country] if p))))
    result.extend(austrian_base_house_variants(row))
    country_variants = country_fallback_variants(row)
    result.extend(((name, query) for name, query in country_variants if name.startswith('address_')))
    if road:
        result.append(('street_structured', ', '.join((p for p in [road, prepared.postcode, city, country] if p))))
    result.extend(((name, query) for name, query in country_variants if not name.startswith('address_')))
    if prepared.postcode:
        result.append(('postcode_structured', ', '.join((p for p in [prepared.postcode, city, country] if p))))
    if prepared.postcode:
        result.append(('postcode_country', f'{prepared.postcode}, {country}'))
    if country == 'GB' and 'gb_postcode_syntax_conflict' in prepared.rules:
        compact = re.sub('\\s', '', prepared.postcode).upper()
        alternative = compact[:-3] + ' 0' + compact[-2:]
        result.append(('postcode_gb_inward_zero_hypothesis', f'{alternative}, GB'))
    return list(dict.fromkeys(result))

# Attach the applicable regional bounds to each query variant
def attach_bounds(rows, index):
    boxes_by_code = {}
    for feature in index.features:
        boxes_by_code.setdefault(feature.code, []).append(feature.bbox)
    for row in rows:
        if row['region_resolution_status'] != 'ready':
            continue
        expected = set(row['resolved_expected_tl3'].split(';'))
        boxes = [box for code in sorted(expected) for box in boxes_by_code.get(code, [])]
        if not boxes:
            raise ValueError('Ready source region has no bounding geometry')
        row['_region_viewbox'] = (min((b[0] for b in boxes)), min((b[1] for b in boxes)), max((b[2] for b in boxes)), max((b[3] for b in boxes)))
