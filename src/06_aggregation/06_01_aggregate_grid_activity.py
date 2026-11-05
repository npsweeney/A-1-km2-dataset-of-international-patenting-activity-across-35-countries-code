"""Allocate one unit per DOCDB family and role across its representative addresses"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sqlite3
from contextlib import closing, nullcontext
from collections import defaultdict
from itertools import groupby
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
SCHEMA = 'docdb-family-grid-v1'
RULE = 'one per DOCDB family and role; equal person shares followed by spatial allocation'
CELL = re.compile('r(\\d+)c(\\d+)')
IDENTITY = ('family_id', 'location_evidence_id', 'observation_id', 'appln_id', 'person_id', 'actor')

def _iter_rows(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        yield from csv.DictReader(handle)

def _rows(path):
    return list(_iter_rows(path))

def _sealed(entry, parent=None):
    path = Path(entry.get('artifact_path', entry.get('path', '')))
    if not path.is_absolute():
        if parent is None or entry.get('path_basis') != 'manifest_relative':
            raise ValueError('artifact has no explicit path basis')
        path = parent / path
    path = path.resolve(strict=True)
    return path

def _physical_address(row):
    fields = ('address', 'city', 'postal_code', 'ctry_code')
    if all((field in row for field in fields)):
        return tuple((row[field] for field in fields))
    return ('observation', row.get('observation_id', ''))

# Split each family-role unit across distinct participants and their distinct physical addresses
def _address_shares(addresses, source_records, *, application_id, actor, roster=None):
    """Divide the family unit among its source addresses"""
    # Calculate fractions from the complete application-role roster
    name_field = 'inv_name' if actor == 'inventor' else 'app_name'
    rows = roster if roster is not None else [row for records in source_records.values() for row in records]
    rows = [row for row in rows if row.get('appln_id') == application_id and row.get('actor') == actor]
    if not rows:
        return None
    # Deduplicate physical addresses within each named participant
    addresses_by_name = defaultdict(set)
    physical_observations = defaultdict(set)
    for row in rows:
        name, oid = (row.get(name_field, '').strip(), row.get('observation_id', ''))
        if not name.strip() or not oid:
            return None
        physical = _physical_address(row)
        addresses_by_name[name].add(physical)
        physical_observations[physical].add(oid)
    # Divide one family-role unit equally across participants, then their addresses
    count = len(addresses_by_name)
    family_share = Fraction(1, count)
    physical_shares = defaultdict(Fraction)
    for locations in addresses_by_name.values():
        address_share = family_share / len(locations)
        for physical in locations:
            physical_shares[physical] += address_share
    # Deduplicate observations of each physical address
    shares = {oid: Fraction(0) for oid in addresses}
    counts = {oid: 0 for oid in addresses}
    duplicates = {}
    for physical, oids in physical_observations.items():
        selected = oids & set(addresses)
        if selected:
            oid = min(selected)
            shares[oid] = physical_shares[physical]
            counts[oid] = sum((physical in locations for locations in addresses_by_name.values()))
            if len(oids) > 1:
                duplicates[oid] = oids
    # Retain the unlocated fraction instead of redistributing it to located addresses
    unrepresented = 1 - sum(shares.values(), Fraction(0))
    if unrepresented < 0:
        raise ValueError('address shares exceed the family unit')
    return (shares, count, counts, unrepresented, duplicates)

FRACTION_SCHEMA = 'family-address-fractions-v1'

def _identity(path):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns)

def _roster_key(rows, actor):
    name = 'inv_name' if actor == 'inventor' else 'app_name'
    return json.dumps(sorted({(str(row.get(name, '')).strip(), str(row.get('observation_id', '')),
                              _physical_address(row)) for row in rows}), ensure_ascii=False)

def _pack_fractions(value):
    if value is None:
        return None
    shares, people, counts, unrepresented, duplicates = value
    pair = lambda f: [f.numerator, f.denominator]
    return dict(shares={key: pair(value) for key, value in shares.items()}, total_people=people,
                people_at_address=counts, unrepresented=pair(unrepresented),
                duplicates={key: sorted(value) for key, value in duplicates.items()})

def _unpack_fractions(value):
    if value is None:
        return None
    return ({key: Fraction(*number) for key, number in value['shares'].items()},
            value['total_people'], value['people_at_address'], Fraction(*value['unrepresented']),
            {key: set(items) for key, items in value['duplicates'].items()})

# Save the exact roster fractions once so aggregation can reuse them after weighting
def prepare_family_fractions(*, source_links: Path, cohort_manifest: Path, output_dir: Path):
    """Prepare reusable family-role fractions from the complete source roster"""
    source_links = source_links.resolve(strict=True)
    cohort_manifest = cohort_manifest.resolve(strict=True)
    cohort = json.loads(cohort_manifest.read_text())
    if not isinstance(cohort.get('start_year'), int) or not isinstance(cohort.get('end_year'), int):
        raise ValueError('fraction preparation requires an explicit intake year range')
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True)
    source_identity, cohort_identity = _identity(source_links), _identity(cohort_manifest)
    staging = output_dir / 'fraction-preparation.sqlite'
    cache_path = output_dir / 'family-fractions.sqlite'
    started = time.monotonic()
    fields = ('appln_id', 'actor', 'observation_id', 'inv_name', 'app_name', 'address',
              'city', 'postal_code', 'ctry_code', 'family_id', 'patent_year')
    rows_count = units = unresolved = 0
    with closing(sqlite3.connect(staging)) as db:
        db.execute('PRAGMA cache_size=-131072')
        db.execute('CREATE TABLE roster(appln_id TEXT,actor TEXT,payload TEXT)')
        pending = []
        with source_links.open(newline='', encoding='utf-8-sig') as source:
            for row in csv.DictReader(source):
                if not row.get('appln_id') or row.get('actor') not in {'inventor', 'applicant'}:
                    raise ValueError('fraction roster requires linked applications and valid roles')
                if row.get('patent_year') and not cohort['start_year'] <= int(row['patent_year']) <= cohort['end_year']:
                    raise ValueError('source roster differs from the recorded intake year range')
                reduced = {key: row[key] for key in fields if key in row}
                pending.append((row['appln_id'], row['actor'], json.dumps(reduced, ensure_ascii=False)))
                rows_count += 1
                if len(pending) == 20000:
                    db.executemany('INSERT INTO roster VALUES (?,?,?)', pending); db.commit(); pending.clear()
                    print('FRACTIONS input_rows', rows_count, flush=True)
        if pending:
            db.executemany('INSERT INTO roster VALUES (?,?,?)', pending); db.commit()
        expected = cohort.get('counts', {}).get('eligible_links')
        if expected is not None and rows_count != expected:
            raise ValueError('fraction source row count differs from the frozen intake')
        db.execute('CREATE INDEX roster_application_role ON roster(appln_id,actor)'); db.commit()
        with closing(sqlite3.connect(cache_path)) as cache:
            cache.execute('CREATE TABLE fractions(appln_id TEXT,actor TEXT,roster TEXT,payload TEXT,years TEXT,families TEXT,PRIMARY KEY(appln_id,actor)) WITHOUT ROWID')
            cursor = db.execute('SELECT appln_id,actor,payload FROM roster ORDER BY appln_id,actor')
            for (aid, actor), group in groupby(cursor, key=lambda item: item[:2]):
                roster = [json.loads(item[2]) for item in group]
                addresses = {row.get('observation_id', ''): [] for row in roster if row.get('observation_id')}
                value = _address_shares(addresses, {}, application_id=aid, actor=actor, roster=roster)
                packed = _pack_fractions(value)
                if _unpack_fractions(packed) != value:
                    raise ValueError('saved fractions differ from the existing exact calculation')
                if value is not None and sum(value[0].values(), value[3]) != 1:
                    raise ValueError('family/person/address fractions do not conserve one unit')
                cache.execute('INSERT INTO fractions VALUES (?,?,?,?,?,?)',
                              (aid, actor, _roster_key(roster, actor), json.dumps(packed, ensure_ascii=False),
                               json.dumps(sorted({r['patent_year'] for r in roster if r.get('patent_year')})),
                               json.dumps(sorted({r['family_id'] for r in roster if r.get('family_id')}))))
                units += 1; unresolved += value is None
                if units % 20000 == 0:
                    cache.commit(); print('FRACTIONS application_roles', units, flush=True)
            cache.commit()
            if cache.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('fraction cache integrity check failed')
    if _identity(source_links) != source_identity or _identity(cohort_manifest) != cohort_identity:
        raise ValueError('immutable fraction inputs changed during preparation')
    result = dict(schema_version=FRACTION_SCHEMA, counting_rule=RULE, source_links=source_identity,
                  cohort_manifest=cohort_identity, cohort=cohort, source_rows=rows_count,
                  application_role_units=units, unresolved_application_roles=unresolved,
                  exact_roundtrip_verified_for_every_unit=True, source_rosters_unfiltered=True,
                  artifact='family-fractions.sqlite', artifact_identity=_identity(cache_path),
                  seconds=time.monotonic()-started)
    temporary = output_dir / 'manifest.json.partial'
    temporary.write_text(json.dumps(result, indent=2)+'\n'); temporary.replace(output_dir/'manifest.json')
    # Remove the verified task-owned staging copy
    staging.unlink()
    return result

class _PreparedFractions:
    def __init__(self, directory, *, scope_countries=None):
        self.directory = Path(directory).resolve(strict=True)
        self.manifest = json.loads((self.directory/'manifest.json').read_text())
        m = self.manifest
        if m.get('schema_version') != FRACTION_SCHEMA or m.get('counting_rule') != RULE or not m.get('exact_roundtrip_verified_for_every_unit'):
            raise ValueError('invalid or incomplete family fraction cache')
        recorded_scope = m.get('cohort', {}).get('scope_countries')
        if recorded_scope and scope_countries and set(recorded_scope) != set(scope_countries):
            raise ValueError('prepared fractions differ from the geographic country scope')
        artifact = self.directory / m['artifact']
        if _identity(artifact) != m['artifact_identity']:
            raise ValueError('family fraction cache changed since verification')
        self.db = sqlite3.connect(artifact.as_uri()+'?mode=ro', uri=True)
        self.db.execute('PRAGMA cache_size=-131072')

    def close(self):
        self.db.close()

    def get(self, addresses, roster, *, application_id, actor, family_id, patent_year):
        row = self.db.execute('SELECT roster,payload,years,families FROM fractions WHERE appln_id=? AND actor=?', (application_id, actor)).fetchone()
        if row is None or row[0] != _roster_key(roster, actor):
            raise ValueError('prepared family fractions do not bind the full source roster')
        years, families = json.loads(row[2]), json.loads(row[3])
        if years and years != [patent_year] or families and families != [family_id]:
            raise ValueError('prepared fractions differ from the representative family/year')
        result = _unpack_fractions(json.loads(row[1]))
        if result is not None and set(result[0]) != set(addresses):
            raise ValueError('prepared fractions differ from the allocated observation cohort')
        return result

# Read allocation labels without retaining every 100 m geometry cell in memory
def _support_metadata(path):
    import ijson
    result = {}
    with path.open('rb') as handle:
        for prefix, event, value in ijson.parse(handle, use_float=True):
            if prefix in {'sealed', 'run_id'} and event not in {'start_map', 'end_map', 'start_array', 'end_array', 'map_key'}:
                result[prefix] = value
            if {'sealed', 'run_id'} <= result.keys():
                break
    fields = ('observation_id', 'postcode_construction_method', 'output_variant', 'support_authority_label')
    with path.open('rb') as handle:
        result['supports'] = [{key: item.get(key) for key in fields} for item in ijson.items(handle, 'supports.item', use_float=True)]
    return result

# Apply family fractions to spatial weights; retain withheld shares and separate output tiers
def build(*, allocation_manifest: Path, family_backbone: Path, output_dir: Path, family_fractions: Path | None=None):
    """Combine address allocations into DOCDB family contributions"""
    if output_dir.exists():
        raise FileExistsError(output_dir)
    allocation_manifest = allocation_manifest.resolve(strict=True)
    manifest = json.loads(allocation_manifest.read_text())
    if manifest.get('schema_version') != 'imputation_final_grid_1km_v1' or manifest.get('manifest_contract_version') != 'imputation-final-grid-manifest-v2':
        raise ValueError('Expected grid allocation files from stage 05')
    cross_path = _sealed(manifest['cross_stage_release'])
    cross = json.loads(cross_path.read_text())
    support_path = _sealed(manifest['support_release'])
    support = _support_metadata(support_path)
    if support.get('sealed') is not True or support.get('run_id') != cross.get('run_id'):
        raise ValueError('support release does not bind the same geographic handoff')
    _sealed(manifest['grid_contract'])
    temporal_path = _sealed(cross['temporal_child'])
    temporal = _rows(temporal_path)
    cell_path = _sealed(manifest['artifacts']['estimated_grid_1km.csv'], allocation_manifest.parent)
    withheld_path = _sealed(manifest['artifacts']['withheld.csv'], allocation_manifest.parent)
    audit_path = _sealed(manifest['artifacts']['audit.json'], allocation_manifest.parent)
    audit = json.loads(audit_path.read_text())
    grid = manifest['grid_identity']
    if grid != audit['grid_identity'] or grid.get('resolution_1km') != 1000:
        raise ValueError('allocation grid identity is inconsistent')
    extent = grid['extent']
    width, height = (int((extent[2] - extent[0]) / 1000), int((extent[3] - extent[1]) / 1000))
    allocated = defaultdict(dict)
    details = {}
    neighbourhood_ids = {str(s['observation_id']) for s in support.get('supports', [])
                         if s.get('postcode_construction_method') in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}
                         and s.get('output_variant') in {'postcode_point_neighbourhood_sensitivity', 'postcode_1km_approximation'}
                         and s.get('support_authority_label') == 'approximate_neighbourhood_not_postcode_boundary'}
    neighbourhood_scenarios = {str(s['observation_id']): 'postcode_1km_centre_built_surface_approximation' for s in support.get('supports', []) if str(s['observation_id']) in neighbourhood_ids}
    for row in _iter_rows(cell_path):
        key = tuple((row[field] for field in IDENTITY))
        cell = row['grid_cell_id']
        match = CELL.fullmatch(cell)
        if not match or not (0 <= int(match[1]) < height and 0 <= int(match[2]) < width):
            raise ValueError('allocation cell is outside its declared grid')
        weight = Decimal(row['normalized_weight'])
        if not weight.is_finite() or weight <= 0 or cell in allocated[key]:
            raise ValueError('duplicate or invalid cell allocation')
        allocated[key][cell] = weight
        metadata = tuple((row[field] for field in ('patent_year', 'selected_ghsl_epoch', 'scenario_id', 'scenario_version', 'evidence_type', 'country_code', 'country_group')))
        if key in details and details[key] != metadata:
            raise ValueError('inconsistent metadata within an allocation')
        details[key] = metadata
    withheld = {}
    for row in _iter_rows(withheld_path):
        key = tuple((row[field] for field in IDENTITY))
        if key in withheld:
            raise ValueError('duplicate withheld application link')
        withheld[key] = row
    source = {tuple((row[field] for field in IDENTITY)): row for row in temporal}
    if len(source) != len(temporal) or set(allocated) & set(withheld) or set(source) != set(allocated) | set(withheld):
        raise ValueError('allocated/withheld application links do not reconcile to source')
    if any((sum(map(Fraction, cells.values()), Fraction(0)) != 1 for cells in allocated.values())):
        raise ValueError('application allocation does not conserve one unit')
    for key in source:
        if key in allocated:
            neighbourhood = details[key][4] == 'postcode_neighbourhood'
            if (details[key][0] != source[key]['patent_year'] or
                details[key][4] not in {'observed', 'street', 'postcode', 'postcode_neighbourhood'} or
                (neighbourhood and (source[key]['observation_id'] not in neighbourhood_ids or
                 details[key][2] != neighbourhood_scenarios.get(source[key]['observation_id']))) or
                (not neighbourhood and details[key][2] in {'postcode_1km_centre_built_surface_approximation'})):
                raise ValueError('allocation changed source year or evidence type')
        elif withheld[key]['patent_year'] != source[key]['patent_year']:
            raise ValueError('withheld link changed source filing year')
    family_backbone = family_backbone.resolve(strict=True)
    families = {}
    with closing(sqlite3.connect(family_backbone.as_uri() + '?mode=ro', uri=True)) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(regpat_family_backbone)')}
        conflict = 'family_resolution_conflict' if 'family_resolution_conflict' in columns else '0'
        for row in db.execute(f'SELECT appln_id, canonical_family_id, resolution_status, family_id_kind, {conflict} FROM regpat_family_backbone'):
            families[str(row[0])] = row[1:]
    source_records = defaultdict(list)
    link_entry = cross.get('inputs', {}).get('address_links.csv')
    if link_entry:
        for row in _rows(_sealed(link_entry)):
            key = (row['appln_id'], row['person_id'], row['observation_id'], row['actor'])
            source_records[key].append(row)
        expected_links = {(key[3], key[4], key[2], key[5]) for key in source}
        if set(source_records) != expected_links:
            raise ValueError('person-fraction source links do not match the allocated cohort')
    roster_entry = cross.get('source_role_links')
    roster = _rows(_sealed(roster_entry)) if roster_entry else None
    grouped = defaultdict(lambda: defaultdict(list))
    representatives = {}
    for key, row in source.items():
        if row['actor'] not in {'applicant', 'inventor'}:
            raise ValueError('invalid source role')
        family = families.get(row['appln_id'])
        if family is None and row['family_id'] != 'unmatched:' + row['appln_id']:
            raise ValueError('missing family backbone application lacks unmatched identity')
        if family is not None and str(family[0]) != row['family_id']:
            raise ValueError('allocation family differs from existing backbone')
        representative = (row['appln_id'], row['patent_year'])
        if row['family_id'] in representatives and representatives[row['family_id']] != representative:
            raise ValueError('DOCDB family must have one representative application and filing year; rerun stage 01')
        representatives[row['family_id']] = representative
        grouped[row['family_id'], row['actor']][row['observation_id']].append(key)
    if roster is not None:
        for row in roster:
            fid = row.get('family_id', '')
            if fid in representatives:
                aid, year = representatives[fid]
                if row.get('appln_id') != aid or row.get('patent_year') != year:
                    raise ValueError('role roster differs from selected DOCDB representative')
                if row.get('actor') not in {'applicant', 'inventor'}:
                    raise ValueError('role roster has invalid actor')
                grouped.setdefault((fid, row['actor']), {})

    source_by_application_role = defaultdict(list)
    full_links = roster if roster is not None else [row for records in source_records.values() for row in records]
    for row in full_links:
        source_by_application_role[row['appln_id'], row['actor']].append(row)

    def number(value):
        return str(Decimal(value.numerator) / Decimal(value.denominator))
    missing_roles = [dict(family_id=fid, representative_appln_id=aid, patent_year=year, actor=actor)
                     for fid, (aid, year) in sorted(representatives.items())
                     for actor in ('applicant', 'inventor') if (fid, actor) not in grouped]
    fact_cells, highest_weight_cells, held, activity = ([], [], [], defaultdict(Fraction))
    accepted = partial = fully_withheld = fully_allocated = 0
    held_mass = Fraction(0)
    metadata_fields = ('patent_year', 'selected_ghsl_epoch', 'scenario_id', 'scenario_version', 'evidence_type', 'country_code', 'country_group')
    with closing(_PreparedFractions(family_fractions, scope_countries=cross.get('scope_countries'))) if family_fractions else nullcontext(None) as prepared_fractions:
        for (fid, actor), addresses in sorted(grouped.items()):
            aid, year = representatives[fid]
            if prepared_fractions:
                fractions = prepared_fractions.get(addresses, source_by_application_role[aid, actor],
                                                   application_id=aid, actor=actor, family_id=fid, patent_year=year)
            else:
                fractions = _address_shares(addresses, source_records, application_id=aid, actor=actor,
                                           roster=source_by_application_role[aid, actor])
            if fractions is None:
                held.append(dict(family_id=fid, representative_appln_id=aid, observation_id='', actor=actor, patent_year=year, address_share='1', people_at_address='', total_people='', reason='person_fraction_unresolved', withheld_weight='1'))
                held_mass += 1
                fully_withheld += 1
                continue
            shares, total_people, people_at_address, unrepresented, duplicates = fractions
            if unrepresented:
                held.append(dict(family_id=fid, representative_appln_id=aid, observation_id='', actor=actor, patent_year=year, address_share=number(unrepresented), people_at_address='', total_people=total_people, reason='outside_allocated_cohort_or_unrepresented_person', withheld_weight=number(unrepresented)))
                held_mass += unrepresented
            supported = 0
            for oid, keys in sorted(addresses.items()):
                share = shares[oid]
                if not share:
                    continue
                common = dict(family_id=fid, representative_appln_id=aid, observation_id=oid, actor=actor, patent_year=year, address_share=number(share), people_at_address=people_at_address[oid], total_people=total_people)
                reasons = set()
                for key in keys:
                    family = families.get(key[3])
                    if family is None:
                        reasons.add('application_unmatched')
                    elif family[3]:
                        reasons.add('family_source_disagreement')
                    elif family[1] != 'resolved':
                        reasons.add('family_' + family[1])
                    elif family[2] != 'docdb':
                        reasons.add('family_non_docdb')
                    if key in withheld:
                        reasons.add(withheld[key]['reason'])
                if reasons:
                    held.append({**common, 'reason': ';'.join(sorted(reasons)), 'withheld_weight': number(share)})
                    held_mass += share
                    continue
                first = keys[0]
                duplicate_keys = [key for other in duplicates.get(oid, {oid}) for key in addresses.get(other, [])]
                if oid in duplicates and (not duplicates[oid] <= set(addresses) or any((key not in allocated or allocated[key] != allocated[first] or details[key] != details[first] for key in duplicate_keys))):
                    held.append({**common, 'reason': 'same_physical_address_conflicting_or_missing_geography', 'withheld_weight': number(share)})
                    held_mass += share
                    continue
                if any((allocated[key] != allocated[first] or details[key] != details[first] for key in keys)):
                    raise ValueError('representative address has inconsistent geographic allocations')
                supported += 1
                metadata = dict(zip(metadata_fields, details[first]))
                best_cell = max(sorted(allocated[first], key=lambda cell: tuple(map(int, CELL.fullmatch(cell).groups()))),
                                key=allocated[first].get)
                highest_weight_cells.append({**common, **metadata, 'grid_cell_id': best_cell, 'weight': share})
                for cell, address_weight in sorted(allocated[first].items()):
                    weight = share * Fraction(address_weight)
                    fact_cells.append({**common, **metadata, 'grid_cell_id': cell, 'weight': weight})
                    activity[cell, actor, *details[first]] += weight
            accepted += supported > 0
            fully_allocated += supported == sum((bool(value) for value in shares.values())) and (not unrepresented)
            partial += supported > 0 and (supported < sum((bool(value) for value in shares.values())) or bool(unrepresented))
            fully_withheld += supported == 0
    allocated_mass = sum(activity.values(), Fraction(0))
    if allocated_mass + held_mass != len(grouped):
        raise ValueError('family allocation and withheld shares do not conserve one unit per family and role')
    if sum((row['weight'] for row in highest_weight_cells), Fraction(0)) != allocated_mass:
        raise ValueError('highest-weight cell cells do not conserve the allocated address shares')
    output_dir.mkdir(parents=True)

    def write(name, fields, rows):
        path = output_dir / name
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(({key: number(value) if isinstance(value, Fraction) else value for key, value in row.items()} for row in rows))
        return dict(path=str(path.resolve()), rows=len(rows))
    common_fields = ('family_id', 'representative_appln_id', 'observation_id', 'actor', 'patent_year', 'address_share', 'people_at_address', 'total_people')
    artifacts = {'family_grid.csv': write('family_grid.csv', (*common_fields, *metadata_fields[1:], 'grid_cell_id', 'weight'), fact_cells), 'withheld_families.csv': write('withheld_families.csv', (*common_fields, 'reason', 'withheld_weight'), held)}
    artifacts['family_highest_weight_cell_grid.csv'] = write('family_highest_weight_cell_grid.csv', (*common_fields, *metadata_fields[1:], 'grid_cell_id', 'weight'), highest_weight_cells)
    artifacts['missing_roles.csv'] = write('missing_roles.csv', ('family_id', 'representative_appln_id', 'patent_year', 'actor'), missing_roles)
    tiers = {'direct': {'observed'}, 'direct_street': {'observed', 'street'}, 'direct_street_pcdb': {'observed', 'street', 'postcode'}, 'direct_street_pcdb_rg': {'observed', 'street', 'postcode', 'postcode_neighbourhood'}}
    output_totals = {}
    for role, label in (('applicant', 'applicants'), ('inventor', 'inventors')):
        for tier, evidence_types in tiers.items():
            for suffix, facts in (('', fact_cells), ('_highest_weight_cell', highest_weight_cells)):
                totals = defaultdict(Fraction)
                for row in facts:
                    if row['actor'] == role and row['evidence_type'] in evidence_types:
                        totals[row['grid_cell_id'], row['patent_year']] += row['weight']
                name = f'{label}_{tier}{suffix}.csv'
                rows = [dict(grid_cell_id=cell, patent_year=year, activity=number(value)) for (cell, year), value in sorted(totals.items())]
                artifacts[name] = write(name, ('grid_cell_id', 'patent_year', 'activity'), rows)
                output_totals[name] = number(sum(totals.values(), Fraction(0)))
    from run import code_revision
    result = dict(code_revision=code_revision(),schema_version=SCHEMA, counting_rule=RULE, allocation_manifest=dict(path=str(allocation_manifest)), family_backbone=dict(path=str(family_backbone)), grid_identity=grid, source_application_links=len(source), docdb_families=len(representatives), family_role_units=len(grouped), distinct_family_addresses=sum((len(addresses) for addresses in grouped.values())), accepted_facts=accepted, withheld_facts=len(held), fully_allocated_family_roles=fully_allocated, partially_allocated_family_roles=partial, fully_withheld_family_roles=fully_withheld, allocated_mass=number(allocated_mass), withheld_mass=number(held_mass), output_totals=output_totals, artifacts=artifacts)
    result['missing_role_counts'] = {role: sum(row['actor'] == role for row in missing_roles)
                                   for role in ('applicant', 'inventor')}
    result['highest_weight_cell_cell_rule'] = 'largest summed 1 km spatial weight per address; ties by row then column'
    if family_fractions:
        result['family_fraction_cache'] = str(Path(family_fractions).resolve())
    (output_dir / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    return result

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allocation-manifest', type=Path)
    parser.add_argument('--prepare-family-fractions', action='store_true')
    parser.add_argument('--source-links', type=Path)
    parser.add_argument('--cohort-manifest', type=Path)
    parser.add_argument('--family-fractions', type=Path)
    parser.add_argument('--family-backbone', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.prepare_family_fractions:
        if not args.source_links or not args.cohort_manifest:
            parser.error('fraction preparation requires --source-links and --cohort-manifest')
        prepare_family_fractions(source_links=args.source_links, cohort_manifest=args.cohort_manifest, output_dir=args.output_dir)
    else:
        if not args.allocation_manifest or not args.family_backbone:
            parser.error('aggregation requires --allocation-manifest and --family-backbone')
        build(allocation_manifest=args.allocation_manifest, family_backbone=args.family_backbone, output_dir=args.output_dir, family_fractions=args.family_fractions)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
