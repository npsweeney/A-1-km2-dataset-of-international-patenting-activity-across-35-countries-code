"""Deterministic staged Nominatim queries derived from preserved observations"""
from __future__ import annotations
import re
from cleaning.clean_addresses import ROAD, prepare_address
from geocoding.validation.address_evidence import US_ADMIN_CODES, US_ADMIN_NAMES, compact, normalize, source_street_components
QUERY_UNIT_COMPONENT_RE = re.compile('^(?:suite|unit|floor|room|apt|apartment|appartment|\\d+(?:st|nd|rd|th)\\s+(?:floor|level))\\b', re.IGNORECASE)
INLINE_UNIT_SUFFIX_RE = re.compile('\\s*[,;:/-]?\\s*(?:(?:suite|unit|floor|room|apt|apartment|appartment)\\s*[#.: -]*[A-Za-z0-9-]+|\\d+(?:st|nd|rd|th)\\s+(?:floor|level)(?:\\s*[#.: -]+[A-Za-z0-9-]+)?)\\s*\\)?\\s*$', re.IGNORECASE)
US_CA_ADMIN_SUFFIXES = {'ab', 'ak', 'al', 'ar', 'az', 'bc', 'ca', 'co', 'ct', 'dc', 'de', 'fl', 'ga', 'hi', 'ia', 'id', 'il', 'in', 'ks', 'ky', 'la', 'ma', 'mb', 'md', 'me', 'mi', 'mn', 'mo', 'ms', 'mt', 'nb', 'nc', 'nd', 'ne', 'nh', 'nj', 'nl', 'nm', 'ns', 'nt', 'nu', 'nv', 'ny', 'oh', 'ok', 'on', 'or', 'pa', 'pe', 'qc', 'ri', 'sc', 'sd', 'sk', 'tn', 'tx', 'ut', 'va', 'vt', 'wa', 'wi', 'wv', 'wy', 'yt', 'alberta', 'british columbia', 'california', 'florida', 'iowa', 'maryland', 'massachusetts', 'new jersey', 'new york', 'north carolina', 'nova scotia', 'ontario', 'pennsylvania', 'quebec', 'tennessee', 'texas', 'virginia', 'washington'}
US_CA_ADMIN_SUFFIXES.update((code.lower() for code in US_ADMIN_CODES))
US_CA_ADMIN_SUFFIXES.update((name.replace('_', ' ') for name in US_ADMIN_NAMES))
US_CA_ADMIN_SUFFIXES.update({'newfoundland and labrador', 'new brunswick', 'prince edward island', 'northwest territories', 'nunavut', 'yukon'})

def structured_city(row: dict[str, str]) -> str:
    postal_pattern = '(?i)\\b[A-Z]\\d[A-Z]\\s*\\d[A-Z]\\d\\b' if row.get('ctry_code', '').upper() == 'CA' else '\\b\\d{5}(?:-\\d{4})?\\b'
    locality = prepare_address(row).city.strip()
    if row.get('ctry_code', '').upper() == 'GB' and (not locality or re.fullmatch('(?i)(?:street|road|avenue|lane|drive|court)', locality)):
        parts = [p.strip() for p in prepare_address(row).text.split(',')]
        if len(parts) >= 3 and prepare_address(row).postcode:
            city_part = parts[-2]
            if city_part and (not re.search('\\d', city_part)) and (not ROAD.search(city_part)):
                return city_part
    if row.get('ctry_code', '').upper() not in {'US', 'CA'}:
        return locality
    address_parts = [part.strip() for part in prepare_address(row).text.split(',')]
    if len(address_parts) >= 3:
        trailing_admin = re.sub(postal_pattern, '', address_parts[-1]).strip()
        if normalize(trailing_admin) in US_CA_ADMIN_SUFFIXES and (not re.search('\\d', address_parts[-2])):
            return address_parts[-2]
    if len(address_parts) >= 2:
        tail = re.sub(postal_pattern, '', address_parts[-1]).strip()
        tail_tokens = tail.split()
        for width in (2, 1):
            if len(tail_tokens) > width and normalize(' '.join(tail_tokens[-width:])) in US_CA_ADMIN_SUFFIXES:
                city_text = ' '.join(tail_tokens[:-width])
                if normalize(city_text) not in US_CA_ADMIN_SUFFIXES and (not re.search('\\d', city_text)):
                    return city_text
    tokens = locality.replace(',', ' ').split()
    for width in (2, 1):
        suffix = normalize(' '.join(tokens[-width:]))
        if len(tokens) > width and suffix in US_CA_ADMIN_SUFFIXES:
            return ' '.join(tokens[:-width])
    return locality

