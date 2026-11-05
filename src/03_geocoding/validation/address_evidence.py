"""Compare address components and classify match precision"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any
from cleaning.clean_addresses import ROAD, prepare_address
DASH_CHARS = '\\-‐‑‒–—'
HOUSE_RE = re.compile('(?<![A-Za-z0-9])(\\d+[a-zA-Z]?)(?=$|[^A-Za-z0-9])')
HOUSE_RANGE_RE = re.compile(f'\\b(\\d+)\\s*[{DASH_CHARS}]+\\s*(\\d+)\\b')
LEADING_HOUSE_UNIT_RE = re.compile(f'^\\s*(\\d+)(?:\\s*[/\\\\_]\\s*[A-Za-z0-9]+|\\s*[{DASH_CHARS}]+\\s*[A-Za-z][A-Za-z0-9]*)\\s*$')
TOKEN_RE = re.compile('[a-z0-9]+')
LOCALITY_KEYS = ('city', 'town', 'village', 'municipality', 'hamlet', 'suburb')
LOCALITY_GRANULARITY_FIELDS = {'city', 'town', 'village', 'municipality', 'hamlet'}
LOCALITY_RESULT_TYPES = {'city', 'town', 'village', 'municipality', 'hamlet', 'suburb', 'borough', 'quarter'}
ROAD_KEYS = ('road', 'pedestrian', 'residential', 'footway', 'path')
UNIT_PREFIX_RE = re.compile('^(?:suite|unit|floor|room|apt|apartment|appartment)\\b', re.IGNORECASE)
UNIT_OR_FLOOR_RE = re.compile('^(?:suite|unit|floor|room|apt|apartment|appartment|\\d+(?:st|nd|rd|th)\\s+level)\\b', re.IGNORECASE)
POSTAL_DELIVERY_RE = re.compile('\\b(?:p\\.?\\s*o\\.?\\s*box|post\\s*office\\s*box|postfach|postbus|bo[iî]te\\s+postale)\\b', re.IGNORECASE)
ROAD_SUFFIXES = {'avenue', 'boulevard', 'court', 'drive', 'highway', 'trail', 'terrace', 'lane', 'parkway', 'place', 'road', 'street', 'strasse', 'way', 'weg'}
NAMED_PREMISE_KEYS = ('building', 'office', 'amenity', 'commercial', 'shop', 'tourism')
ROAD_PREFIXES = {'avenue', 'boulevard', 'calle', 'carrer', 'rue', 'via', 'viale'}
ROAD_TOKEN_ALIASES = {'ave': 'avenue', 'av': 'avenue', 'blvd': 'boulevard', 'ct': 'court', 'dr': 'drive', 'hwy': 'highway', 'ln': 'lane', 'pkwy': 'parkway', 'pl': 'place', 'rd': 'road', 'st': 'street', 'str': 'strasse', 'strasse': 'strasse', 'n': 'north', 's': 'south', 'e': 'east', 'w': 'west', 'ne': 'northeast', 'nw': 'northwest', 'se': 'southeast', 'sw': 'southwest'}
US_ADMIN_CODES = ['AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC']
US_ADMIN_NAMES = ['alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 'delaware', 'florida', 'georgia', 'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine', 'maryland', 'massachusetts', 'michigan', 'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada', 'new_hampshire', 'new_jersey', 'new_mexico', 'new_york', 'north_carolina', 'north_dakota', 'ohio', 'oklahoma', 'oregon', 'pennsylvania', 'rhode_island', 'south_carolina', 'south_dakota', 'tennessee', 'texas', 'utah', 'vermont', 'virginia', 'washington', 'west_virginia', 'wisconsin', 'wyoming', 'district_of_columbia']
CA_ADMIN_CODES = 'AB BC MB NB NL NS NT NU ON PE QC SK YT'.split()
CA_ADMIN_NAMES = ['alberta', 'british_columbia', 'manitoba', 'new_brunswick', 'newfoundland_and_labrador', 'nova_scotia', 'northwest_territories', 'nunavut', 'ontario', 'prince_edward_island', 'quebec', 'saskatchewan', 'yukon']
ADMIN_AREA_ALIASES = dict(zip(US_ADMIN_CODES + CA_ADMIN_CODES, US_ADMIN_NAMES + CA_ADMIN_NAMES, strict=True))
ADMIN_AREA_ALIASES.update({value.replace('_', ' '): value for value in ADMIN_AREA_ALIASES.values()})

def normalize(value: Any) -> str:
    """Normalize address text for exact comparison"""
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join((char for char in text if not unicodedata.combining(char)))
    return ' '.join(TOKEN_RE.findall(text))

def compact(value: Any) -> str:
    return ''.join(TOKEN_RE.findall(normalize(value)))

def exact_equal(left: Any, right: Any) -> bool:
    return bool(normalize(left) and normalize(left) == normalize(right))

def exact_component_in_raw(component: Any, raw_address: str) -> bool:
    needle = normalize(component)
    haystack = normalize(raw_address)
    return bool(needle and f' {needle} ' in f' {haystack} ')

def canonical_admin_area(value: Any) -> str:
    normalized = normalize(value)
    return ADMIN_AREA_ALIASES.get(normalized.upper(), ADMIN_AREA_ALIASES.get(normalized, ''))

def source_admin_area(row: dict[str, str]) -> str:
    """Extract an explicitly recorded US state or Canadian province"""
    if row.get('ctry_code', '').upper() not in {'US', 'CA'}:
        return ''
    raw = f"{row.get('locality', '')} {row.get('address', '')}"
    text = normalize(raw)
    matches: set[str] = set()
    for alias, canonical in ADMIN_AREA_ALIASES.items():
        if len(alias) == 2:
            if re.search(f'(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])', raw):
                matches.add(canonical)
        elif f' {normalize(alias)} ' in f' {text} ':
            matches.add(canonical)
    return next(iter(matches)) if len(matches) == 1 else ''

def canonical_road_tokens(value: Any) -> list[str]:
    text = re.sub('^c\\(', 'calle ', str(value or ''), flags=re.IGNORECASE)
    text = re.sub("(?i)(?<=\\w)['’]s\\b", 's', text)
    text = re.sub('(?i)(?<=\\w)str\\.(?=\\s|$)', 'strasse', text)
    raw = normalize(text).split()
    collapsed: list[str] = []
    index = 0
    while index < len(raw):
        pair = ''.join(raw[index:index + 2])
        if pair in {'ne', 'nw', 'se', 'sw', 'northeast', 'northwest', 'southeast', 'southwest'}:
            collapsed.append(pair)
            index += 2
        else:
            collapsed.append(raw[index])
            index += 1
    aliased = [('doctor' if token == 'dr' else 'saint') if index == 0 and token in {'dr', 'st'} and (len(collapsed) > 1) and collapsed[1].isalpha() else ROAD_TOKEN_ALIASES.get(token, token) for index, token in enumerate(collapsed)]
    expanded: list[str] = []
    for token in aliased:
        suffix = next((marker for marker in ('strasse', 'weg') if token.endswith(marker) and len(token) > len(marker)), '')
        if suffix:
            expanded.extend((token[:-len(suffix)], suffix))
        else:
            expanded.append(token)
    return expanded

def dutch_designator_alternatives(road: str, country: str) -> list[tuple[str, str]]:
    """Check Belgian and Dutch road-name variants"""
    if country.upper() not in {'BE', 'NL'}:
        return []
    match = re.fullmatch("(?i)([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ .'-]{2,})strat", road)
    if not match:
        return []
    stem = match.group(1).strip()
    return [('straat_corrected', stem + 'straat'), ('straat_omitted', stem)]

def source_named_premise(row: dict[str, str]) -> str:
    """Identify a leading named premise followed by unit detail"""
    parts = [part.strip() for part in str(row.get('address') or '').split(',')]
    if len(parts) < 2 or not parts[0] or (not UNIT_OR_FLOOR_RE.match(parts[1])):
        return ''
    premise = parts[0]
    if POSTAL_DELIVERY_RE.search(premise) or exact_equal(premise, row.get('locality')) or (expected_house(premise) and expected_road(premise)) or (not re.search('[A-Za-zÀ-ÖØ-öø-ÿ]', premise)):
        return ''
    return premise

def road_relation(expected: Any, candidate: Any) -> str:
    """Compare road names using explicit matching rules"""
    left = canonical_road_tokens(expected)
    right = canonical_road_tokens(candidate)
    if not left or not right:
        return ''
    if left == right:
        return 'canonical_exact'
    directions = {'north', 'south', 'east', 'west', 'northeast', 'northwest', 'southeast', 'southwest'}
    left_directions = [(index, token) for index, token in enumerate(left) if token in directions]
    right_directions = [(index, token) for index, token in enumerate(right) if token in directions]
    if len(left_directions) == len(right_directions) == 1 and left_directions[0][1] == right_directions[0][1] and ([token for token in left if token not in directions] == [token for token in right if token not in directions]):
        return 'canonical_direction_order'
    if left[0] == right[0] and left[-1] == right[-1]:
        iterator = iter(right)
        if all((any((token == item for item in iterator)) for token in left)):
            return 'official_name_extension'
    return ''

def street_segment(address: str) -> str:
    """Return the street-like part before REGPAT's appended postcode/locality"""
    parts = [part.strip() for part in (address or '').split(',')]
    if len(parts) > 1:
        leading_unit = LEADING_HOUSE_UNIT_RE.fullmatch(parts[0])
        following = parts[1]
        if leading_unit and re.search('[A-Za-zÀ-ÖØ-öø-ÿ]', following) and (not re.search('\\b\\d{4,}\\b', following)):
            return f'{leading_unit.group(1)} {following}'.strip()
    for index, part in enumerate(parts):
        plain_part = part.strip('()[]{} ')
        is_house = bool(re.fullmatch('\\d+[a-zA-Z]?', plain_part))
        is_range = bool(HOUSE_RANGE_RE.fullmatch(plain_part))
        if not (is_house or is_range):
            continue
        next_tokens = normalize(parts[index + 1]).split() if index + 1 < len(parts) else []
        if next_tokens and next_tokens[0] in ROAD_PREFIXES:
            return f'{part} {parts[index + 1]}'.strip()
        if index > 0:
            return f'{parts[index - 1]} {part}'.strip()
        if index + 1 < len(parts) and re.search('[A-Za-zÀ-ÖØ-öø-ÿ]', parts[index + 1]):
            return f'{part} {parts[index + 1]}'.strip()
    for part in parts:
        if not part or UNIT_OR_FLOOR_RE.match(part):
            continue
        range_match = HOUSE_RANGE_RE.search(part)
        house_match = HOUSE_RE.search(part)
        if not (range_match or house_match):
            continue
        if re.search('[A-Za-zÀ-ÖØ-öø-ÿ]', part):
            return part
    if len(parts) > 1 and UNIT_PREFIX_RE.match(parts[0]):
        return parts[1]
    if len(parts) > 1 and re.fullmatch('\\d+[a-zA-Z]?', parts[0]):
        return f'{parts[0]} {parts[1]}'.strip()
    return parts[0] if parts else ''

