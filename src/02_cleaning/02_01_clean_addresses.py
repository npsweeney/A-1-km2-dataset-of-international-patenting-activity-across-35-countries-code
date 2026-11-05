"""Clean address fields using shared and country-specific rules"""
from __future__ import annotations
import re
import json
import unicodedata
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
RULES_VERSION = 'regpat-address-preparation-v2.7.28'
REGISTRATION = re.compile('\\b(?:HRB\\s*|(?:under\\s+)?company\\s+(?:no\\.?|number)\\s*)\\d+\\b', re.I)
PHONE = re.compile('^(?:tel(?:ephone)?|fax)\\.?\\s*:?\\s*[+\\d][\\d ()-]*$', re.I)
BOX = re.compile('^(?:p\\.?\\s*o\\.?\\s*box|post\\s+office\\s+box|postfach|postbus)\\s+[\\d -]+$', re.I)
KM = re.compile('\\b(?:km\\.?|kilomet(?:er|re)s?)\\s*\\d', re.I)
ROAD = re.compile('\\b(?:road|street|avenue|drive|lane|rd|rue|via|viale|calle|carretera|ctra\\.?|strada|chemin|quai|route|boulevard|square|crescent|mall|court|terrace|place|way|trail|parkway|ave|blvd|close|mews|gardens|grove)\\b|(?:strasse|straße|straat|vägen)\\b', re.I)
UNIT_PREFIX = re.compile('^(?:suite|unit|floor|room|apt\\.?|apartment|appartment)\\s*[#.: -]*[A-Za-z]?\\d+[A-Za-z]?\\b\\s*', re.I)

def unit_range(part: str) -> bool:
    match = re.match('(?i)^(?:unit|suite|floor|room|apt\\.?|apartment)\\s+(\\d+)\\s*[-/]\\s*(\\d+)\\b', part)
    return bool(match and 0 < int(match[2]) - int(match[1]) <= 20)

def strip_unit_prefix(part: str) -> str:
    return part if unit_range(part) else UNIT_PREFIX.sub('', part)

def plain(value: str) -> str:
    return re.sub('[^\\w]', '', value.casefold())

@dataclass(frozen=True)
class PreparedAddress:
    text: str
    street_text: str
    rules: tuple[str, ...]
    kilometre_road: str | None = None
    city: str = ''
    postcode: str = ''
CLEANING_FIELDS = ('cleaned_address', 'cleaning_rules', 'cleaning_rules_version', 'prepared_address_json', 'cleaning_source_record')

def _cleaning_source(row):
    return [str(row.get(key) or (row.get('city', '') if key == 'locality' else '')).strip() for key in ('ctry_code', 'address', 'locality', 'postal_code', 'reg_code')]

def _cleaning_source_record(row, payload):
    content = [_cleaning_source(row), payload]
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def cleaned_stage(row):
    """Read the prepared address fields"""
    if not any((field in row for field in CLEANING_FIELDS)):
        return None
    if any((field not in row for field in CLEANING_FIELDS)):
        raise ValueError('Incomplete stage-02 cleaning fields; rerun cleaning')
    if row['cleaning_rules_version'] != RULES_VERSION:
        raise ValueError('Stage-02 cleaning version differs; rerun cleaning')
    prepared = json.loads(row['prepared_address_json'])
    rules = json.loads(row['cleaning_rules'])
    payload = [row['cleaning_rules_version'], prepared, row['cleaned_address'], rules]
    if row['cleaning_source_record'] != _cleaning_source_record(row, payload):
        raise ValueError('Stage-02 source or prepared fields changed; rerun cleaning')
    if set(prepared) != set(PreparedAddress.__dataclass_fields__) or not isinstance(rules, list):
        raise ValueError('Invalid stage-02 preparation')
    prepared['rules'] = tuple(prepared['rules'])
    return (PreparedAddress(**prepared), row['cleaned_address'], rules)

