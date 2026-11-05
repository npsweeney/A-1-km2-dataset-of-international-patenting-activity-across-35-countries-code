"""Compare external patent locations with family grid activity"""

import argparse
import datetime
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from pyproj import Geod, Transformer


# Stream source tables for each comparison pass
def iter_rows(source):
    if isinstance(source, (str, Path)):
        with Path(source).open(newline='', encoding='utf-8-sig') as handle:
            yield from csv.DictReader(handle)
    else:
        yield from source


def read(path):
    return list(iter_rows(path))


# Keep the same coordinate qualification for every external reference
def valid_coordinate(lat, lon):
    return (math.isfinite(lat + lon) and -90 <= lat <= 90
            and -180 <= lon <= 180 and (lat, lon) != (0, 0))


# Read the release identity and stream geographic areas
def support_items(path, grid, run_id):
    import ijson
    identity = {}
    for field in ('grid_identity', 'run_id'):
        with path.open('rb') as handle:
            identity[field] = next(ijson.items(handle, field, use_float=True), None)
    if identity['grid_identity'] != grid or identity['run_id'] != run_id:
        raise ValueError('Geographic support and allocation differ')
    with path.open('rb') as handle:
        array = next((event for prefix, event, value in ijson.parse(handle, use_float=True)
                      if prefix == 'supports'), None)
    if array != 'start_array':
        raise ValueError('Geographic support list is required')
    with path.open('rb') as handle:
        yield from ijson.items(handle, 'supports.item', use_float=True)