def remove_unit_details(query_address: str, row: dict[str, str]) -> str:
    """Remove unit detail after parsing the source house number and road"""
    house_members, road = source_street_components(row)
    if not house_members or not road:
        return query_address
    cleaned: list[str] = []
    for part in query_address.split(','):
        candidate = part.strip()
        if any((member in normalize(candidate).split() for member in house_members)) and normalize(road) in normalize(candidate):
            candidate = INLINE_UNIT_SUFFIX_RE.sub('', candidate).strip()
        if candidate and (not QUERY_UNIT_COMPONENT_RE.match(candidate)):
            cleaned.append(candidate)
    return ', '.join(cleaned)

def cleaned_query_address(row: dict[str, str]) -> tuple[str, list[str]]:
    """Prepare the query address and retain its cleaning audit"""
    from cleaning.clean_addresses import cleaned_stage
    saved = cleaned_stage(row)
    if saved is not None:
        return (saved[1], list(saved[2]))
    prepared = prepare_address(row)
    value = prepared.text
    applied = list(prepared.rules)
    without_units = remove_unit_details(value, row)
    if without_units != value:
        applied.append('strip_units_after_confirmed_source_house_and_road')
        value = without_units
    houses, road = source_street_components(row)
    if road and compact(road) in compact(row.get('address', '')) and (compact(road) not in compact(value)):
        house = houses[0] if len(houses) == 1 else f'{houses[0]}-{houses[-1]}' if houses else ''
        value = f'{house} {road}, {value}'.strip(' ,')
        applied.append('restore_source_street_removed_by_cleaning')
    trimmed = value.strip(' ,')
    if trimmed != value:
        applied.append('trim_separators')
    return (trimmed, applied)

# Build the ordered query variants from the prepared address fields

def structured_query_parameters(row: dict[str, str], variant: str, query: str) -> dict[str, str] | None:
    """Return true Nominatim structured-search fields for precise variants"""
    precise_variant = variant == 'street_structured' or variant == 'address_structured' or variant == 'address_at_base_house' or variant.startswith('address_straat_') or (variant == 'address_road_initial_removed') or variant.startswith('address_range_member_')
    if variant == 'postcode_structured':
        postal_code = prepare_address(row).postcode
        params = {'postalcode': postal_code, 'country': row.get('ctry_code', '')}
        if 'cleaned_address' in row:
            params['city'] = structured_city(row)
        return {key: value for key, value in params.items() if value}
    if not precise_variant:
        return None
    house_members, road = source_street_components(row)
    house = house_members[0] if house_members else ''
    if variant == 'address_at_base_house':
        if not austrian_base_house_variants(row):
            raise ValueError('Austrian base-house query requires a numeric slash address')
        house = house.split('/', 1)[0]
    if variant.startswith('address_range_member_'):
        house = variant.removeprefix('address_range_member_')
    street = road if variant == 'street_structured' else f'{house} {road}'.strip()
    if variant == 'address_road_initial_removed' or variant.startswith('address_straat_'):
        street = query.split(',', 1)[0].strip()
    params = {'street': street, 'city': structured_city(row), 'postalcode': prepare_address(row).postcode, 'country': row.get('ctry_code', '')}
    return {key: value for key, value in params.items() if value}