# Prepare query fields with ordered audit IDs; preserve the original address and source identifiers
def prepare_address(row: dict[str, str]) -> PreparedAddress:
    saved = cleaned_stage(row)
    if saved is not None:
        return saved[0]
    prepared = _prepare(*(row.get(key, '') or '' for key in ('ctry_code', 'address', 'locality', 'postal_code', 'query_postal_code')))
    if row.get('ctry_code', '').upper() == 'GB':
        if prepared.city.strip().casefold() in {'city', 'town', 'village', 'county', 'south', 'north', 'east', 'west', 'greater'}:
            prepared = replace(prepared, rules=(*prepared.rules, 'gb_weak_locality_query_variant'))
        compact_postal = re.sub('\\s', '', prepared.postcode).upper()
        if re.fullmatch('[A-Z]{1,2}\\d[A-Z\\d]?O[A-Z]{2}', compact_postal):
            prepared = replace(prepared, rules=(*prepared.rules, 'gb_postcode_syntax_conflict'))
        compound = re.match('^\\d+\\s*/\\s*\\d+\\s+(.+)', prepared.street_text.split(',', 1)[0])
        if compound and ROAD.search(compound[1]):
            prepared = replace(prepared, rules=(*prepared.rules, 'gb_compound_physical_identifier'))
        parts = [x.strip() for x in prepared.street_text.split(',')]
        if len(parts) > 1 and (not ROAD.search(parts[0])) and re.search('(?i)\\b(?:company|ltd\\.?|office|house|cent(?:re|er)|laboratory)\\b', parts[0]):
            roads = [(i, x) for i, x in enumerate(parts[1:], 1) if re.fullmatch("(?i)[A-Za-zÀ-ÿ .'-]+\\s+(?:road|street|lane|avenue|drive|court|mews|terrace)", x)]
            if len(roads) == 1:
                i, road = roads[0]
                prepared = replace(prepared, street_text=', '.join(parts[i:]), rules=(*prepared.rules, 'gb_independent_road_clause'))
    clause = prepared.street_text.split(',', 1)[0].strip()
    if re.search('(?i)\\bterrace\\b', clause):
        prepared = replace(prepared, rules=(*prepared.rules, 'street_designator_terrace'))
    source_clause = row.get('address', '').split(',', 1)[0]
    postal = re.sub('\\s', '', prepared.postcode or row.get('postal_code', ''))
    explicit_postal_boundary = bool(postal and postal.casefold() in re.sub('\\s', '', source_clause).casefold())

    def suffix_form(value):
        return ' '.join(re.findall('[a-z0-9]+', ''.join((ch for ch in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(ch)))))
    road_clause = re.sub('\\s+\\d+[A-Za-z]?(?:\\s+[A-Za-z])?$', '', clause)
    clause_form, city_form = (suffix_form(road_clause), suffix_form(prepared.city))
    if ROAD.search(clause) and city_form and (clause_form == city_form or clause_form.endswith(' ' + city_form)) and (not explicit_postal_boundary):
        prepared = replace(prepared, rules=(*prepared.rules, 'explicit_road_locality_suffix_guard'))
    if row.get('ctry_code', '').upper() in {'US', 'CA'} and city_form in {'terrace', 'road', 'street', 'avenue', 'lane', 'drive', 'court', 'trail', 'way'} and clause_form.endswith(' ' + city_form) and re.search('\\d', clause):
        from geocoding.validation.address_evidence import canonical_admin_area
        clauses = prepared.text.split(',')
        if len(clauses) >= 3:
            separate = clauses[1].strip()
            if re.fullmatch("[A-Za-zÀ-ÿ .'-]+", separate) and (not ROAD.search(separate)) and (not canonical_admin_area(separate)) and (separate.casefold() not in {'canada', 'united states'}):
                prepared = replace(prepared, city=separate, rules=(*prepared.rules, 'us_ca_independent_locality_after_civic_road'))
    if row.get('ctry_code', '').upper() == 'GB' and city_form in {'terrace', 'road', 'street', 'avenue', 'lane', 'drive', 'court', 'trail', 'way'} and clause_form.endswith(' ' + city_form) and re.search('\\d', clause):
        clauses = prepared.text.split(',')
        if len(clauses) >= 2 and postal:
            separate = clauses[1].strip()
            code = re.search('(?i)\\b[A-Z]{1,2}\\d[A-Z\\d]?\\s*\\d[A-Z]{2}\\s*$', separate)
            if code and re.sub('\\s', '', code[0]).casefold() == postal.casefold():
                locality = separate[:code.start()].strip()
                locality = re.sub('(?i)\\s+[A-Za-z]+shire$', '', locality).strip()
                if re.fullmatch("[A-Za-zÀ-ÿ .'-]+", locality) and (not ROAD.search(locality)):
                    prepared = replace(prepared, city=locality, rules=(*prepared.rules, 'gb_independent_postal_locality_after_civic_road'))
    civic_road = bool(ROAD.search(clause) or (row.get('ctry_code', '').upper() in {'DE', 'AT', 'CH'} and re.search('(?i)(?:allee|gasse|weg|platz|ring|damm|ufer)\\b', clause)) or (row.get('ctry_code', '').upper() == 'DK' and re.search('(?i)(?:gade|vej|boulevarden|alle|allé)\\b', clause)))
    if civic_road and re.search('(?i)(?<![\\w/\\\\‐‑‒–—-])\\d+\\s+[A-Za-z]$', clause) and (not re.search('(?i)\\b(?:unit|suite|floor|room|apt|apartment|block)\\b', clause)):
        prepared = replace(prepared, rules=(*prepared.rules, 'spaced_civic_letter_identifier'))
    return prepared

