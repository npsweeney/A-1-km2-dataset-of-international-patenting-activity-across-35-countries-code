"""Compare source address fields with geocoder candidates"""
import re
from geocoding.validation.address_evidence import evaluate_candidate, canonical_road_tokens, normalize, compact, canonical_admin_area
from cleaning.clean_addresses import prepare_address
AGREEMENT_VERSION = 'regpat-field-agreement-v3'

def normalise(value, field, country=''):
    value = str(value or '')
    if field == 'house':
        return re.sub('\\s+', '', value.casefold())
    if field == 'road':
        if country in {'US', 'GB', 'IE', 'CA', 'AU', 'NZ'}:
            value = re.sub('(?<=[\\w])\\s*&\\s*(?=[\\w])', ' and ', value)
        tokens = canonical_road_tokens(value)
        if country == 'FR' and tokens and (tokens[0] in {'rue', 'route', 'avenue', 'boulevard', 'chemin', 'quai', 'impasse', 'place', 'square', 'allee', 'passage', 'cours'}):
            index = 1
            while index < len(tokens) and tokens[index] in {'de', 'du', 'des', 'd', 'la', 'le', 'les'}:
                index += 1
            if index < len(tokens):
                tokens = tokens[:1] + tokens[index:]
        if country == 'ES' and tokens and (tokens[0] in {'calle', 'carrer', 'avenue', 'avenida', 'paseo', 'carretera', 'camino', 'placa', 'plaza'}):
            index = 1
            while index < len(tokens) and tokens[index] in {'de', 'del', 'dels', 'd', 'la', 'las', 'el', 'los', 'les', 'l'}:
                index += 1
            if index < len(tokens):
                tokens = tokens[:1] + tokens[index:]
        return ' '.join(tokens)
    if field == 'postcode':
        return compact(value)
    if field == 'locality' and country == 'GB':
        value = re.sub('(?i)^city\\s+of\\s+(?=\\S)', '', value.strip())
    tokens = normalize(value).split()
    if field == 'locality' and country in {'US', 'CA'}:
        for index in range(1, len(tokens)):
            if canonical_admin_area(' '.join(tokens[index:])):
                tokens = tokens[:index]
                break
    return ' '.join(tokens)

def compare(source, returned, field, country=''):
    a, b = (normalise(source, field, country), normalise(returned, field, country))
    state = 'missing' if not a or not b else 'exact' if source == returned else 'normalised_exact' if a == b else 'conflicting'
    sa, sb = (a.split(), b.split())
    if state == 'conflicting' and field in {'road', 'locality'} and (set(sa) <= set(sb) or set(sb) <= set(sa)):
        state = 'partial_tokens'
    return dict(source=source, returned=returned, normalised_source=a, normalised_return=b, state=state, normalisation_rule={'house': 'preserve_compound_join_whitespace_casefold', 'road': 'shared_road_tokens_fr_linking_particles' if country == 'FR' else 'shared_road_tokens_es_linking_particles' if country == 'ES' else 'shared_road_tokens_english_ampersand' if country in {'US', 'GB', 'IE', 'CA', 'AU', 'NZ'} else 'shared_existing_canonical_road_tokens', 'postcode': 'shared_compact_postcode', 'locality': 'shared_locality_us_ca_admin_separation' if country in {'US', 'CA'} else 'shared_unicode_case_punctuation'}[field], source_only_tokens=[v for v in sa if v not in sb], return_only_tokens=[v for v in sb if v not in sa])

def agreed(c):
    return c['state'] in {'exact', 'normalised_exact', 'documented_country_equivalence', 'explicit_number_list_member'}