def austrian_base_house_variants(row):
    """Try the Austrian building number while retaining raw internal slash details"""
    houses, road = source_street_components(row)
    if row.get('ctry_code', '').upper() != 'AT' or not road or len(houses) != 1:
        return []
    if not re.fullmatch('\\d{1,4}(?:/\\d{1,4}){1,2}', houses[0]):
        return []
    prepared = prepare_address(row)
    query = ', '.join((p for p in [f"{road} {houses[0].split('/', 1)[0]}", prepared.postcode, structured_city(row), 'AT'] if p))
    return [('address_at_base_house', query)]

def country_fallback_variants(row):
    """Construct country-specific search variants"""
    prepared = prepare_address(row)
    country = row['ctry_code'].upper()
    _, road = source_street_components(row)
    output = []
    if country == 'CL' and re.match('(?i)^Nva\\.\\s+', prepared.text):
        output.append(('address_cl_nueva', re.sub('(?i)^Nva\\.\\s+', 'Nueva ', prepared.text) + ', CL'))
    if country == 'IE' and re.match('(?i)^apt\\.?\\s+\\d+\\s+\\S', prepared.text):
        output.append(('address_ie_apartment_label_omitted', re.sub('(?i)^apt\\.?\\s+', '', prepared.text) + ', IE'))
    if country == 'US' and re.match('^1\\s+[A-Za-z]', prepared.text):
        output.append(('address_us_one', re.sub('^1\\s+', 'One ', prepared.text) + ', US'))
    from geocoding.validation.address_evidence import dutch_designator_alternatives
    houses, _ = source_street_components(row)
    if len(houses) == 1:
        for name, alternative in dutch_designator_alternatives(road, country):
            output.append(('address_' + name, ', '.join((p for p in [f'{houses[0]} {alternative}', prepared.postcode, structured_city(row), country] if p))))
    if country == 'GB' and road and ('gb_weak_locality_query_variant' in prepared.rules):
        if houses:
            for house in houses:
                output.append(('address_gb_locality_omitted', ', '.join((x for x in [f'{road} {house}', prepared.postcode, 'GB'] if x))))
        else:
            output.append(('street_gb_locality_omitted', ', '.join((x for x in [road, prepared.postcode, 'GB'] if x))))
    if country == 'GB' and road and prepared.postcode and structured_city(row):
        city = structured_city(row)
        for house in houses:
            output.append(('address_gb_without_postcode', f'{house} {road}, {city}, GB'))
        output.append(('street_gb_without_postcode', f'{road}, {city}, GB'))
    if country == 'CA' and road and prepared.postcode:
        provinces = 'Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Nova Scotia|Ontario|Prince Edward Island|Quebec|Québec|Saskatchewan|Northwest Territories|Nunavut|Yukon'
        province = re.search('(?i)\\b(' + provinces + ')\\b', prepared.text)
        if province:
            if len(houses) == 1:
                output.append(('address_ca_province_without_postcode', f'{houses[0]} {road}, {province[0]}, CA'))
            output.append(('street_ca_province_without_postcode', f'{road}, {province[0]}, CA'))
    if country == 'LV' and road and re.search('(?i)\\s+street$', road):
        name = re.sub('(?i)\\s+street$', '', road)
        output.append(('street_type_omitted', ', '.join((p for p in [name, prepared.postcode, prepared.city, country] if p))))
    if country == 'MX' and prepared.postcode:
        components = [part.strip() for part in prepared.text.split(',')]
        labelled = [part for part in components if re.match('(?i)^colonia\\s+', part)]
        if not labelled and len(components) >= 3 and road and re.search('\\d', components[0]):
            second = components[1]
            if re.fullmatch("[A-Za-zÀ-ÿ .'-]+", second):
                labelled = [second]
        for place in labelled:
            output.append(('neighbourhood_context', ', '.join((p for p in [place, prepared.postcode, prepared.city, country] if p))))
    return output