def expected_house(address: str) -> str:
    match = HOUSE_RE.search(street_segment(address))
    return compact(match.group(1)) if match else ''

def expected_house_members(address: str) -> list[str]:
    """Parse a house number or bounded numeric range"""
    segment = street_segment(address)
    match = HOUSE_RANGE_RE.search(segment)
    if match:
        start_raw, end_raw = match.groups()
        start, end = (int(start_raw), int(end_raw))
        if not (len(end_raw) > 1 and end_raw.startswith('0')) and start < end <= start + 20:
            return [str(value) for value in range(start, end + 1)]
    house = expected_house(address)
    return [house] if house else []

def house_range_size(address: str) -> int:
    """Return the inclusive size of a valid numeric range, otherwise zero"""
    match = HOUSE_RANGE_RE.search(street_segment(address))
    if not match:
        return 0
    start_raw, end_raw = match.groups()
    if len(end_raw) > 1 and end_raw.startswith('0'):
        return 0
    start, end = (int(start_raw), int(end_raw))
    return end - start + 1 if start < end else 0

def replace_house_range_member(address: str, member: str) -> str:
    """Replace the first house-number range matched by the separator grammar"""
    return HOUSE_RANGE_RE.sub(member, address, count=1)

def expected_road(address: str, locality: str='') -> str:
    segment = street_segment(address)
    match = HOUSE_RANGE_RE.search(segment) or HOUSE_RE.search(segment)
    if not match:
        road = segment
    elif match.start() == 0:
        road = segment[match.end():].strip()
    else:
        before = segment[:match.start()].strip()
        after = segment[match.end():].strip()
        road = after if ROAD.search(after) and (not ROAD.search(before)) and (not re.search('(?i)\\b(?:unit|suite|floor|apartment|apt|block|building|tower)\\b', before)) else before
    locality_canonical = canonical_road_tokens(locality)
    directional = {'north', 'south', 'east', 'west', 'northeast', 'northwest', 'southeast', 'southwest'}
    weak_locality = bool(locality_canonical) and all((token in ROAD_SUFFIXES | ROAD_PREFIXES | directional for token in locality_canonical))
    if locality and (not weak_locality):
        locality_tokens = normalize(locality).split()
        road_tokens = normalize(road).split()
        if locality_tokens and road_tokens[-len(locality_tokens):] == locality_tokens:
            for token in re.finditer('\\S+', road):
                if normalize(road[token.start():]) == normalize(locality):
                    road = road[:token.start()].rstrip(' ,')
                    break
    tokens = road.split()
    canonical = [canonical_road_tokens(token) for token in tokens]
    for index, token_parts in enumerate(canonical):
        if index > 0 and len(token_parts) == 1 and (token_parts[0] in ROAD_SUFFIXES):
            end = index + 1
            while end < len(canonical) and len(canonical[end]) == 1 and (canonical[end][0] in ROAD_SUFFIXES | directional):
                end += 1
            return ' '.join(tokens[:end]).strip(' ,')
    return road.strip(' ,')