def write(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def resolve(entry, parent):
    p = Path(entry['path'])
    return p if p.is_absolute() else parent / p


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    i = (len(values) - 1) * q
    return values[math.floor(i)] * (1 - i % 1) + values[math.ceil(i)] * (i % 1)


def key(r):
    return r['family_id'], r['representative_appln_id'], r['actor'], r['observation_id']


def application_key(row):
    return row['family_id'], row['representative_appln_id'], row['actor']


# Compare all cell weights within one patent and role before averaging across patents
def patent_cell_agreement(our_cells, reference_cells):
    our_total = sum(our_cells.values())
    reference_total = sum(reference_cells.values())
    if our_total <= 0 or reference_total <= 0:
        return None
    cells = our_cells.keys() | reference_cells.keys()
    return 100 * sum(
        min(our_cells.get(c, 0) / our_total, reference_cells.get(c, 0) / reference_total)
        for c in cells
    )


def cell_index(grid):
    transform = Transformer.from_crs('EPSG:4326', grid['crs'], always_xy=True)

    def cell(lat, lon, metres):
        x, y = transform.transform(lon, lat)
        xmin, ymin, xmax, ymax = grid['extent']
        if not math.isfinite(x + y) or not (xmin <= x < xmax and ymin < y <= ymax):
            raise ValueError('Reference coordinate is outside the grid')
        return f"r{math.floor((grid['origin_y'] - y) / metres)}c{math.floor((x - grid['origin_x']) / metres)}"

    return cell


# Compare spatial distributions using shared grid cells, independently of direct-point distance
def compare_grids(estimates, references):
    grid_rows, grid_metrics = [], []
    for factor in (1, 10):
        own, ref = defaultdict(float), defaultdict(float)
        for target, source in ((own, estimates), (ref, references)):
            for (actor, year, c), weight in source.items():
                rr, cc = map(int, re.fullmatch(r'r(\d+)c(\d+)', c).groups())
                target[(actor, year, f'r{rr // factor}c{cc // factor}')] += weight
        for actor, year in sorted({k[:2] for k in own}):
            cells = {k for k in set(own) | set(ref) if k[:2] == (actor, year)}
            mass = sum(ref[k] for k in cells)
            if not math.isclose(sum(own[k] for k in cells), mass, abs_tol=1e-8):
                raise ValueError('Matched-cohort grid totals do not reconcile')
            l1 = sum(abs(own[k] - ref[k]) for k in cells)
            grid_metrics.append(
                dict(
                    actor=actor,
                    patent_year=year,
                    resolution_m=1000 * factor,
                    compared_mass=mass,
                    redistributed_mass_fraction=l1 / (2 * mass),
                )
            )
            for k in sorted(cells):
                grid_rows.append(
                    dict(
                        actor=actor,
                        patent_year=year,
                        resolution_m=1000 * factor,
                        grid_cell_id=k[2],
                        weighted_count=own[k],
                        reference_count=ref[k],
                    )
                )
    return grid_rows, grid_metrics


def run(args):
    if args.output.exists():
        raise FileExistsError(args.output)
    stage6 = json.loads(args.activity_manifest.read_text())
    manifest_path = resolve(stage6['allocation_manifest'], args.activity_manifest.parent)
    stage5 = json.loads(manifest_path.read_text())
    grid = stage5['grid_identity']
    if (
        grid != stage6['grid_identity']
        or grid['resolution_1km'] != 1000
        or grid['resolution_100m'] != 100
    ):
        raise ValueError('Stages 05 and 06 must use the same 100 m / 1 km grid')
    if grid['cell_indexing'] != 'zero_based_row_column':
        raise ValueError('Unsupported grid indexing')
    handoff_path = resolve(stage5['cross_stage_release'], manifest_path.parent)
    handoff = json.loads(handoff_path.read_text())
    support_path = resolve(stage5['support_release'], manifest_path.parent)
    if handoff['source_role_links'].get('before_geographic_filter') is not True:
        raise ValueError('Complete source roster before geographic filtering is required')
    roster = resolve(handoff['source_role_links'], handoff_path.parent)
    cell = cell_index(grid)
    geod = Geod(ellps='WGS84')

    allocated = defaultdict(dict)
    metadata = {}
    family_path = resolve(stage6['artifacts']['family_grid.csv'], args.activity_manifest.parent)
    for r in iter_rows(family_path):
        if not args.start_year <= int(r['patent_year']) <= args.end_year:
            continue
        k = key(r)
        meta = (r['patent_year'], r['address_share'], r['evidence_type'], r['country_code'])
        if k in metadata and metadata[k] != meta:
            raise ValueError('Inconsistent family-address metadata')
        metadata[k] = meta
        if r['grid_cell_id'] in allocated[k] or not re.fullmatch(r'r\d+c\d+', r['grid_cell_id']):
            raise ValueError('Duplicate or invalid family-address grid cell')
        weight = float(r['weight'])
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError('Invalid family weight')
        allocated[k][r['grid_cell_id']] = weight
    for k, cells in allocated.items():
        if not math.isclose(sum(cells.values()), float(metadata[k][1]), abs_tol=1e-9):
            raise ValueError('Spatial allocation does not conserve the address share')

    point_rows, support_rows = [], []
    estimates, references = defaultdict(float), defaultdict(float)
    excluded = defaultdict(int)
    family_rows = (dict(zip(('family_id', 'representative_appln_id', 'actor', 'observation_id'), k),
                        patent_year=meta[0]) for k, meta in metadata.items())
    matches, matching = prepare_matches(args, roster, family_rows)
    by_application = defaultdict(list)
    for row in matches:
        by_application[application_key(row)].append(row)
    patent_rows, patent_cells, single_matches = [], [], []
    matched = set()
    for app, rows in sorted(by_application.items()):
        address_groups = json.loads(rows[0]['source_address_observations'])
        available = {r['observation_id'] for r in rows}
        if not all(set(group) & available for group in address_groups):
            for r in rows:
                args._matching_exclusions.append({**dict(zip(
                    ('family_id', 'representative_appln_id', 'actor', 'observation_id'), key(r))),
                    'reason': 'incomplete_our_allocation'})
            matching['exclusions']['incomplete_our_allocation'] = (
                matching['exclusions'].get('incomplete_our_allocation', 0) + len(rows))
            continue
        our_cells, reference_cells = defaultdict(float), defaultdict(float)
        points = json.loads(rows[0]['reference_locations'])
        year = rows[0]['patent_year']
        for r in rows:
            k = key(r)
            if r['patent_year'] != year or metadata[k][0] != year:
                raise ValueError('Inconsistent application year')
            for c, weight in allocated[k].items():
                our_cells[c] += weight
        for lat, lon in points:
            reference_cells[cell(lat, lon, 1000)] += 1 / len(points)
        mass = sum(our_cells.values())
        common = dict(zip(('family_id', 'representative_appln_id', 'actor'), app))
        common['patent_year'] = year
        patent_rows.append({**common, 'source_addresses': len(address_groups),
            'reference_locations': len(points), 'allocated_mass': mass,
            'agreement_percent': patent_cell_agreement(our_cells, reference_cells)})
        for c in sorted(our_cells.keys() | reference_cells.keys()):
            patent_cells.append({**common, 'grid_cell_id': c,
                'our_spatial_weight': our_cells.get(c, 0) / mass,
                'reference_spatial_weight': reference_cells.get(c, 0)})
            estimates[(app[2], year, c)] += our_cells.get(c, 0)
            references[(app[2], year, c)] += reference_cells.get(c, 0) * mass
        matched.update(key(r) for r in rows)
        if len(rows) == len(points) == 1:
            single_matches.extend(rows)
    matching['identity_matched_address_pairs'] = len(matches)
    matching['matched_address_pairs'] = len(matched)
    matching['matched_application_roles'] = len(patent_rows)
    needed = {r['observation_id'] for r in single_matches}
    runner = {r['observation_id']: r for r in iter_rows(resolve(handoff['runner'], handoff_path.parent))
              if r['observation_id'] in needed}
    supports, area_methods = {}, {}
    for item in support_items(support_path, grid, handoff['run_id']):
        cells = {c['cell_id_100m'] for c in item['accepted_cells']}
        if item['observation_id'] in needed:
            supports[item['observation_id']] = cells
            area_methods[item['observation_id']] = item.get('postcode_construction_method', '')
    for r in single_matches:
        k = key(r)
        if k not in allocated:
            excluded['outside_window_or_unallocated'] += 1
            continue
        year, share, evidence, country = metadata[k]
        if r['patent_year'] != year:
            raise ValueError('Reference and representative application years differ')
        lat, lon = float(r['reference_latitude']), float(r['reference_longitude'])
        if not valid_coordinate(lat, lon):
            raise ValueError('Invalid reference coordinate')
        reference_cell = cell(lat, lon, 1000)
        common = dict(zip(('family_id', 'representative_appln_id', 'actor', 'observation_id'), k))
        common.update(patent_year=year, route=evidence, country_code=country)
        if evidence == 'observed':
            own = runner[k[3]]
            own_lat, own_lon = float(own['observed_latitude']), float(own['observed_longitude'])
            if not (
                math.isfinite(own_lat + own_lon) and -90 <= own_lat <= 90 and -180 <= own_lon <= 180
            ):
                raise ValueError('Invalid direct-address coordinate')
            _, _, distance = geod.inv(lon, lat, own_lon, own_lat)
            point_rows.append({**common, 'distance_m': distance,
                               'reference_cell_1km': reference_cell,
                               'reference_cell_spatial_weight': allocated[k].get(reference_cell, 0) / float(share)})
        elif evidence in {'street', 'postcode', 'postcode_neighbourhood'}:
            if evidence == 'postcode_neighbourhood':
                method = area_methods[k[3]]
                if method not in {
                    'postcode_1km_centre_flood_fill',
                    'postcode_point_single_cell',
                }:
                    raise ValueError('Postcode approximation lacks its recorded area method')
                common['route'] = method
            support_rows.append(
                {
                    **common,
                    'in_100m_support': int(cell(lat, lon, 100) in supports[k[3]]),
                    'reference_cell_1km': reference_cell,
                    'reference_cell_spatial_weight': allocated[k].get(reference_cell, 0)
                    / float(share),
                }
            )
        else:
            raise ValueError('Unknown geographic route')

    fields = [
        'family_id',
        'representative_appln_id',
        'actor',
        'observation_id',
        'patent_year',
        'route',
        'country_code',
    ]
    grid_rows, grid_metrics = compare_grids(estimates, references)
    distances = defaultdict(list)
    for r in point_rows:
        distances[r['actor']].append(r['distance_m'])
    points = {
        actor: dict(
            n=len(v),
            unique_addresses=len({r['observation_id'] for r in point_rows if r['actor'] == actor}),
            median_m=quantile(v, 0.5),
            p90_m=quantile(v, 0.9),
            within_100m=sum(x <= 100 for x in v) / len(v),
            within_1km=sum(x <= 1000 for x in v) / len(v),
        )
        for actor, v in distances.items()
    }
    uncertainty = []
    for actor, route in sorted({(r['actor'], r['route']) for r in support_rows}):
        rs = [r for r in support_rows if (r['actor'], r['route']) == (actor, route)]
        uncertainty.append(
            dict(
                actor=actor,
                route=route,
                n=len(rs),
                support_coverage=sum(r['in_100m_support'] for r in rs) / len(rs),
                mean_reference_cell_spatial_weight=sum(
                    r['reference_cell_spatial_weight'] for r in rs
                )
                / len(rs),
            )
        )
    coverage = []
    withheld_mass = defaultdict(int)
    withheld_without_year = 0
    for r in iter_rows(resolve(stage6['artifacts']['withheld_families.csv'], args.activity_manifest.parent)):
        if not r['patent_year'].isdigit():
            withheld_without_year += 1
        elif r['actor'] in {'applicant', 'inventor'} and args.start_year <= int(r['patent_year']) <= args.end_year:
            withheld_mass[r['actor']] += float(r['withheld_weight'])
    for actor in ('applicant', 'inventor'):
        keys = [k for k in allocated if k[2] == actor]
        mass = sum(sum(allocated[k].values()) for k in keys)
        paired_mass = sum(sum(allocated[k].values()) for k in keys if k in matched)
        coverage.append(
            dict(
                actor=actor,
                allocated_address_pairs=len(keys),
                matched_address_pairs=sum(k in matched for k in keys),
                allocated_mass=mass,
                compared_mass=paired_mass,
                unmatched_allocated_mass=mass - paired_mass,
                withheld_mass=withheld_mass[actor],
            )
        )
    summary = dict(
        activity_manifest=str(args.activity_manifest.resolve()),
        reference_inputs={
            name: str(value.resolve())
            for name, value in vars(args).items()
            if name.startswith(('morrison_', 'de_rassenfosse_')) and value is not None
        },
        matching=matching,
        filing_years=[args.start_year, args.end_year],
        reference=getattr(args, 'reference', 'morrison'),
        id_crosswalk_status=(
            ('exact_application_identity_crosswalk' if getattr(args, 'de_rassenfosse_id_map', None)
             else 'stable_patstat_id_office_full_filing_date')
            if getattr(args, 'reference', 'morrison') == 'de-rassenfosse'
            else 'publication_identity'
        ),
        grid_identity=grid,
        comparison_unit='matched application and role',
        reference_weighting='equal weight per distinct eligible coordinate',
        patent_cell_agreement=[dict(actor=actor, n=len(rs),
            mean_agreement_percent=sum(r['agreement_percent'] for r in rs) / len(rs))
            for actor in ('applicant', 'inventor')
            if (rs := [r for r in patent_rows if r['actor'] == actor])],
        distance_unit='single-address single-reference diagnostics only',
        coverage=coverage,
        excluded_reference_rows=dict(excluded),
        withheld_rows_without_year=withheld_without_year,
        points=points,
        uncertainty=uncertainty,
        aggregates=grid_metrics,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    write(
        args.output / 'matches.csv',
        [r for r in matches if key(r) in matched],
        [
            'family_id',
            'representative_appln_id',
            'actor',
            'observation_id',
            'patent_year',
            'source_observation_ids',
            'source_address_observations',
            'reference_locations',
            'reference_latitude',
            'reference_longitude',
            'reference_location_type',
            'reference_source',
            'match_method',
        ],
    )
    write(
        args.output / 'matching_exclusions.csv',
        getattr(args, '_matching_exclusions', []),
        ['family_id', 'representative_appln_id', 'actor', 'observation_id', 'reason'],
    )
    write(args.output / '01_point_distances.csv', point_rows, fields + ['distance_m', 'reference_cell_1km', 'reference_cell_spatial_weight'])
    write(
        args.output / '02_support_checks.csv',
        support_rows,
        fields + ['in_100m_support', 'reference_cell_1km', 'reference_cell_spatial_weight'],
    )
    write(
        args.output / '03_grid_comparison.csv',
        grid_rows,
        [
            'actor',
            'patent_year',
            'resolution_m',
            'grid_cell_id',
            'weighted_count',
            'reference_count',
        ],
    )
    patent_fields = ['family_id', 'representative_appln_id', 'actor', 'patent_year']
    write(args.output / '04_patent_cell_agreement.csv', patent_rows, patent_fields +
          ['source_addresses', 'reference_locations', 'allocated_mass', 'agreement_percent'])
    write(args.output / '05_patent_cells.csv', patent_cells, patent_fields +
          ['grid_cell_id', 'our_spatial_weight', 'reference_spatial_weight'])
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')


def publication(value, office=''):
    value = value.strip().upper().replace('.', '').replace('/', '')
    if office and office not in {'EPO', 'PCT'}:
        return None
    if office:
        prefix = 'WO' if office == 'PCT' else 'EP'
        value = value if value.startswith(prefix) else prefix + value
    match = re.fullmatch(r'(EP|WO)(\d+)', value)
    if not match:
        return None
    prefix, digits = match.groups()
    if prefix == 'WO' and len(digits) in (7, 8):
        year = int(digits[:2])
        digits = str(1900 + year if year >= 70 else 2000 + year) + digits[2:].zfill(6)
    return prefix + str(int(digits))


# Match eligible references to the same representative application, role and source observation
def prepare_matches(args, roster, family_rows):
    own = {
        key(r): r for r in family_rows if args.start_year <= int(r['patent_year']) <= args.end_year
    }
    apps = {k[1] for k in own}
    nonempty_observations, publications = defaultdict(set), defaultdict(set)
    observation_ids = defaultdict(set)
    address_observations = defaultdict(lambda: defaultdict(set))
    for r in iter_rows(roster):
        app = r['appln_id']
        address = tuple(
            r.get(c, '').strip() for c in ('address', 'city', 'postal_code', 'ctry_code')
        )
        pair = (app, r['actor'])
        observation_id = r['observation_id']
        if app in apps:
            if any(address[:3]):
                nonempty_observations[pair].add(observation_id)
            observation_ids[pair].add(observation_id)
            address_observations[pair][address].add(observation_id)
        pub = publication(r.get('pct_nbr' if r['office'] == 'PCT' else 'pub_nbr', ''), r['office'])
        if pub:
            publications[pub].add(app)
    wanted = {p for p, ids in publications.items() if len(ids) == 1 and ids & apps}
    if getattr(args, 'reference', 'morrison') == 'de-rassenfosse':
        locations, invalid, high, by_app, sources = de_rassenfosse_locations(args, iter_rows(roster), apps)
    else:
        locations, invalid, high, by_app = morrison_locations(args, publications, wanted)
        sources = {}
    rows, excluded = [], defaultdict(int)
    audits = []
    for k, r in sorted(own.items()):
        pair = (k[1], k[2])
        if k[3] not in observation_ids[pair]:
            reason = 'missing_source_observation_identity'
        elif k[3] not in nonempty_observations[pair]:
            reason = 'missing_source_address'
        elif by_app[k[1]] != {r['patent_year']}:
            reason = 'missing_ambiguous_or_different_application_year'
        elif pair in invalid or pair not in high or not locations[pair]:
            reason = 'missing_ambiguous_or_ineligible_reference'
        else:
            points = sorted(locations[pair])
            lat, lon = points[0] if len(points) == 1 else ('', '')
            rows.append(
                {
                    **dict(
                        zip(('family_id', 'representative_appln_id', 'actor', 'observation_id'), k)
                    ),
                    'patent_year': r['patent_year'],
                    'source_observation_ids': json.dumps(sorted(observation_ids[pair])),
                    'source_address_observations': json.dumps([sorted(ids) for _, ids in sorted(address_observations[pair].items())]),
                    'reference_locations': json.dumps(points),
                    'reference_latitude': lat,
                    'reference_longitude': lon,
                    'reference_location_type': {
                        'de-rassenfosse': 'same_filing_geocoded',
                    }.get(getattr(args, 'reference', 'morrison'), 'high'),
                    'reference_source': sources.get(pair, 'publication'),
                    'match_method': 'application_role_all_locations',
                }
            )
            continue
        excluded[reason] += 1
        audits.append(
            {
                **dict(zip(('family_id', 'representative_appln_id', 'actor', 'observation_id'), k)),
                'reason': reason,
            }
        )
    args._matching_exclusions = audits
    return rows, dict(
        allocated_address_pairs=len(own), matched_address_pairs=len(rows), exclusions=dict(excluded),
        reference_acquisition=getattr(args, '_reference_audit', {})
    )


# Qualify descriptor completeness and precise coordinates before accepting Morrison matches
def morrison_locations(args, publications, wanted):
    years = defaultdict(set)
    incomplete = set()
    with args.morrison_descriptors.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle, delimiter='|')
        if not {'pat', 'yr', 'wasComplete'} <= set(reader.fieldnames or []):
            raise ValueError('Morrison descriptors require pat, yr and wasComplete columns')
        for r in reader:
            pub = publication(r['pat'])
            if pub in wanted:
                years[pub].add(r['yr'].strip())
                if r['wasComplete'].strip() != '1':
                    incomplete.add(next(iter(publications[pub])))
    locations, invalid, high = (
        defaultdict(set),
        {(app, actor) for app in incomplete for actor in ('inventor', 'applicant')},
        set(),
    )
    for actor, path in (
        ('inventor', args.morrison_inventors),
        ('applicant', args.morrison_applicants),
    ):
        with path.open(newline='', encoding='utf-8-sig') as handle:
            for r in csv.reader(handle, delimiter='|'):
                if len(r) != 6:
                    raise ValueError(f'Morrison location file requires six columns: {path}')
                pub = publication(r[1])
                if pub not in wanted:
                    continue
                app = next(iter(publications[pub]))
                pair = (app, actor)
                try:
                    lat, lon = map(float, r[3].split(','))
                    valid = valid_coordinate(lat, lon)
                except ValueError:
                    valid = False
                if not valid or r[5].strip() not in {'high', 'low'}:
                    invalid.add(pair)
                else:
                    locations[pair].add((lat, lon))
                    if r[5].strip() == 'high':
                        high.add(pair)
    by_app = defaultdict(set)
    for pub in wanted:
        if pub in years:
            by_app[next(iter(publications[pub]))].update(years[pub])
    return locations, invalid, high, by_app


# Match stable PATSTAT application IDs, office and full date; verify any supplied remapping
def de_rassenfosse_locations(args, roster, apps):
    mapping = getattr(args, 'de_rassenfosse_id_map', None)
    identity_evidence = {}
    targets = None
    if mapping is not None:
        from validation.build_reference_ids import verified_rows

        aliases, reverse = defaultdict(set), defaultdict(set)
        for r in verified_rows(mapping):
            reference = r['reference_appln_id'].strip()
            identity_evidence[reference] = (
                r['appln_auth'].strip().upper(),
                datetime.date.fromisoformat(r['appln_filing_date'].strip()).isoformat(),
            )
            aliases[reference].add(r['regpat_appln_id'].strip())
            reverse[r['regpat_appln_id'].strip()].add(reference)
        targets = {ref: next(iter(ids)) for ref, ids in aliases.items()
                   if len(ids) == 1 and ids & apps and len(reverse[next(iter(ids))]) == 1}
    dates, offices = defaultdict(set), defaultdict(set)
    for r in iter_rows(roster):
        app, office = r['appln_id'], {'EPO': 'EP', 'PCT': 'WO'}.get(r['office'], '')
        if app in apps:
            dates[app].add(r.get('representative_filing_date', ''))
            offices[app].add(office)
    locations, invalid, qualified, years, sources = (
        defaultdict(set),
        set(),
        set(),
        defaultdict(set),
        defaultdict(set),
    )
    for actor, path in (
        ('inventor', args.de_rassenfosse_inventors),
        ('applicant', args.de_rassenfosse_applicants),
    ):
        with path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            required = {
                'appln_id',
                'patent_office',
                'filing_date',
                'lat',
                'lng',
                'source',
                'coord_source',
            }
            if not required <= set(reader.fieldnames or []):
                raise ValueError('De Rassenfosse file is missing required fields')
            for r in reader:
                reference = r['appln_id'].strip()
                app = targets.get(reference) if targets is not None else reference
                if app not in apps:
                    continue
                pair = (app, actor)
                date, office = r['filing_date'].strip(), r['patent_office'].strip().upper()
                if (
                    dates[app] != {date}
                    or offices[app] != {office}
                    or (mapping is not None and identity_evidence[reference] != (office, date))
                ):
                    invalid.add(pair)
                    continue
                years[app].add(date[:4])
                try:
                    lat, lon = float(r['lat']), float(r['lng'])
                    valid = valid_coordinate(lat, lon)
                except ValueError:
                    valid = False
                # Source 1 is the same filing/role; geonames is city-level
                coordinate_source = r['coord_source'].strip().lower()
                if (
                    not valid
                    or r['source'].strip() != '1'
                    or coordinate_source not in {'geolocalization', 'patentsview'}
                ):
                    invalid.add(pair)
                else:
                    locations[pair].add((lat, lon))
                    qualified.add(pair)
                    sources[pair].add('1:' + coordinate_source)
    return (
        locations,
        invalid,
        qualified,
        years,
        {pair: '|'.join(sorted(values)) for pair, values in sources.items()},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity-manifest', type=Path, required=True)
    parser.add_argument(
        '--reference', choices=('morrison', 'de-rassenfosse'), default='morrison'
    )
    parser.add_argument('--morrison-descriptors', type=Path)
    parser.add_argument('--morrison-inventors', type=Path)
    parser.add_argument('--morrison-applicants', type=Path)
    parser.add_argument('--de-rassenfosse-inventors', type=Path)
    parser.add_argument('--de-rassenfosse-applicants', type=Path)
    parser.add_argument('--de-rassenfosse-id-map', type=Path,
                        help='Optional verified application-identity crosswalk for changed IDs')
    parser.add_argument('--output-dir', dest='output', type=Path, required=True)
    parser.add_argument('--start-year', type=int, default=2000)
    parser.add_argument('--end-year', type=int, default=2024)
    args = parser.parse_args()
    if args.start_year > args.end_year:
        parser.error('Start year exceeds end year')
    required = (('de_rassenfosse_inventors', 'de_rassenfosse_applicants')
                if args.reference == 'de-rassenfosse'
                else ('morrison_descriptors', 'morrison_inventors', 'morrison_applicants'))
    for name in required:
        if getattr(args, name) is None:
            parser.error('Required for ' + args.reference + ': --' + name.replace('_', '-'))
    run(args)


if __name__ == '__main__':
    main()