@lru_cache(maxsize=4096)
# Apply the format and country-specific cleaning rules
def _prepare(country: str, raw: str, city: str, postcode: str, query_postcode: str) -> PreparedAddress:
    from ingest.prepare_observations import derive_query_postcode
    if not query_postcode:
        query_postcode, _ = derive_query_postcode(dict(ctry_code=country, address=raw, postal_code=postcode, locality=city))
    row = dict(locality=city, postal_code=postcode, query_postal_code=query_postcode)
    country = country.upper()
    if re.fullmatch('\\s*deceased\\s*', raw, re.I):
        return PreparedAddress('', '', ('non_address_status',), city=city, postcode=query_postcode)
    text = re.sub('\\s+', ' ', raw)
    text = re.sub('\\s*,\\s*', ', ', text).strip(' ,')
    applied = []
    if text != raw:
        applied.append('spacing')

    def replace(rule: str, pattern: str | re.Pattern, replacement: str='') -> None:
        nonlocal text
        changed = re.sub(pattern, replacement, text)
        if changed != text:
            text = changed
            applied.append(rule)
    if city.strip().casefold() == 'trail' and re.search('(?i)\\b\\d+[A-Za-z]?\\s+[^,]+\\btrail\\b', text):
        applied.append('road_type_locality_guard')
    if country in {'US', 'CA', 'GB', 'IE', 'AU', 'NZ'}:
        replace('attached_civic_road_spacing', re.compile('(?i)^(\\d+)([A-Za-z]{2,}\\s+[^,]*?\\b(?:street|road|avenue|drive|lane|way|trail)\\b)'), '\\1 \\2')
    if country == 'FR':
        replace('fr_road_abbreviations', re.compile('(?i)((?:^|,\\s*)\\d+[A-Za-z]?\\s*,?\\s*)av\\.\\s+'), '\\1Avenue ')
        replace('fr_road_abbreviations', re.compile('(?i)((?:^|,\\s*)\\d+[A-Za-z]?\\s*,?\\s*)(?:bd|boul)\\.\\s+'), '\\1Boulevard ')
        replace('fr_road_title', re.compile('(?i)(\\b(?:Avenue|Boulevard|Rue)\\s+(?:du\\s+)?)Prés\\.\\s+'), '\\1Président ')
    replace('labelled_department_before_civic', re.compile('(?i)^(?:dept\\.?|department|service)\\b[^,]*,\\s*(?=\\d+[A-Za-z]?\\s*,?\\s*(?:Avenue|Boulevard|Rue|Road|Street|Lane)\\b)'))
    if country == 'CO' and re.search('(?i)\\b(?:calle|carrera|cra\\.?)\\s+\\d+[A-Za-z]?\\s+(?:no\\.?|nº|n°)\\s*\\d', text):
        replace('co_labelled_office', re.compile('(?i)(?<=\\d)\\s+of\\.?\\s+\\d+[A-Za-z]?(?=\\s*(?:,|$))'))
        replace('co_phone_fragment', re.compile('(?i)(?:^|,\\s*)(?:pbx|tel\\.?)\\s*:?\\s*[+\\d][\\d+ ()-]*(?=\\s*(?:,|$))'))
    if country == 'AU':
        replace('au_suite_before_civic', re.compile('(?i)^suite\\s+\\d+[A-Za-z]?\\s+(?=\\d+[A-Za-z]?\\s+[^,]+\\b(?:road|street|avenue|drive|lane)\\b)'))
    if country == 'IE':
        replace('ie_floor_recipient_before_road', re.compile('(?i)^[^,]*\\b\\d+(?:st|nd|rd|th)\\s+floor\\s*,\\s*(?=[^,]+\\b(?:street|road|lane|quay)\\b)'))
    if country == 'SE':
        replace('se_postal_box', re.compile('(?i)(?:^|,\\s*)box\\s+\\d+\\s*(?=,\\s*\\S)'))
    if country == 'CL' and re.search('^[^,]+\\d+\\s*,', text):
        replace('cl_floor', re.compile('(?i),\\s*piso\\s+\\d+(?=\\s*(?:,|$))'))
    if country == 'PT':
        replace('pt_labelled_civic', re.compile('(?i)([^,]*\\bRua\\b[^,]*),\\s*nr\\.?\\s*(\\d+)'), '\\1 \\2')
        if 'pt_labelled_civic' in applied:
            replace('pt_floor_side', re.compile('(?i),\\s*\\d+°\\s*(?:dto|esq)(?=\\s*(?:,|$))'))
    if country == 'EE':
        replace('ee_attached_postal_prefix', re.compile('(?i)(?<!\\w)EE(?=\\d{5}(?!\\d))'))
    if country == 'CL':
        if re.search('^[^,]+\\s\\d+[A-Za-z]?\\s*,', text):
            replace('cl_labelled_apartment', re.compile('(?i),\\s*(?:dp\\.?|depto\\.?)\\s*\\d+[A-Za-z]?(?=\\s*(?:,|$))'))
        if re.match('(?i)^Universidad\\b', text):
            replace('cl_recipient_before_avenue', re.compile('(?i)^.*?\\s+(?=Avda\\.\\s+[^,]*\\d)'))
    if country == 'DK' and re.search(',\\s*[^,]+\\s+\\d+\\s*,', text):
        replace('dk_attention_recipient', re.compile('(?i),\\s*Att\\.:\\s*[^,]+(?=,)'))
    if country == 'LT' and (not query_postcode):
        postal_only = re.fullmatch('(?:LT\\s*-\\s*)?(\\d{5})\\s+([^\\d,]+)', text, re.I)
        if postal_only:
            query_postcode = postal_only[1]
            city = city or postal_only[2].strip()
            row.update(query_postal_code=query_postcode, locality=city)
            applied.append('lt_postal_only_fields')
    if country == 'ES':
        replace('es_english_street_number_label', re.compile('(?i)(?<=\\S)\\s+street\\s+no\\.?\\s*(?=\\d)'), ' ')
    replace('explicit_civic_marker', re.compile('(?i)(?<!\\w)n(?:º|°|\\[deg\\])\\s*(?=\\d)'))
    if country == 'CL':
        replace('cl_apartment_before_street', re.compile('(?i)^(\\d+[A-Za-z]?)\\s+(?:depto\\.?|dp\\.?)\\s*\\d+[A-Za-z]?\\s*,\\s*(?=[A-Za-zÀ-ÿ])'), '\\1 ')
    replace('leading_separator', '^[/;]+\\s*(?=[A-Za-zÀ-ÿ\\d])')
    replace('registration_identifier', REGISTRATION)
    if country == 'BE':
        replace('be_house_number_label', re.compile('(?i)(?<=\\s)(?:no\\.?|nº|n°)\\s*(?=\\d)'))
    if country == 'ES':
        replace('es_export_floor_unit', re.compile('(?i)(\\b\\d+)(?:\\s+)\\d+\\[deg\\]\\s*[A-Za-z](?=\\s*(?:,|$))'), '\\1')
    if country == 'ES':
        replace('es_export_number_marker', re.compile('(?i)(?<!\\w)n\\[deg\\]\\s*(?=\\d)'))
        replace('es_leading_slash', re.compile('^/\\s*(?=c\\.?/)', re.I))
        replace('es_staircase_unit', re.compile('(?i)(?<=\\d)\\s+esc\\s*-\\s*[A-Za-z]\\s+\\d+[-/]\\d+(?=\\s*(?:,|$))'))
    if country == 'MX':
        replace('mx_internal_unit', re.compile('(?i)(?<=\\d)\\s+int\\.\\s+[A-Za-z]+-\\d+(?=\\s*(?:,|$))'))
    if country == 'LT' and (ROAD.search(text) or re.search('(?i)\\b(?:str\\.|g\\.)', text)) and re.search('\\d', text):
        replace('lt_servicing_post', re.compile("(?i)(?:^|,)\\s*[A-Za-zÀ-ÿ .'-]+\\s+post(?=\\s*(?:,|$))"))
    if country == 'NL':
        replace('nl_postcode_locality_spacing', re.compile('(^|,\\s*)(NL\\s*-\\s*[1-9]\\d{3}\\s*[A-Z]{2})(?=[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]{2,})'), '\\1\\2 ')
    if country == 'ES':
        replace('es_street_label', re.compile('(^|,\\s*)c\\.?/\\.?\\s*(?=[A-Za-zÀ-ÿ])', re.I), '\\1')
    if country == 'ES':
        attic = re.search('(?i),\\s*(?:àtic|ático|atico)\\s+\\d+[A-Za-z]?(?=\\s*(?:,|$))', text)
        if attic:
            previous = text[:attic.start()].rsplit(',', 1)[-1].strip()
            if re.fullmatch("[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]*\\s+\\d{1,4}[A-Za-z]?", previous) or re.fullmatch("\\d{1,4}[A-Za-z]?\\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]*", previous):
                replace('es_attic_unit', re.compile('(?i),\\s*(?:àtic|ático|atico)\\s+\\d+[A-Za-z]?(?=\\s*(?:,|$))'))
    if country == 'MX':
        replace('mx_postcode_label', re.compile('(?<!\\w)c\\.\\s*p\\.\\s*:?\\s*(?=\\d{5}(?!\\d))', re.I))
    if country in {'MX', 'LT'}:
        replace('civic_number_label', re.compile('(?<!\\w)(?:no\\.|nº)\\s*(?=\\d)', re.I))
    if country == 'FI':
        replace('fi_apartment_suffix', re.compile("([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .\\'-]*\\s+\\d{1,4}[A-Za-z]?)\\s+as\\.?\\s+\\d+[A-Za-z]?(?=\\s*(?:,|$))", re.I), '\\1')
    postal_prefix = {'BE': ('(?:B|BE)', '\\d{4}'), 'DK': ('DK', '\\d{4}'), 'EE': ('EE', '\\d{5}'), 'ES': ('(?:E|ES)', '\\d{5}'), 'FI': ('(?:FI|FIN)', '\\d{5}'), 'HU': ('(?:H|HU)', '\\d{4}'), 'IT': ('(?:I|IT)', '\\d{5}'), 'IS': ('IS', '\\d{3}'), 'LT': ('LT', '\\d{5}'), 'LU': ('(?:L|LU)', '\\d{4}'), 'LV': ('LV', '\\d{4}'), 'NO': ('(?:N|NO)', '\\d{4}'), 'PT': ('(?:P|PT)', '\\d{4}-\\d{3}'), 'SE': ('(?:S|SE)', '\\d{3}\\s*\\d{2}'), 'SI': ('SI', '\\d{4}'), 'SK': ('SK', '\\d{3}\\s*\\d{2}'), 'AT': ('(?:A|AT)', '\\d{4}'), 'CH': ('CH', '\\d{4}'), 'DE': ('(?:D|DE)', '\\d{5}'), 'FR': ('(?:F|FR)', '\\d{5}'), 'PL': ('PL', '\\d{2}-\\d{3}'), 'NL': ('NL', '[1-9]\\d{3}\\s*[A-Z]{2}'), 'BG': ('BG', '\\d{4}')}.get(country)
    if postal_prefix:
        prefix, code = postal_prefix
        separator = '(?:\\s*-\\s*|\\s+)' if country == 'AT' else '\\s*-\\s*'
        replace('postal_country_prefix', re.compile(f'(?<!\\w){prefix}{separator}(?={code}(?!\\w))', re.I))
    if country == 'PL':
        replace('pl_street_label', re.compile('(^|,\\s*)ul\\.\\s*(?=[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż])', re.I), '\\1')
    if country == 'FR':
        replace('fr_cedex_delivery', re.compile('\\bc[eé]dex\\b(?:\\s+\\d{1,2}\\b)?', re.I))
        replace('fr_bp_cs_delivery', re.compile('\\b(?:B\\.?\\s*P\\.?|CS)\\s*\\d+\\b', re.I))
    if country == 'BG':
        physical = bool(re.search('(?i)\\b(?:bl\\.?|block|building)\\s*\\d+[A-Za-z]?\\b', text) or (re.search('(?i)\\b(?:str\\.?|street|road)\\b', text) and re.search('\\d', text)))
        if physical:
            replace('bg_internal_delivery', re.compile('\\b(?:entr\\.?|enr\\.?|entrance|floor|apt\\.?|app\\.?|ap\\.?|apartment)\\s*(?:[A-Za-z]|\\d+[A-Za-z]?)\\b\\.?', re.I))
            if 'bg_internal_delivery' in applied:
                replace('bg_storey_delivery', re.compile('(?<!\\w)st\\.\\s*\\d+\\b\\.?', re.I))
    if country == 'BH' and re.search('(?i)\\b(?:building|villa)\\s+\\d+[A-Za-z]?\\b', text) and re.search('(?i)\\broad\\s+\\d+\\b', text):
        replace('bh_flat_delivery', re.compile('\\b(?:flat|apartment|apt\\.?)\\s+\\d+[A-Za-z]?\\b', re.I))
    if country == 'NL' and (not query_postcode):
        postal_only = re.fullmatch("([1-9]\\d{3})\\s*([A-Z]{2})\\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]*)", text)
        if postal_only:
            query_postcode = postal_only.group(1) + ' ' + postal_only.group(2)
            city = city or postal_only.group(3)
            row.update(query_postal_code=query_postcode, locality=city)
            applied.append('nl_postcode_only_components')
    if country == 'LT' and (not query_postcode):
        explicit_postal = re.search('(?<!\\w)LT\\s*-\\s*(\\d{5})(?!\\d)', raw, re.I)
        if explicit_postal:
            query_postcode = explicit_postal.group(1)
            row.update(query_postal_code=query_postcode)
            applied.append('lt_explicit_postcode')
    parser_text = text
    parts = [re.sub('\\s+', ' ', p).strip() for p in parser_text.split(',') if p.strip()]
    if country in {'US', 'GB', 'IE', 'CA'}:
        revised = []
        independent = any((ROAD.search(p) and re.search('\\d', p) for p in parts))
        for part in parts:
            candidate = re.sub('(?i)^\\d+(?:st|nd|rd|th)\\s+(?:floor|level)\\b[ .:-]*', '', part)
            if candidate != part and (not candidate and independent or (re.match('^\\d+[A-Za-z]?\\s+', candidate) and ROAD.search(candidate))):
                part = candidate
                applied.append('ordinal_floor_preserve_street')
            suffix = re.search('(?i)\\s+(?:suite|unit|room|apt\\.?|apartment)\\s*[#.: -]*[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?\\s*$', part)
            if suffix and re.match('^\\d+[A-Za-z]?\\s+[^\\d]', part[:suffix.start()]):
                part = part[:suffix.start()].strip(' ,-')
                applied.append('numbered_street_unit_suffix')
            box = re.search('(?i)\\s+p\\.?\\s*o\\.?\\s*box\\s+[\\d -]+$', part)
            if box and ROAD.search(part[:box.start()]) and (re.search('\\d', part[:box.start()]) or (revised and re.fullmatch('\\d+[A-Za-z]?', revised[-1]))):
                part = part[:box.start()].strip()
                applied.append('attached_postal_box_after_street')
            if part:
                revised.append(part)
        parts = revised
    preserve_lot_label = country == 'AU'
    selected_parts = []
    for part in parts:
        joined = re.fullmatch('([^\\d]+?)\\s+(\\d+[A-Za-z]?\\s+.+)', part)
        if joined:
            prefix, remainder = joined.groups()
            number = re.match('\\d+', remainder).group()
            if ROAD.search(remainder) and (not ROAD.search(prefix)) and (not (preserve_lot_label and re.match('(?i)^lot\\b', prefix))) and (not re.search('(?i)\\b(?:unit|suite|floor|apartment|apt|block|building|tower|km)\\b', prefix)) and (not re.search('(?i)\\bbusiness\\s+cent(?:re|er)\\b', prefix)) and (number != re.sub('\\D', '', query_postcode)):
                part = remainder
                applied.append('recipient_before_numbered_street')
        selected_parts.append(part)
    parts = selected_parts
    filtered = [p for p in parts if not PHONE.fullmatch(p)]
    if filtered != parts:
        applied.append('telephone_component')
        parts = filtered
    physical = [p for p in parts if not BOX.fullmatch(p) and re.search('\\d', p) and ROAD.search(p)]
    if country == 'NL':
        physical += [p for p in parts if re.fullmatch('[A-Za-zÀ-ÿ -]+\\s+\\d{1,4}[A-Za-z]?', p)]
    if physical:
        filtered = [p for p in parts if not BOX.fullmatch(p)]
        if filtered != parts:
            applied.append('separate_postal_box')
            parts = filtered
    if country in {'GB', 'US', 'IE', 'CA'} and physical:
        filtered = [strip_unit_prefix(p) for p in parts]
        if filtered != parts:
            applied.append('unit_prefix_preserve_remainder')
            parts = filtered
    if country == 'ES':
        filtered = [re.sub('(?i)^n[º°o]\\.?\\s*(?=\\d)', '', p) for p in parts]
        filtered = [re.sub('(?i)\\s+(?:pta\\.?\\s*\\d+|\\d+[ºª]\\s*(?:izda|dcha))\\s*$', '', p) for p in filtered]
        if filtered != parts:
            applied.append('es_house_marker_and_door')
            parts = filtered
    if country == 'CA':
        separate_unit = re.compile('(?i)^(?:apt\\.?|apartment|suite|unit|room)\\s*[#.: -]*[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?$')
        independent = any((re.match('^\\d+[A-Za-z]?\\s+[^\\d]', part) for part in parts if not separate_unit.fullmatch(part)))
        if independent:
            revised = [part for part in parts if not separate_unit.fullmatch(part)]
            if revised != parts:
                applied.append('ca_separate_delivery_unit')
                parts = revised
    box_identifier = re.compile('(?i)(?<!\\w)(?:p\\.?\\s*o\\.?\\s*box|post\\s+office\\s+box|postfach|postbus)\\s*[:#-]?\\s*\\d+[A-Za-z]?(?:\\s*[-/]\\s*\\d+[A-Za-z]?)?(?!\\w)')
    without_boxes = [box_identifier.sub('', part).strip(' ,') for part in parts]
    if without_boxes != parts:
        applied.append('postal_box_identifier')
        parts = without_boxes
    text = ', '.join((p.strip() for p in parts if p.strip()))
    postcode = row.get('query_postal_code') or row.get('postal_code', '')
    digits = re.sub('\\D', '', postcode)
    postal = None
    if country == 'US':
        match = re.search('(?<!\\d)\\d{5}(?:-\\d{4})?(?!\\d)\\s*$', text)
        if match:
            postal = re.escape(match.group().strip())
    elif country == 'SE' and len(digits) == 5:
        postal = f'(?:S(?:E)?\\s*-?\\s*)?{digits[:3]}[ -]*{digits[3:]}'
    elif country == 'NL' and re.fullmatch('[1-9]\\d{3}\\s?[A-Z]{2}', postcode, re.I):
        compact = re.sub('\\s', '', postcode)
        postal = f'(?:NL-)?{compact[:4]}\\s*{compact[4:]}'
    elif country == 'LU' and re.fullmatch('\\d{4}', postcode):
        postal = f'(?:L-)?{postcode}'
    elif country == 'PL' and len(digits) == 5:
        postal = f'(?:PL-)?{digits[:2]}[- ]?{digits[2:]}'
    elif country == 'GB' and re.fullmatch('[A-Z]{1,2}\\d[A-Z\\d]?\\s*\\d[A-Z]{2}', postcode, re.I):
        compact_postcode = re.sub('\\s', '', postcode)
        postal = re.escape(compact_postcode[:-3]) + '\\s*' + re.escape(compact_postcode[-3:])
    elif country in {'DE', 'FR'} and len(digits) == 5:
        prefix = 'D(?:E)?' if country == 'DE' else 'F(?:R)?'
        postal = f'(?:{prefix}\\s*-\\s*)?{digits}'
    elif country not in {'US', 'GB', 'DE', 'FR', 'NL', 'SE', 'PL'} and re.fullmatch('\\d{4,6}', postcode):
        prefix = {'IT': 'I(?:T)?', 'BE': 'B(?:E)?', 'AT': 'A(?:T)?', 'ES': 'E(?:S)?', 'PT': 'P(?:T)?', 'FI': 'FI(?:N)?'}.get(country, country)
        postal = f'(?:{prefix}\\s*-\\s*)?{postcode}'
    if country in {'GR', 'IS'}:
        shape = {'GR': '\\d{3}\\s+\\d{2}', 'IS': '\\d{3}'}[country]
        found = re.search(f'(?<![\\w-])({shape})(?![\\w-])', text)
        if found:
            postal = re.escape(found.group(1)).replace('\\ ', '\\s+')
            if not query_postcode:
                query_postcode = found.group(1)
            applied.append('historical_postal_shape')
    locality = plain(row.get('locality', ''))
    prepared_city = city
    parser_parts = []
    postal_seen = False
    for part_index, part in enumerate(parts):
        cleaned = part
        if postcode and plain(part) == plain(postcode):
            cleaned = ''
        elif postal:
            boundary = '(?<!\\w)' if country in {'SE', 'PL'} else '(?<![\\w-])'
            cleaned = re.sub(f'{boundary}{postal}(?!\\d)', '', cleaned, flags=re.I).strip(' ,-()')
        if country != 'US' and postal and re.match(f'{postal}(?!\\d)', part, re.I) and (re.search('\\d', cleaned) or ROAD.search(cleaned)):
            cleaned = part
            applied.append('ambiguous_postal_component_retained')
        if cleaned != part:
            applied.append('postcode_isolated')
            postal_seen = True
            if country != 'US' and postal and re.match(f'{postal}(?!\\d)', part, re.I) and cleaned and (not re.search('\\d', cleaned)) and (not ROAD.search(cleaned)):
                if not prepared_city:
                    prepared_city = re.sub('\\([A-Z]{2}\\)', '', cleaned).strip()
                    applied.append('postal_component_locality')
                cleaned = ''
            residue = re.sub('\\([A-Z]{2}\\)', '', cleaned).strip()
            begins_postcode = bool(postal and re.match(f'{postal}(?!\\d)', part, re.I))
            if not residue or plain(residue) == locality or (country in {'SE', 'PL'} and begins_postcode and (not re.search('\\d', residue)) and (not ROAD.search(residue))) or (country == 'US' and re.fullmatch('[A-Z]{2}', residue)):
                cleaned = ''
        elif postal_seen and country in {'SE', 'PL', 'DE', 'FR'} and (part == parts[-1]) and (not re.search('\\d', part)) and (not ROAD.search(part)):
            if not prepared_city:
                prepared_city = part
                applied.append('separate_postal_locality')
            cleaned = ''
            applied.append('post_postal_admin_component')
        if country in {'GB', 'DE', 'FR'} and cleaned != part and (not ROAD.search(cleaned)) and (not re.search('\\d', cleaned)) and (not (part_index and re.fullmatch('\\d+[A-Za-z]?', parts[part_index - 1]))):
            cleaned = ''
            applied.append('postal_admin_not_street')
        if cleaned:
            parser_parts.append(cleaned)
    if country == 'GB' and city and postcode and parts:
        tail = re.fullmatch("([A-Za-zÀ-ÿ .'-]+?)\\s+" + re.escape(city) + '\\s+' + re.escape(postcode), parts[-1], re.I)
        if tail and (not ROAD.search(tail[1])) and (not re.search('(?i)business|enterprise|centre|floor|suite|unit', tail[1])):
            prepared_city = tail[1].strip()
            applied.append('gb_explicit_smaller_locality')
    if country == 'GB':
        detached = [re.sub('(?i)(\\bsquare)\\s+business\\s+cent(?:re|er)\\b.*$', '\\1', p) if re.match('^\\d+\\s+', p) else p for p in parser_parts]
        if detached != parser_parts:
            applied.append('gb_labelled_business_premise')
            parser_parts = detached
        if len(parts) >= 3 and city and postcode:
            candidate = parts[-2]
            tail = plain(parts[-1])
            if tail.endswith(plain(postcode)) and tail != plain(postcode) and (city.casefold() in {'west', 'east', 'north', 'south'}) and (not re.search('(?i)shire|midlands|sussex|county', candidate)) and re.fullmatch('[A-Z]{1,2}\\d[A-Z\\d]?\\s*\\d[A-Z]{2}', postcode, re.I) and candidate.casefold().startswith(city.casefold() + ' ') and (not re.search('\\d', candidate)) and (not ROAD.search(candidate)) and any((ROAD.search(p) and re.search('\\d', p) for p in parts[:-2])):
                prepared_city = candidate
                applied.append('gb_explicit_locality_completion')
    if country in {'GB', 'IE', 'US', 'CA'}:
        premise_label = re.compile('^(?:block|floor|unit|suite|room|apt\\.?|apartment)\\s*[#.: -]*\\d+\\b', re.I)
        independent_roads = [p for p in parser_parts if ROAD.search(p) and (not premise_label.match(p))]
        if independent_roads:
            retained = [p for p in parser_parts if not premise_label.match(p)]
            if retained != parser_parts:
                applied.append('labelled_premise_separate_street')
                parser_parts = retained
        if country == 'IE' and len(parser_parts) >= 2 and re.fullmatch('(?:\\d{1,2}|6W)', parser_parts[-1], re.I) and (plain(parser_parts[-2]) == 'dublin'):
            parser_parts = parser_parts[:-1]
            applied.append('dublin_postal_district_not_house')
    if country == 'FR':
        isolated = [re.sub('(?i)^(?:Z\\.\\s*I\\.|zone industrielle)\\s+[^\\d]*?(?=\\b(?:\\d+\\s+)?(?:rue|route|avenue|boulevard|chemin)\\s)', '', p) for p in parser_parts]
        if isolated != parser_parts:
            applied.append('fr_industrial_zone_separate_street')
            parser_parts = isolated
    explicit = [p for p in parser_parts if ROAD.search(p) and (not re.match('(?i)^c/o\\b', p))]
    if explicit and any((re.match('(?i)^c/o\\b', p) for p in parser_parts)) and (not any((re.search('\\d', p) for p in parser_parts if p not in explicit))):
        parser_parts = explicit
        applied.append('care_of_separate_street')
    if country == 'US' and parts:
        repeated = re.fullmatch('(.+?)\\s+\\1', parts[-1], re.I)
        if repeated and plain(repeated.group(1)) == 'newyork':
            prepared_city = repeated.group(1)
            applied.append('explicit_repeated_city_state')
    business = re.compile('\\bbusiness\\s+cent(?:re|er)\\b(?!\\s+(?:road|drive|way|avenue|court|street|lane|crescent|place|terrace|parkway|boulevard|dr|rd|ct|st|ave|blvd)\\b)', re.I)
    business_parts = []
    saw_business_premise = False
    for part in parser_parts:
        label = business.search(part)
        if not label:
            business_parts.append(part)
            continue
        saw_business_premise = True
        tail = part[label.end():].strip()
        numbered = re.search('\\b\\d+[A-Za-z]?(?:-\\d+)?\\s+(?=[A-Za-z])', tail)
        recovered = tail[numbered.start():] if numbered else tail
        if ROAD.search(recovered) and (not re.search('(?i)\\b(?:suite|unit|floor|house|estate)\\b', recovered)):
            recovered = re.sub('(?i)(\\bcrescent)\\s+(?!drive\\b|street\\b|road\\b|west\\b|east\\b|north\\b|south\\b).*$', '\\1', recovered)
            business_parts.append(recovered)
            applied.append('business_premise_street_separation')
        else:
            applied.append('business_premise_not_civic_street')
    if saw_business_premise:
        business_parts = [p for p in business_parts if ROAD.search(p) and len(re.findall('[^\\W\\d_]+', p)) >= 2 and (not re.match('(?i)^(?:suite|ste|unit|extension)\\b', p))]
    parser_parts = business_parts
    if re.fullmatch('(?i)\\d{5}(?:\\d{2})?\\s+Tel[ -]+(?:Aviv|Mond)', text):
        parser_parts = []
        applied.append('conflicting_place_only_not_street')
    if country in {'GB', 'IE', 'US', 'CA'}:
        parser_parts = [p for p in parser_parts if not (re.match('(?i)^(?:block|floor|unit|suite|room|apt\\.?|apartment)\\s*[#.: -]*(?=[A-Za-z0-9]*\\d)[A-Za-z0-9]+\\b', p) and (not ROAD.search(p) or unit_range(p)))]
    street_text = ', '.join(parser_parts)
    km_road = None
    km_text = re.sub('\\bKM\\d[A-Z\\d]?\\s*\\d[A-Z]{2}\\b', '', text, flags=re.I) if country == 'GB' else text
    km = KM.search(km_text)
    if km:
        km_parts = [p.strip() for p in km_text.split(',')]
        independent = [p for index, p in enumerate(parser_parts) if not (index and KM.search(parser_parts[index - 1])) and (not KM.search(p)) and (re.search('^\\d+[A-Za-z]?\\s+', p) and ROAD.search(p) or (country == 'RO' and re.search('(?i)^intrarea\\s+.+\\bnr\\.?\\s*\\d+', p)))]
        if independent:
            street_text = ', '.join(independent)
            if country == 'RO':
                street_text = re.sub('(?i)\\bnr\\.?(?=\\s*\\d)', '', street_text)
            applied.append('kilometre_preserve_independent_civic')
        else:
            prefix_pattern = {'ES': '^(?:crtra|crtar|ctra|autov[ií]a|autopista)\\.?\\s+\\S', 'IT': '^s\\.?\\s*[sp]\\.?(?:\\s|n\\.|\\d)', 'FR': '^(?:RN|RD|D|N)\\s*\\d+\\b', 'PT': '^(?:estrada\\s+nacional|EN)\\s*\\d+\\b', 'RO': '^DN\\s*\\d+\\b'}.get(country)
            options = []
            for part in km_parts:
                marker = KM.search(part)
                candidate = part[:marker.start()].strip(' ,-') if marker else part
                if candidate and (ROAD.search(candidate) or (prefix_pattern and re.search(prefix_pattern, candidate, re.I))):
                    options.append(candidate)
            unique = list(dict.fromkeys(options))
            km_road = unique[0] if len(unique) == 1 else ''
            applied.append('kilometre_locator_not_house')
    elif km_text != text:
        applied.append('postcode_like_km_not_distance')
        street_text = re.sub('(?i)\\bKM\\d[A-Z\\d]?\\s*\\d[A-Z]{2}\\b', '', street_text).strip(' ,')
        if not ROAD.search(street_text):
            street_text = ''
    care_of = re.compile('(?<!\\w)c\\s*/\\s*o(?!\\w)\\s*', re.I)
    cleaned_text = care_of.sub('', text).strip(' ,')
    cleaned_street = care_of.sub('', street_text).strip(' ,')
    if (cleaned_text, cleaned_street) != (text, street_text):
        applied.append('care_of_marker')
    return PreparedAddress(cleaned_text, cleaned_street, tuple(dict.fromkeys(applied)), km_road, prepared_city, query_postcode)