def source_street_components(row: dict[str, str]) -> tuple[list[str], str]:
    prepared = prepare_address(row)
    if prepared.kilometre_road is not None:
        return ([], prepared.kilometre_road)
    country = row.get('ctry_code', '').upper()
    physical = prepared.street_text
    if country == 'IE' and 'ie_floor_recipient_before_road' in prepared.rules:
        return ([], physical.split(',', 1)[0].strip())
    if country == 'CA':
        civic = re.search('(?i)\\b(\\d+)-(\\d+(?:st|nd|rd|th)\\s+[^,]*?)(?:\\s+Suite\\s+\\d+)(?=,|$)', physical)
        if civic:
            return ([civic[1]], civic[2].strip())
    if country == 'MX':
        civic = re.search('(?i)\\b((?:Poniente|Oriente|Calle|Avenida)\\s+\\d+)\\s+no\\.?\\s*(\\d+)', row.get('address', ''))
        if civic:
            return ([civic[2]], civic[1])
    if country == 'GB' and 'gb_compound_physical_identifier' in prepared.rules:
        clause = prepared.street_text.split(',', 1)[0]
        compound = re.fullmatch('(\\d+\\s*/\\s*\\d+)\\s+(.+)', clause)
        if compound:
            return ([re.sub('\\s', '', compound[1])], compound[2])
    if country == 'IE' and (not re.search('\\d', physical.split(',', 1)[0])):
        for part in physical.split(',')[1:]:
            if re.fullmatch("[A-Za-zÀ-ÿ .'-]+\\s+(?:Road|Street|Lane|Quay)", part.strip(), re.I):
                return ([], part.strip())
    if country == 'AU':
        unit = re.match('(?i)^(?:unit\\s+)?(\\d+)/(\\d+)\\s+([^,]+)', row.get('address', ''))
        if unit:
            return ([unit.group(2)], unit.group(3).strip())
        if re.match('(?i)^lot\\s+\\d+\\b', row.get('address', '')):
            return ([], re.sub('(?i)^lot\\s+\\d+\\s*', '', row.get('address', '').split(',', 1)[0]))
    if country == 'CL':
        civic = re.match('(?i)^(\\d+\\s+(?:norte|sur|oriente|poniente))\\s+(\\d+[A-Za-z]?)(?=\\s|,|$)', physical)
        if civic:
            return ([civic[2]], civic[1])
    if country == 'CO':
        original = row.get('address', '')
        designator = '(?:calle|carrera|cra\\s*\\.?|cr\\s*\\.?|diagonal|transversal|trans\\s*\\.?|circular|avenida|autopista|carretera)'
        identifier = '\\d+(?:(?:ra|ro|da|do|ta|to)\\.?|[A-Za-z])?(?:\\s+[A-Za-z](?=\\s|,|$))?(?:\\s*(?:este|oeste|norte|sur))?'
        numbered = re.search('(?i)\\b(' + designator + '\\s+' + identifier + ')(?=\\s|,|$)', original)
        if numbered:
            road = re.sub('\\s+', ' ', numbered[1]).strip()
            road = re.sub('(?i)^(?:cra|cr)\\s*\\.?\\s+', 'Carrera ', road)
            road = re.sub('(?i)^trans\\s*\\.?\\s+', 'Transversal ', road)
            tail = re.sub('^\\s*,\\s*', ' ', original[numbered.end():])
            label = '(?:no\\s*\\.?|nro\\s*\\.?|nº|n°|n\\[deg\\]|número|numero)\\s*[, :]?\\s*'
            compound = re.match('(?i)\\s*(?:' + label + ')?(\\d+(?:\\s*[A-Za-z])?(?:\\s*(?:sur|norte|este|oeste))?\\s*-\\s*\\d+[A-Za-z]?)(?=\\s|,|$)', tail)
            if compound:
                return ([re.sub('\\s+', '', compound[1])], road)
            civic = re.match('(?i)\\s*' + label + '(\\d+[A-Za-z]?)(?=\\s|,|$)', tail)
            if civic:
                if re.match('(?i)(?:\\s+|\\s*,\\s*)(?:sur|norte|este|oeste)\\b', tail[civic.end():]):
                    return ([], road)
                return ([civic[1]], road)
            return ([], road)
        named = re.search('(?i)\\b((?:autopista|avenida|carretera)\\s+[^,]+?)\\s+(?:no\\s*\\.?|nro\\s*\\.?|nº|n°|número|numero)\\s*[, :]?\\s*(\\d+(?:\\s*[A-Za-z])?(?:\\s*(?:sur|norte|este|oeste))?\\s*-\\s*\\d+[A-Za-z]?)(?=\\s|,|$)', original)
        if named:
            return ([re.sub('\\s+', '', named[2])], named[1].strip())
    if country == 'TR':
        civic = re.search('(?i)([^,]*?(?:\\d+(?:/\\d+)?\\s+sokak|\\d+\\.\\s*(?:sok\\.?|cad\\.?)))\\s+no\\s*:\\s*(\\d+)', row.get('address', ''))
        if civic:
            road = re.sub('(?i)^.*?\\bmahallesi\\s+', '', civic.group(1)).strip()
            return ([civic.group(2)], road)
    if country == 'CR' and re.search('(?i)\\d+(?:[.,]\\d+)?\\s*(?:m\\b|mts\\b|km\\b)', row.get('address', '')):
        return ([], '')
    if country in {'GR', 'EE', 'IS'} and (not re.search('\\d', physical)):
        return ([], '')
    parts = [part.strip() for part in prepared.street_text.split(',')]
    if row.get('ctry_code', '').upper() == 'PL':
        for index, part in enumerate(parts[1:], 1):
            if re.fullmatch('\\d+\\s*/\\s*\\d+', part):
                previous = parts[index - 1]
                street_labelled = 'pl_street_label' in prepared.rules or bool(ROAD.search(previous))
                if street_labelled and re.search('[A-Za-zÀ-ÿ]', previous) and (not re.search('\\d', previous)):
                    return ([re.sub('\\s', '', part)], previous)
                return ([], '')
    if row.get('ctry_code', '').upper() == 'CA' and parts:
        civic = re.fullmatch('(\\d+-[A-Za-z])\\s+(.+)', parts[0])
        if civic and ROAD.search(civic.group(2)):
            return ([civic.group(1)], civic.group(2))
    prepared_row = {**row, 'address': prepared.street_text, 'locality': prepared.city}
    if not row.get('locality') and any((rule in prepared.rules for rule in ('postal_component_locality', 'separate_postal_locality'))):
        prepared_row['locality'] = ''
    if country == 'GB' and 'gb_weak_locality_query_variant' in prepared.rules:
        prepared_row['locality'] = ''
    if 'explicit_road_locality_suffix_guard' in prepared.rules:
        prepared_row['locality'] = ''
    members, road = _source_street_components(prepared_row)
    if not road and (not POSTAL_DELIVERY_RE.search(prepared.street_text)) and any((rule in prepared.rules for rule in ('postcode_isolated', 'fr_bp_cs_delivery'))):
        for segment in prepared.street_text.split(','):
            segment = segment.strip()
            if (ROAD.search(segment) or (row.get('ctry_code', '').upper() == 'SE' and 'postal_component_locality' in prepared.rules and (len(prepared.street_text.split(',')) == 1))) and (not re.search('\\d', segment)) and (not exact_equal(segment, row.get('locality', ''))):
                members, road = ([], segment)
                break
    if row.get('ctry_code', '').upper() in {'IT', 'CZ', 'DE', 'AT', 'PL', 'NL'} and (not POSTAL_DELIVERY_RE.search(prepared.street_text)):
        segment = street_segment(prepared.street_text)
        compound_pattern = '(?<!\\d)(\\d+\\s*/\\s*(?:\\d+|[A-Za-z]))(?![A-Za-z0-9])' if row.get('ctry_code', '').upper() == 'IT' else '(?<!\\d)(\\d+\\s*/\\s*\\d+)(?!\\d)'
        compound = re.search(compound_pattern, segment)
        if compound:
            prefix = segment[:compound.start()].strip(' ,')
            if prefix:
                road = prefix
            else:
                previous = prepared.street_text.split(',', 1)[0].strip()
                if previous != segment and re.search('[A-Za-zÀ-ÿ]', previous):
                    road = previous
            if road and (not road.startswith('/')):
                members = [re.sub('\\s', '', compound.group(1))]
    if row.get('ctry_code', '').upper() in {'US', 'GB', 'DE', 'FR'} and 'postcode_isolated' in prepared.rules and (not members) and (not ROAD.search(road)):
        return ([], '')
    label = re.match('(?i)^(?:block|floor|unit|suite|room|apt\\.?|apartment)\\b', road)
    literal_named_road = bool(re.fullmatch('(?i)(?:block|floor|unit|suite|room|apt\\.?|apartment)\\s+(?:road|street|avenue|drive|lane|way|court|place|rd\\.?|st\\.?|ave\\.?)', road))
    if members and label and (not literal_named_road):
        return ([], '')
    if not re.search('[A-Za-zÀ-ÖØ-öø-ÿ]', road):
        return ([], '')
    clause = prepared.street_text.split(',', 1)[0].strip()
    suffix = re.search('(?i)\\b(\\d+)\\s+([A-Za-z])$', clause)
    if suffix and 'spaced_civic_letter_identifier' in prepared.rules and (members == [suffix[1]]) and (not re.search('(?i)\\b(?:unit|suite|floor|room|apt|apartment|block)\\b', clause)):
        members = [suffix[1] + suffix[2].lower()]
    return (members, road)