# Compare source and returned components separately so a disagreement remains visible
def assess(row, c):
    raw = c['raw_candidate']
    a = raw.get('address') or {}
    ev = evaluate_candidate(row, raw, 0.35)
    comparisons = {f: compare(ev[f + '_expected'], a.get('house_number', '') if f == 'house' else ev[f + '_candidate'], f, row.get('ctry_code', '')) for f in ['house', 'road', 'postcode', 'locality']}
    settlement_checks = []
    for key in ('city', 'town', 'village', 'municipality', 'borough', 'suburb', 'quarter', 'hamlet'):
        if a.get(key):
            check = compare(comparisons['locality']['source'], a[key], 'locality', row.get('ctry_code', ''))
            settlement_checks.append(dict(returned_field=key, **check))
    matches = [x for x in settlement_checks if agreed(x)]
    if matches:
        comparisons['locality'] = {**matches[0], 'agreement_rule': 'explicit_returned_settlement_hierarchy'}
    comparisons['locality']['hierarchy_checks'] = settlement_checks
    road = comparisons['road']
    house = comparisons['house']
    post = comparisons['postcode']
    loc = comparisons['locality']
    lengths = {'DE': 5, 'FR': 5, 'IT': 5, 'AT': 4, 'CH': 4, 'FI': 5, 'NO': 4, 'DK': 4, 'IS': 3, 'HU': 4, 'LU': 4}
    length = lengths.get(row.get('ctry_code'))
    if length:
        extracted = []
        for v in [post['source'], post['returned']]:
            match = re.fullmatch('(\\d{' + str(length) + '})\\s+(.+)', str(v))
            extracted.append(match[1] if match and normalize(match[2]) in {normalize(ev.get('locality_candidate')), normalize(prepare_address(row).city)} else v)
        if extracted != [post['source'], post['returned']]:
            refined = compare(*extracted, 'postcode')
            post.update(normalised_source=refined['normalised_source'], normalised_return=refined['normalised_return'], state=refined['state'], agreement_rule='country_formatted_postcode_plus_exact_locality', source_only_tokens=refined['source_only_tokens'], return_only_tokens=refined['return_only_tokens'])
    if not agreed(loc) and ev.get('locality_match') and (not ev.get('road_expected')) and (not ev.get('house_expected')) and (raw.get('addresstype') in {'city', 'town', 'village', 'municipality', 'hamlet', 'suburb', 'borough', 'quarter'}) and (' ' + normalize(loc['returned']) + ' ' in ' ' + normalize(row['address']) + ' '):
        loc.update(state='documented_country_equivalence', agreement_rule='raw_source_exact_locality_component', source_provenance=row['address'], normalised_source=normalize(loc['returned']))
    if not agreed(road) and raw.get('addresstype') == 'road':
        for key, alias in sorted((raw.get('namedetails') or {}).items()):
            if key == 'name' or key.startswith('name:') or key == 'official_name':
                alias_comparison = compare(road['source'], alias, 'road', row.get('ctry_code', ''))
                if agreed(alias_comparison) and road['normalised_source']:
                    road.update(state='documented_country_equivalence', agreement_rule='explicit_returned_road_alias:' + key, matched_alias=alias, normalised_alias=alias_comparison['normalised_return'])
                    break
    method = ev.get('road_match_method', '')
    if not agreed(road) and (method.startswith(('nominatim_road_alias_exact:', 'co_numbered_', 'es_avenue_', 'ca_duplicated_')) or method == 'canonical_direction_order'):
        road.update(state='documented_country_equivalence', agreement_rule=method)
    identifiers = str(a.get('house_number') or '')
    identifier = '\\d+[A-Za-z]?'
    if not agreed(house) and re.fullmatch(identifier + '(?:\\s*[,;]\\s*' + identifier + ')+', identifiers) and (house['normalised_source'] in [normalise(v, 'house') for v in re.split('[,;]', identifiers)]):
        house.update(state='explicit_number_list_member', agreement_rule='explicit_complete_civic_list')
    if not agreed(post) and ev.get('postcode_match') and (ev.get('postcode_match_method') == 'us_zip5_exact'):
        post.update(state='documented_country_equivalence', agreement_rule='US ZIP5')
    if not agreed(loc) and ev.get('locality_match') and (ev.get('locality_match_method') == 'structured_exact'):
        loc.update(state='documented_country_equivalence', agreement_rule='existing_exact_locality_field')
    precision = 'address' if agreed(house) and agreed(road) else 'street' if agreed(road) else 'postcode' if agreed(post) else 'locality' if agreed(loc) and raw.get('addresstype') in {'city', 'town', 'village', 'municipality', 'hamlet', 'suburb', 'borough', 'quarter'} else 'region'
    if ev.get('premise_match'):
        precision = 'address'
    return dict(candidate_id=c['candidate_id'], raw=raw, lat=c['lat'], lon=c['lon'], within_tl3=c['tl3_match'], precision=precision, previous_candidate_precision=c['assessed_granularity'], comparisons=comparisons)