# Stream prepared records to the cleaning output and its source-preserving audit
def clean_addresses(source, output):
    """Clean each imported CSV row, preserving source columns and row order"""
    import csv
    from pathlib import Path
    from ingest.prepare_observations import clean_row, derive_query_postcode
    from geocoding.geocoder.queries import cleaned_query_address
    source, output = (Path(source), Path(output))
    if source.resolve() == output.resolve():
        raise ValueError('Input and output must differ')
    count, created = (0, False)
    try:
        with source.open(newline='', encoding='utf-8-sig') as incoming:
            reader = csv.DictReader(incoming)
            fields = reader.fieldnames or []
            required = {'appln_id', 'person_id', 'reg_code', 'address', 'postal_code', 'ctry_code'}
            if not required.issubset(fields) or not {'city', 'locality'} & set(fields):
                raise ValueError('Input must contain imported REGPAT location and identifier fields')
            if len(set(fields)) != len(fields) or any((not field for field in fields)):
                raise ValueError('Empty or duplicate column names')
            if set(fields) & set(CLEANING_FIELDS):
                raise ValueError('Input already has stage-02 fields; supply the stage-01 CSV')
            with output.open('x', newline='', encoding='utf-8') as outgoing:
                created = True
                writer = csv.DictWriter(outgoing, fieldnames=fields + list(CLEANING_FIELDS))
                writer.writeheader()
                for raw in reader:
                    if None in raw or any((value is None for value in raw.values())):
                        raise ValueError(f'Malformed CSV row at line {reader.line_num}')
                    row = clean_row(raw)
                    row['query_postal_code'], _ = derive_query_postcode(row)
                    prepared = asdict(prepare_address(row))
                    text, rules = cleaned_query_address(row)
                    payload = [RULES_VERSION, prepared, text, rules]
                    raw.update(cleaned_address=text, cleaning_rules=json.dumps(rules, ensure_ascii=False), cleaning_rules_version=RULES_VERSION, prepared_address_json=json.dumps(prepared, ensure_ascii=False), cleaning_source_record=_cleaning_source_record(row, payload))
                    writer.writerow(raw)
                    count += 1
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return count

def main(argv=None):
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Clean imported REGPAT addresses using general and country rules')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(f'Cleaned {clean_addresses(args.input, args.output)} rows')
if __name__ == '__main__':
    main()