def _source_street_components(row: dict[str, str]) -> tuple[list[str], str]:
    """Extract house-number members and road names from address evidence"""
    address = row.get('address', '')
    if POSTAL_DELIVERY_RE.search(address):
        return ([], '')
    if row.get('ctry_code', '').upper() == 'SE':
        evidence_postcode = compact(row.get('query_postal_code') or row.get('postal_code', ''))
        postcode_match = re.match(f'^\\s*(?:S\\s*-?\\s*)?{evidence_postcode[:3]}\\s+{evidence_postcode[3:]}(?:\\s+|$)', address, re.IGNORECASE) if re.fullmatch('\\d{5}', evidence_postcode) else None
        if postcode_match:
            remainder = address[postcode_match.end():].strip(' ,-()')
            locality = row.get('locality', '')
            locality_form = bool(not remainder or (locality and exact_equal(remainder, locality)) or (not locality and re.fullmatch("[A-Za-zÀ-ÖØ-öø-ÿ .'-]+", remainder)))
            if locality_form:
                return ([], '')
    address_tokens = normalize(address).split()
    locality_tokens = normalize(row.get('locality', '')).split()
    evidence_postcode = row.get('query_postal_code') or row.get('postal_code', '')
    postcode_tokens = normalize(evidence_postcode).split()
    residual = list(address_tokens)
    for token in [*locality_tokens, *postcode_tokens]:
        if token in residual:
            residual.remove(token)
    if locality_tokens and postcode_tokens and all((token.isalpha() and (len(token) <= 3 or token in {'ontario'}) for token in residual)):
        return ([], '')
    members = expected_house_members(address)
    road = expected_road(address, row.get('locality', ''))
    postcode = compact(evidence_postcode)
    road_tokens = canonical_road_tokens(road)
    has_road_marker = bool(set(road_tokens).intersection(ROAD_PREFIXES | ROAD_SUFFIXES))
    if not members and postcode_tokens and locality_tokens and (not has_road_marker):
        return ([], '')
    member_is_postcode = bool(members and postcode and (compact(members[0]) == postcode))
    if not member_is_postcode:
        return (members, road)
    if member_is_postcode and (not has_road_marker):
        return ([], '')
    return (members, road)

def assess_source_granularity(row: dict[str, str]) -> str:
    """Classify the detail recorded in the source address"""
    house_members, road = source_street_components(row)
    if house_members and road or source_named_premise(row):
        return 'address'
    if road:
        return 'street'
    if prepare_address(row).postcode or row.get('postal_code'):
        return 'postcode'
    if prepare_address(row).city:
        return 'locality'
    return 'region'

def first_component(address: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        if address.get(key):
            return (str(address[key]), key)
    return ('', '')

def matching_component(expected: str, address: dict[str, Any], keys: tuple[str, ...], raw_address: str, *, allow_raw_component: bool=True) -> tuple[str, str, bool, str]:
    for key in keys:
        value = str(address.get(key) or '')
        if exact_equal(expected, value):
            return (value, key, True, 'structured_exact')
        if allow_raw_component and exact_component_in_raw(value, raw_address):
            return (value, key, True, 'raw_component_exact')
    value, key = first_component(address, keys)
    return (value, key, False, '')

def house_relation(expected: str, candidate: str) -> str:
    if not expected or not candidate:
        return ''
    if expected == candidate:
        return 'exact'
    if expected.isdigit():
        if re.fullmatch('\\d+(?:\\s*;\\s*\\d+)+', candidate):
            return 'exact_list_member' if expected in [x.strip() for x in candidate.split(';')] else ''
        match = re.fullmatch('(\\d+)(?:(?:\\s+|-(?=0\\d))[a-z0-9]+|[a-z]+)', candidate)
        if match and match.group(1) == expected:
            return 'base'
        if not re.search('[-/]', candidate) and expected in normalize(candidate).split():
            return 'exact_list_member'
    return ''

@dataclass(frozen=True)
class Evidence:
    house_expected: str
    house_candidate: str
    house_match: bool
    house_match_kind: str
    road_expected: str
    road_candidate: str
    road_match: bool
    road_match_method: str
    postcode_expected: str
    postcode_candidate: str
    postcode_match: bool
    postcode_match_method: str
    locality_expected: str
    locality_candidate: str
    locality_field: str
    locality_match: bool
    locality_match_method: str
    country_match: bool
    premise_expected: str
    premise_candidate: str
    premise_match: bool
    admin_area_expected: str
    admin_area_candidate: str
    admin_area_match: bool
    candidate_addresstype: str
    evidence_score: float
    evidence_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result['evidence_reasons'] = list(self.evidence_reasons)
        return result

# Assess individual address components before assigning a location precision
def compare_components(row: dict[str, str], candidate: dict[str, Any]) -> Evidence:
    address = candidate.get('address') or {}
    house_members, road_a = source_street_components(row)
    house_a = '-'.join((house_members[0], house_members[-1])) if len(house_members) > 1 else house_members[0] if house_members else ''
    house_b = normalize(address.get('house_number'))
    raw_address = row.get('address', '')
    road_b, _road_field, road_match, road_match_method = matching_component(road_a, address, ROAD_KEYS, raw_address, allow_raw_component=False)
    if road_b and (not road_match):
        road_match_method = road_relation(road_a, road_b)
        road_match = bool(road_match_method)
    if road_b and (not road_match) and (row.get('ctry_code', '').upper() == 'CO'):

        def numbered_road_identity(value):
            match = re.fullmatch('(calle|carrera|cra|cr|diagonal|transversal|trans|circular)\\s+(\\d+(?:ra|ro|da|do|ta|to)?(?:\\s*[a-z])?(?:\\s*(?:este|oeste|norte|sur))?)', normalize(value))
            if not match:
                return None
            label = {'cra': 'carrera', 'cr': 'carrera', 'trans': 'transversal'}.get(match[1], match[1])
            return (label, re.sub('\\s+', '', match[2]))
        source_identity, returned_identity = (numbered_road_identity(road_a), numbered_road_identity(road_b))
        if source_identity and source_identity == returned_identity:
            road_match, road_match_method = (True, 'co_numbered_road_identity_exact')
    if road_b and (not road_match) and (row.get('ctry_code', '').upper() == 'ES'):

        def avenue_identity(value):
            tokens = canonical_road_tokens(value)
            if not tokens or tokens[0] not in {'avenue', 'avenida', 'avinguda', 'avda'}:
                return None
            rest = tokens[1:]
            if rest and rest[0] == 'del':
                rest = rest[1:]
            return rest or None
        source_identity, returned_identity = (avenue_identity(road_a), avenue_identity(road_b))
        if source_identity and source_identity == returned_identity:
            road_match, road_match_method = (True, 'es_avenue_designator_exact_name')
    if not road_match and candidate.get('addresstype') == 'road':
        aliases = candidate.get('namedetails') or {}
        for key, alias in sorted(aliases.items()):
            if key == 'name' or key.startswith('name:') or key == 'official_name':
                if exact_equal(road_a, alias):
                    road_b, road_match, road_match_method = (alias, True, 'nominatim_road_alias_exact:' + key)
                    break
    if road_b and (not road_match) and (row.get('ctry_code', '').upper() == 'CA'):
        source_tokens, returned_tokens = (canonical_road_tokens(road_a), canonical_road_tokens(road_b))
        if len(returned_tokens) >= 3 and returned_tokens[0] == returned_tokens[-1] and (returned_tokens[0] in ROAD_SUFFIXES) and (source_tokens == returned_tokens[1:]):
            road_match = True
            road_match_method = 'ca_duplicated_designator'
    if road_b and (not road_match):
        for method, alternative in dutch_designator_alternatives(road_a, row.get('ctry_code', '')):
            if exact_equal(alternative, road_b):
                road_match = True
                road_match_method = 'be_nl_' + method
                break
    locality_a = prepare_address(row).city
    if locality_a and any((exact_equal(locality_a, address.get(key)) for key in ('state', 'province', 'region', 'state_district'))):
        locality_a = ''
    locality_b, locality_field, locality_match, locality_match_method = matching_component(locality_a, address, LOCALITY_KEYS, raw_address)
    candidate_house_numbers = re.findall('\\d+', str(address.get('house_number') or ''))
    house_match_kind = 'range_exact' if len(house_members) > 1 and candidate_house_numbers == [house_members[0], house_members[-1]] else 'range_member' if len(house_members) > 1 and house_b in house_members else house_relation(house_a, str(address.get('house_number') or '').casefold().strip()) if ';' in str(address.get('house_number') or '') else house_relation(house_a, house_b)
    if row.get('ctry_code', '').upper() == 'CO' and '-' in house_a or '/' in house_a or re.fullmatch('\\d+-[A-Za-z]', house_a):
        house_match_kind = 'exact' if re.sub('\\s', '', str(address.get('house_number') or '')).casefold() == house_a.casefold() else ''
    house_match = bool(house_match_kind)
    if not house_match and row.get('ctry_code', '').upper() == 'AT' and road_match and re.fullmatch('\\d{1,4}(?:/\\d{1,4}){1,2}', house_a) and (house_b == house_a.split('/', 1)[0]):
        house_match_kind = 'base'
        house_match = True
    raw_postcode = str(row.get('postal_code') or '')
    postcode_a = str(row.get('query_postal_code') or '') if row.get('query_postal_code_method') in {'derived_terminal_address_us_zip', 'source_valid_se_postcode', 'source_valid_pl_postcode', 'derived_joined_address_de_postcode', 'source_valid_nl_postcode', 'derived_address_nl_postcode', 'derived_explicit_luxembourg_postcode'} else raw_postcode
    prepared_postal = prepare_address(row)
    if row.get('ctry_code', '').upper() == 'LT' and any((rule in prepared_postal.rules for rule in ('lt_postal_only_fields', 'lt_explicit_postcode'))):
        postcode_a = prepared_postal.postcode
    postcode_b = str(address.get('postcode') or '')
    postcode_expected_compact = compact(postcode_a)
    postcode_candidate_compact = compact(postcode_b)
    postcode_structured_match = bool(postcode_expected_compact and postcode_expected_compact == postcode_candidate_compact)
    us_base_match = bool(row.get('ctry_code', '').upper() == 'US' and re.fullmatch('\\d{5}(?:\\d{4})?', postcode_expected_compact) and re.fullmatch('\\d{5}(?:\\d{4})?', postcode_candidate_compact) and (postcode_expected_compact[:5] == postcode_candidate_compact[:5]))
    source_postcode_tokens = normalize(postcode_a).split()
    candidate_postcode_tokens = normalize(postcode_b).split()
    postcode_raw_match = bool(exact_component_in_raw(postcode_b, raw_address) and source_postcode_tokens and (len(candidate_postcode_tokens) > len(source_postcode_tokens)) and (candidate_postcode_tokens[:len(source_postcode_tokens)] == source_postcode_tokens))
    postcode_match = postcode_structured_match or us_base_match or postcode_raw_match
    postcode_match_method = 'structured_exact' if postcode_structured_match else 'us_zip5_exact' if us_base_match else 'raw_component_exact' if postcode_raw_match else ''
    country_match = bool(compact(row.get('ctry_code')) and compact(row.get('ctry_code')) == compact(address.get('country_code')))
    premise_a = source_named_premise(row)
    premise_b, _premise_field, premise_match, _premise_method = matching_component(premise_a, address, NAMED_PREMISE_KEYS, '', allow_raw_component=False)
    if not premise_match and premise_a and (str(candidate.get('addresstype') or '') in {'building', 'office', 'amenity', 'commercial', 'shop', 'tourism'}):
        display_component = str(candidate.get('display_name') or '').split(',', 1)[0]
        if exact_equal(premise_a, display_component):
            premise_b, premise_match = (display_component, True)
    admin_a = source_admin_area(row)
    admin_b_raw = next((str(address.get(key) or '') for key in ('state', 'province', 'region') if address.get(key)), '')
    admin_b = canonical_admin_area(admin_b_raw)
    admin_match = bool(admin_a and admin_b and (admin_a == admin_b))
    candidate_addresstype = str(candidate.get('addresstype') or candidate.get('type') or '')
    score = 0.0
    reasons: list[str] = []
    if country_match:
        score += 0.1
        reasons.append('country_exact')
    else:
        reasons.append('country_mismatch')
    if postcode_match:
        score += 0.25
        reasons.append(f'postcode_exact:{postcode_match_method}')
    if locality_match:
        score += 0.25
        reasons.append(f'locality_exact:{locality_field}')
    if road_match:
        score += 0.25
        reasons.append(f'road_exact:{road_match_method}')
    if house_match:
        score += 0.15
        reasons.append(f'house_{house_match_kind}')
    elif house_a:
        reasons.append('house_not_confirmed')
    if premise_match:
        score += 0.25
        reasons.append('named_premise_exact')
    if admin_match:
        reasons.append('admin_area_exact')
    return Evidence(house_a, house_b, house_match, house_match_kind, road_a, road_b, road_match, road_match_method, postcode_a, postcode_b, postcode_match, postcode_match_method, locality_a, locality_b, locality_field, locality_match, locality_match_method, country_match, premise_a, premise_b, premise_match, admin_a, admin_b, admin_match, candidate_addresstype, round(score, 4), tuple(reasons))

def assess_granularity(candidate: dict[str, Any], evidence: Evidence) -> tuple[str, str]:
    if evidence.premise_match:
        return ('address', 'exact named-premise evidence returned by Nominatim')
    if evidence.house_match and evidence.road_match:
        if evidence.house_match_kind == 'exact_list_member':
            return ('address', 'house included in explicit returned number list and confirmed road; shared building identifier')
        if evidence.house_match_kind == 'base':
            return ('address', 'base house number and exact road; subordinate-unit ambiguity')
        return ('address', 'exact house number and exact road evidence')
    if evidence.road_match:
        return ('street', 'exact road evidence; house number not confirmed')
    if evidence.postcode_match:
        return ('postcode', 'exact postcode evidence; street not confirmed')
    if evidence.locality_match and evidence.locality_field in LOCALITY_GRANULARITY_FIELDS and (evidence.candidate_addresstype in LOCALITY_RESULT_TYPES):
        return ('locality', f'exact locality evidence from Nominatim {evidence.locality_field}; postcode/street not confirmed')
    return ('region', 'no finer component evidence is confirmed')

def eligible(evidence: Evidence, minimum_score: float) -> tuple[bool, str]:
    if not evidence.country_match:
        return (False, 'country_mismatch')
    if evidence.evidence_score < minimum_score:
        return (False, 'address_evidence_below_threshold')
    if evidence.admin_area_expected and evidence.admin_area_candidate and (not evidence.admin_area_match):
        return (False, 'admin_area_component_conflict')
    if evidence.house_match and evidence.road_match and evidence.postcode_expected and evidence.postcode_candidate and (not evidence.postcode_match) and evidence.locality_expected and (not evidence.locality_match):
        return (False, 'precise_candidate_location_conflict')
    if evidence.road_match and evidence.postcode_expected and evidence.postcode_candidate and (not evidence.postcode_match) and evidence.locality_expected and evidence.locality_candidate and (not evidence.locality_match):
        return (False, 'street_candidate_location_conflict')
    if evidence.road_expected and evidence.road_candidate and (not evidence.road_match):
        return (False, 'road_component_conflict')
    if evidence.house_expected and evidence.house_candidate and (not evidence.house_match):
        return (False, 'house_component_conflict')
    if evidence.postcode_expected and evidence.postcode_candidate and (not evidence.postcode_match) and (not evidence.road_match):
        return (False, 'postcode_component_conflict')
    if not evidence.postcode_match and evidence.locality_expected and evidence.locality_candidate and (not evidence.locality_match) and (not evidence.road_match):
        return (False, 'locality_component_conflict')
    if not (evidence.house_match or evidence.road_match or evidence.premise_match or evidence.postcode_match or evidence.locality_match):
        return (False, 'no_confirmed_address_component')
    if evidence.locality_match and (not (evidence.house_match or evidence.road_match or evidence.postcode_match)) and (evidence.candidate_addresstype not in LOCALITY_RESULT_TYPES):
        return (False, 'locality_evidence_on_nonlocality_feature')
    return (True, 'eligible')

# Combine component agreement with country and regional evidence
def evaluate_candidate(row: dict[str, str], candidate: dict[str, Any], minimum_score: float) -> dict[str, Any]:
    """Classify precision from the recorded component evidence"""
    evidence = compare_components(row, candidate)
    evidence_ok, evidence_reason = eligible(evidence, minimum_score)
    granularity, granularity_reason = assess_granularity(candidate, evidence)
    if 'cleaned_address' in row and granularity not in {'address', 'street', 'postcode'}:
        evidence_ok = False
        evidence_reason = 'locality_or_region_is_context_only'
    return {**evidence.as_dict(), 'eligible_address_evidence': evidence_ok, 'eligibility_reason': evidence_reason, 'assessed_granularity': granularity, 'granularity_reason': granularity_reason}
