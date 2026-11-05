"""Apply year-specific built-surface weights and aggregate to 1 km cells"""
from __future__ import annotations
import argparse
import csv
from uuid import uuid4
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict, OrderedDict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from geographic_uncertainty.contracts import COUNTRY_FIELDS, TEMPORAL_FIELDS, temporal_link_key
from geographic_uncertainty.routing import ROUTING_FIELDS, atomic_write
SCHEMA_VERSION = 'imputation_final_grid_1km_v1'
SCENARIO_ID = 'ghsl_built_surface_100m_to_1km'
SCENARIO_VERSION = '2'
MAX_EXPANDED_CELL_ROWS = 50000
OFFICIAL_GHSL_EPOCHS = tuple(range(1975, 2035, 5))
WIDTH_100M = 360820
HEIGHT_100M = 180000
METHODS = {'address_passthrough': 'observed_coordinate_passthrough', 'street_support_request': 'actual_street_geometry_100m_support', 'postcode_support_request': 'nominatim_derived_postcode_support'}
LINK_FIELDS = TEMPORAL_FIELDS[:6]
CELL_FIELDS = (*LINK_FIELDS, 'patent_year', 'selected_ghsl_epoch', 'estimate_id', 'scenario_id', 'scenario_version', 'evidence_type', 'country_code', 'country_group', 'grid_cell_id', 'normalized_weight', 'support_id')
WITHHELD_FIELDS = (*TEMPORAL_FIELDS, 'country_code', 'country_group', 'reason')
GRID_FIELDS = {'ghsl_registry_version', 'crs', 'origin_x', 'origin_y', 'extent', 'cell_indexing', 'resolution_100m', 'resolution_1km', 'parent_mapping'}
CELL_ID = re.compile('^r(-?\\d+)c(-?\\d+)$')
OUTCOME_FIELDS = ('observation_id', 'validation_id', 'input_route', 'status', 'support_method', 'support_id', 'reason', 'pending_request_ids', 'source_artifact_role')
GHSL_SUPPORT_FAILURE_REASONS = {'ghsl_nodata_in_support', 'zero_built_surface_mass'}

def _sum_precision(values):
    integer_digits = max((max(value.adjusted() + 1, 1) for value in values), default=1)
    decimal_places = max((max(-value.as_tuple().exponent, 0) for value in values), default=0)
    return max(28, integer_digits + decimal_places + len(str(len(values))) + 1)

def _exact_decimal_sum(values):
    with localcontext() as context:
        context.prec = _sum_precision(values)
        return sum(values, Decimal(0))

# Divide by total built surface and assign rounding remainder deterministically to conserve mass
def _normalized_weights(raw):
    total = _exact_decimal_sum(raw)
    if total <= 0:
        raise ValueError('accepted support has zero built-surface mass')
    weights = [value / total for value in raw]
    largest = max(range(len(raw)), key=raw.__getitem__)
    with localcontext() as context:
        context.prec = _sum_precision(weights)
        weights[largest] += Decimal(1) - sum(weights, Decimal(0))
    return weights

def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()

def _csv_bytes(fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> bytes:
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fields, lineterminator='\n', extrasaction='raise')
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()

def _read_exact(path: Path, fields: Sequence[str]) -> list[dict[str, str]]:
    with path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise ValueError(f'{path.name} schema/order is not exact')
        return list(reader)

def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('built-surface weight must be finite and non-negative') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('built-surface weight must be finite and non-negative')
    return result

def _sealed_path(entry: Mapping[str, object], role: str) -> Path:
    path = Path(str(entry.get('artifact_path', entry.get('path', ''))))
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f'sealed {role} artifact mismatch')
    return path

# Use the latest GHSL epoch no later than the representative filing year
def select_epoch(year: int, published_epochs: Sequence[int]) -> int | None:
    """Select the official latest published GHSL epoch no later than the year"""
    eligible = [epoch for epoch in published_epochs if epoch <= year]
    return max(eligible) if eligible else None

def select_available_epoch(year: int, published_epochs: Sequence[int], available_epochs: Sequence[int]) -> tuple[int | None, bool]:
    """Select the latest published epoch no later than the patent year"""
    selected = select_epoch(year, published_epochs)
    return (selected, selected is not None and selected in available_epochs)

def _verify_support_artifacts(support_release: Mapping[str, object], expected_roles: set[str]) -> dict[str, Path]:
    artifacts = support_release.get('artifacts')
    if not isinstance(artifacts, list):
        raise ValueError('support artifact ledger is missing')
    by_role: dict[str, Path] = {}
    for entry in artifacts:
        if not isinstance(entry, Mapping):
            raise ValueError('support artifact entry is invalid')
        role = str(entry.get('role', ''))
        if not role or role in by_role:
            raise ValueError('support artifact roles must be unique and nonblank')
        path = Path(str(entry.get('path', '')))
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'sealed support artifact mismatch: {role}')
        by_role[role] = path
    if set(by_role) != expected_roles:
        raise ValueError('support artifact roles are not exact')
    return by_role

def _validate_grid(support_release: Mapping[str, object]) -> Mapping[str, object]:
    contract_entry = support_release.get('grid_contract')
    if not isinstance(contract_entry, Mapping):
        raise ValueError('canonical GHSL grid contract is missing')
    contract_path = Path(str(contract_entry.get('path', '')))
    if contract_entry.get('schema_version') != 'gpd-ghsl-grid-contract-v1' or contract_entry.get('version') != 'gpd-ghsl-mollweide-100m-1km-v2' or (not contract_path.is_absolute()) or (not contract_path.is_file()):
        raise ValueError('canonical GHSL grid contract identity is invalid')
    contract = json.loads(contract_path.read_text())
    if contract.get('schemaVersion') != contract_entry['schema_version'] or contract.get('gridVersion') != contract_entry['version']:
        raise ValueError('canonical GHSL grid contract content is invalid')
    grid = support_release.get('grid_identity')
    if not isinstance(grid, Mapping) or GRID_FIELDS - set() - set(grid):
        raise ValueError('fixed GHSL grid identity is incomplete')
    if grid['resolution_100m'] != 100 or grid['resolution_1km'] != 1000:
        raise ValueError('GHSL grid resolutions are invalid')
    if grid['parent_mapping'] != 'parent_row=floor(row_100m/10);parent_col=floor(col_100m/10)':
        raise ValueError('100 m to 1 km parent rule is not canonical')
    canonical = {'ghsl_registry_version': contract['ghslRegistryVersion'], 'crs': contract['crs'], 'origin_x': contract['originX'], 'origin_y': contract['originY'], 'extent': contract['extent'], 'cell_indexing': contract['cellIndexing'], 'resolution_100m': contract['resolution100m'], 'resolution_1km': contract['resolution1km'], 'parent_mapping': contract['parentMapping']}
    if any((grid.get(field) != value for field, value in canonical.items())):
        raise ValueError('grid identity is not the signed canonical GHSL grid')
    return grid

# Validate immutable batch inputs once for repeated source links
def _prepare_allocation_inputs(cross_stage_release, routing_rows, country_rows, support_release):
    if cross_stage_release.get('schema_version') not in {'imputation_v3_cross_stage_release_v1', 'patent_geographic_handoff_v1'} or support_release.get('schema_version') != 'imputation_100m_support_release_v1' or (support_release.get('sealed') is not True) or (support_release.get('run_id') != cross_stage_release.get('run_id')):
        raise ValueError('sealed cross-stage/support release identity is invalid')
    grid = _validate_grid(support_release)
    raster_assets = support_release.get('ghsl_built_assets')
    if not isinstance(raster_assets, Mapping):
        raise ValueError('GHSL built asset registry is missing')
    support_items = support_release.get('supports')
    if not isinstance(support_items, list):
        raise ValueError('support release has no canonical support set')
    expected_roles = {'ghsl_registry', 'ghsl_values', 'stage_a_outcomes', 'stage_a_outcomes_manifest'}
    expected_roles.update((str(asset.get('artifact_role', '')) for asset in raster_assets.values()))
    for item in support_items:
        source_artifacts = item.get('source_artifacts')
        if not isinstance(source_artifacts, Mapping):
            raise ValueError('support source-artifact roles are missing')
        expected_roles.update((str(role) for role in source_artifacts.values()))
    artifact_paths = _verify_support_artifacts(support_release, expected_roles)
    registry = json.loads(artifact_paths['ghsl_registry'].read_text())
    if registry.get('product') != 'GHS-BUILT-S' or registry.get('release') != 'R2023A' or registry.get('measure') != 'built_surface_m2':
        raise ValueError('GHSL registry source identity is invalid')
    registry_grid = {key: value for key, value in grid.items()}
    if registry.get('grid_identity') != registry_grid:
        raise ValueError('GHSL registry grid identity mismatch')
    ghsl_values = json.loads(artifact_paths['ghsl_values'].read_text())
    ghsl_source = {'product': registry.get('product'), 'release': registry.get('release'), 'measure': registry.get('measure')}
    if ghsl_values.get('schema_version') != 'ghsl_100m_values_v1' or ghsl_values.get('grid_identity') != grid or ghsl_values.get('ghsl_source') != ghsl_source or (support_release.get('ghsl_source') != ghsl_source):
        raise ValueError('GHSL values grid identity mismatch')
    for epoch, asset in raster_assets.items():
        if not str(epoch).isdigit() or not isinstance(asset, Mapping) or asset.get('grid_identity') != grid or (asset.get('artifact_role') not in artifact_paths):
            raise ValueError('GHSL epoch asset/grid identity is invalid')
    epoch_scope = support_release.get('ghsl_epoch_scope')
    if not isinstance(epoch_scope, Mapping):
        raise ValueError('sealed GHSL epoch scope is missing')
    published_epochs = epoch_scope.get('published_epochs')
    available_epochs = epoch_scope.get('available_epochs')
    if published_epochs != list(OFFICIAL_GHSL_EPOCHS) or not isinstance(available_epochs, list) or (not available_epochs) or any((not isinstance(epoch, int) for epoch in available_epochs)) or (sorted(set(available_epochs)) != available_epochs) or (not set(available_epochs) <= set(OFFICIAL_GHSL_EPOCHS)) or (set(available_epochs) != {int(epoch) for epoch in raster_assets}) or (registry.get('published_epochs') != published_epochs) or (registry.get('available_epochs') != available_epochs) or (registry.get('complete_temporal_registry') is not (available_epochs == list(OFFICIAL_GHSL_EPOCHS))) or (epoch_scope.get('selection_rule') != 'latest_published_epoch_no_later_than_patent_year_then_require_available'):
        raise ValueError('sealed GHSL published/available epoch scope is invalid')
    values_by_support = ghsl_values.get('values_by_support')
    if not isinstance(values_by_support, Mapping):
        raise ValueError('GHSL values artifact is invalid')
    outcome_rows = _read_exact(artifact_paths['stage_a_outcomes'], OUTCOME_FIELDS)
    outcome_manifest = json.loads(artifact_paths['stage_a_outcomes_manifest'].read_text())
    outcome_artifacts = outcome_manifest.get('artifacts')
    sealed_outcomes = outcome_artifacts.get('support_outcomes.csv') if isinstance(outcome_artifacts, Mapping) else None
    if outcome_manifest.get('schema_version') != 'imputation_stage_a_outcomes_v1' or outcome_manifest.get('run_id') != support_release.get('run_id') or (not isinstance(sealed_outcomes, Mapping)):
        raise ValueError('Stage-A outcomes manifest binding is invalid')
    outcomes = {row['observation_id']: row for row in outcome_rows}
    validation_ids = [row['validation_id'] for row in outcome_rows]
    if '' in outcomes or len(outcomes) != len(outcome_rows) or '' in validation_ids or (len(set(validation_ids)) != len(validation_ids)):
        raise ValueError('Stage-A outcome identities must be unique and nonblank')
    if any((row['status'] == 'pending' for row in outcome_rows)):
        raise ValueError('pending Stage-A outcomes prevent final weighting')
    if any((row['status'] not in {'support_ready', 'withheld'} for row in outcome_rows)):
        raise ValueError('Stage-A outcome status is invalid')
    for row in outcome_rows:
        if json.loads(row['pending_request_ids']):
            raise ValueError('spatial support has pending requests')
        if row['status'] == 'support_ready' and (not row['support_method'] or not row['support_id'] or (not row['source_artifact_role']) or row['reason']):
            raise ValueError('Stage-A ready outcome fields are invalid')
        if row['status'] == 'withheld' and (not row['reason'] or row['support_method'] or row['support_id'] or row['source_artifact_role']):
            raise ValueError('Stage-A withheld outcome fields are invalid')
    routes = {str(row.get('observation_id', '')): row for row in routing_rows}
    countries = {str(row.get('observation_id', '')): (row.get('ctry_code'), row.get('country_group')) for row in country_rows}
    supports = {str(row.get('observation_id', '')): row for row in support_items}
    if any(('' in index or len(index) != len(rows) for index, rows in ((routes, routing_rows), (countries, country_rows), (supports, support_items)))):
        raise ValueError('observation identities must be unique and nonblank')
    if set(outcomes) != set(routes):
        raise ValueError('Stage-A outcomes must exactly cover immutable routing')
    for oid, outcome in outcomes.items():
        if outcome['input_route'] != routes[oid]['route']:
            raise ValueError('Stage-A outcome route does not match immutable routing')
        if outcome['status'] == 'withheld' and (not outcome['reason']):
            raise ValueError('withheld Stage-A outcome requires an explicit reason')
    support_ids = [str(item.get('support_id', '')) for item in support_items]
    if '' in support_ids or len(set(support_ids)) != len(support_ids):
        raise ValueError('support IDs must be unique and nonblank')
    accepted_route_ids = {oid for oid, row in routes.items() if row.get('status') == 'accepted' and row.get('route') in METHODS}
    ready_ids = {oid for oid, row in outcomes.items() if row['status'] == 'support_ready'}
    if set(supports) != ready_ids or not ready_ids <= accepted_route_ids:
        raise ValueError('support coverage must exactly match Stage-A ready observations')
    for oid in ready_ids:
        route = routes[oid]
        outcome = outcomes[oid]
        if outcome['input_route'] != route['route'] or outcome['support_method'] != METHODS[route['route']] or outcome['support_id'] != str(supports[oid].get('support_id', '')):
            raise ValueError('Stage-A ready outcome does not match sealed support')
    if set(values_by_support) != {str(item['support_id']) for item in support_items}:
        raise ValueError('GHSL values must exactly cover sealed supports')
    support_failures = ghsl_values.get('support_failures_by_epoch')
    if not isinstance(support_failures, Mapping) or support_release.get('support_failures_by_epoch') != support_failures:
        raise ValueError('GHSL support-failure ledger is absent or inconsistent')
    available_epoch_keys = {str(epoch) for epoch in available_epochs}
    methods_by_support = {str(item['support_id']): str(item['support_method']) for item in support_items}
    cells_by_support = {str(item['support_id']): {str(cell.get('cell_id_100m', '')) for cell in item.get('accepted_cells', []) if isinstance(cell, Mapping)} for item in support_items}
    if not set(support_failures) <= set(methods_by_support):
        raise ValueError('GHSL support-failure ledger names an unknown support')
    for support_id, failures_by_epoch in support_failures.items():
        if methods_by_support[support_id] == METHODS['address_passthrough'] or not isinstance(failures_by_epoch, Mapping) or (not set(failures_by_epoch) <= available_epoch_keys):
            raise ValueError('GHSL support-failure entry is invalid')
        for failure in failures_by_epoch.values():
            if not isinstance(failure, Mapping) or failure.get('reason') not in GHSL_SUPPORT_FAILURE_REASONS:
                raise ValueError('GHSL support-failure entry is invalid')
            cell_ids = failure.get('cell_ids')
            if not isinstance(cell_ids, list) or len(cell_ids) != len(set(cell_ids)) or (not set(cell_ids) <= cells_by_support[support_id]) or ((failure['reason'] == 'ghsl_nodata_in_support') is not bool(cell_ids)):
                raise ValueError('GHSL support-failure cell evidence is invalid')
    return (grid, support_items, artifact_paths, routes, countries, supports, outcomes, values_by_support, support_failures, published_epochs, available_epochs)

# Calculate spatial shares on the 100 m grid and sum them into aligned 1 km cells
def build(*, cross_stage_release: Mapping[str, object], routing_rows: Sequence[Mapping[str, object]], country_rows: Sequence[Mapping[str, object]], temporal_rows: Sequence[Mapping[str, object]], support_release: Mapping[str, object], _validated_postcodes: set[str] | None = None, _prepared=None) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict]:
    """Apply built-surface weights to the geographic support"""
    # One immutable support is shared by application links and temporal chunks
    # Initialize the weight-validation cache for this run
    validated_postcodes = set() if _validated_postcodes is None else _validated_postcodes
    context_key = (id(cross_stage_release), id(routing_rows), id(country_rows), id(support_release))
    if _prepared is None:
        prepared = _prepare_allocation_inputs(cross_stage_release, routing_rows, country_rows, support_release)
    else:
        if 'inputs' not in _prepared:
            _prepared.update(source_objects=(cross_stage_release, routing_rows, country_rows, support_release), key=context_key, inputs=_prepare_allocation_inputs(cross_stage_release, routing_rows, country_rows, support_release))
        if _prepared['key'] != context_key:
            raise ValueError('Allocation context belongs to different immutable inputs')
        prepared = _prepared['inputs']
    grid, support_items, artifact_paths, routes, countries, supports, outcomes, values_by_support, support_failures, published_epochs, available_epochs = prepared
    cells_100m: list[dict[str, object]] = []
    withheld: list[dict[str, object]] = []
    links_by_observation: Counter[str] = Counter()
    skipped_temporal_rows = 0
    seen_temporal_links = set()
    for link in sorted(temporal_rows, key=lambda row: tuple((str(row.get(f, '')) for f in TEMPORAL_FIELDS))):
        key = temporal_link_key(link)
        if key in seen_temporal_links:
            raise ValueError('duplicate temporal child identity')
        seen_temporal_links.add(key)
        observation_id = str(link.get('observation_id', ''))
        if observation_id not in routes:
            skipped_temporal_rows += 1
            continue
        links_by_observation[observation_id] += 1
        route = routes.get(observation_id)
        country = countries.get(observation_id)
        if country is None:
            raise ValueError('temporal link is not covered by routed country evidence')
        code, group = country
        common = {field: str(link.get(field, '')) for field in TEMPORAL_FIELDS}
        common.update({'country_code': code, 'country_group': group})
        if route.get('status') != 'accepted' or route.get('route') not in METHODS:
            withheld.append({**common, 'reason': str(route.get('withholding_reason', '')) or 'route_withheld'})
            continue
        outcome = outcomes[observation_id]
        if outcome['status'] == 'withheld':
            withheld.append({**common, 'reason': outcome['reason'] or 'stage_a_support_withheld'})
            continue
        if link.get('patent_year_status') == 'invalid':
            raise ValueError('invalid temporal row was not quarantined upstream')
        if link.get('patent_year_status') != 'valid':
            withheld.append({**common, 'reason': 'patent_year_missing'})
            continue
        year = int(str(link['patent_year']))
        method = METHODS[str(route['route'])]
        epoch = None
        if method != METHODS['address_passthrough']:
            epoch, epoch_available = select_available_epoch(year, published_epochs, available_epochs)
            if epoch is None:
                withheld.append({**common, 'reason': 'no_ghsl_epoch_no_later_than_patent_year'})
                continue
            if not epoch_available:
                withheld.append({**common, 'reason': 'selected_ghsl_epoch_unavailable'})
                continue
        support = supports.get(observation_id)
        required_sources = {'observed_coordinate'} if method == METHODS['address_passthrough'] else {'street_geometry'} if method == METHODS['street_support_request'] else {'query_ledger', 'accepted_support'}
        if support is not None and support.get('postcode_construction_method') in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}:
            required_sources.add('tl3_boundaries')
        source_artifacts = support.get('source_artifacts') if support else None
        if support is None or support.get('support_method') != method or support.get('closed') is not True or (support.get('grid_identity') != grid) or (not isinstance(source_artifacts, Mapping)) or (set(source_artifacts) != required_sources) or any((role not in artifact_paths for role in source_artifacts.values())):
            raise ValueError('accepted observation lacks matching sealed 100 m support')
        if method == METHODS['street_support_request'] and support.get('geometry_type') not in {'LineString', 'MultiLineString'}:
            raise ValueError('street support lacks actual geometry provenance')
        if method == METHODS['postcode_support_request'] and observation_id not in validated_postcodes:
            from weighting.sample_built_surface import _validate_supports
            _validate_supports({'supports': [support]}, {observation_id: route},
                               {observation_id: outcomes[observation_id]},
                               {str(role): artifact_paths[str(role)] for role in source_artifacts.values()})
            validated_postcodes.add(observation_id)
        accepted_cells = support.get('accepted_cells')
        if not isinstance(accepted_cells, list) or not accepted_cells:
            raise ValueError('accepted 100 m support is empty')
        if support.get('support_cell_count') != len(accepted_cells) or support.get('support_area_m2') != len(accepted_cells) * 10000:
            raise ValueError('sealed support cell count or area is inconsistent')
        accepted_cells = sorted(accepted_cells, key=lambda cell: str(cell.get('cell_id_100m', '')))
        cell_ids = [str(cell.get('cell_id_100m', '')) for cell in accepted_cells]
        if '' in cell_ids or len(set(cell_ids)) != len(cell_ids):
            raise ValueError('accepted 100 m cell IDs must be unique and nonblank')
        if method == METHODS['address_passthrough'] and len(accepted_cells) != 1:
            raise ValueError('observed point passthrough must contain exactly one 100 m cell')
        if method == METHODS['street_support_request'] and (not isinstance(support.get('street_length_m'), (int, float)) or not Decimal(str(support['street_length_m'])).is_finite() or Decimal(str(support['street_length_m'])) <= 0):
            raise ValueError('street support length is invalid')
        neighbourhood = support.get('postcode_construction_method') in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}
        expected_authority = 'approximate_neighbourhood_not_postcode_boundary' if neighbourhood else 'non_authoritative_postcode_support'
        if method == METHODS['postcode_support_request'] and support.get('support_authority_label') != expected_authority:
            raise ValueError('postcode support authority label is invalid')
        raw = []
        support_values = values_by_support.get(str(support['support_id']))
        if not isinstance(support_values, Mapping):
            raise ValueError('GHSL values omit an accepted support')
        epoch_failures = support_failures.get(str(support['support_id']), {})
        if not isinstance(epoch_failures, Mapping):
            raise ValueError('GHSL support-failure entry is invalid')
        failure = epoch_failures.get(str(epoch))
        if failure is not None:
            if method == METHODS['address_passthrough'] or not isinstance(failure, Mapping) or failure.get('reason') not in GHSL_SUPPORT_FAILURE_REASONS or (not isinstance(failure.get('cell_ids'), list)):
                raise ValueError('GHSL support-failure entry is invalid')
            withheld.append({**common, 'reason': str(failure['reason'])})
            continue
        for cell in accepted_cells:
            match = CELL_ID.fullmatch(str(cell.get('cell_id_100m', '')))
            if match is None:
                raise ValueError('100 m cell ID is invalid')
            row, column = (int(match[1]), int(match[2]))
            if not 0 <= row < HEIGHT_100M or not 0 <= column < WIDTH_100M:
                raise ValueError('100 m cell ID is outside the canonical raster')
            expected_parent = f'r{row // 10}c{column // 10}'
            if cell.get('parent_cell_id_1km') != expected_parent:
                raise ValueError('100 m cell violates canonical integer parent mapping')
            if method == METHODS['address_passthrough']:
                raw.append(Decimal(1))
            else:
                values = support_values.get(str(cell['cell_id_100m']))
                if not isinstance(values, Mapping) or str(epoch) not in values:
                    raise ValueError('support cell lacks year-matched built surface')
                if values[str(epoch)] is None:
                    raise ValueError('unrecorded GHSL nodata reached final weighting')
                raw.append(_decimal(values[str(epoch)]))
        weights = _normalized_weights(raw)
        estimate_id = 'estimate-' + uuid4().hex
        for cell, weight in zip(accepted_cells, weights, strict=True):
            if weight == 0:
                continue
            cells_100m.append({**{field: link[field] for field in LINK_FIELDS}, 'patent_year': year, 'selected_ghsl_epoch': epoch, 'estimate_id': estimate_id, 'scenario_id': 'postcode_1km_centre_built_surface_approximation' if neighbourhood else SCENARIO_ID, 'scenario_version': SCENARIO_VERSION, 'evidence_type': 'postcode_neighbourhood' if neighbourhood else str(route['route']).removesuffix('_support_request').replace('address_passthrough', 'observed'), 'country_code': code, 'country_group': group, 'grid_cell_id': str(cell['cell_id_100m']), 'normalized_weight': str(weight), 'support_id': str(support['support_id']), '_parent': str(cell['parent_cell_id_1km'])})
        if sum(map(Fraction, weights), Fraction(0)) != 1:
            raise ValueError('100 m estimate mass does not equal one')
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for row in cells_100m:
        key = (str(row['estimate_id']), str(row['_parent']))
        if key not in grouped:
            grouped[key] = {**{field: row[field] for field in CELL_FIELDS if field in row}, 'grid_cell_id': row['_parent'], 'normalized_weight': '0'}
            grouped[key].pop('support_id', None)
        grouped[key]['normalized_weight'] = str(_exact_decimal_sum([Decimal(str(grouped[key]['normalized_weight'])), Decimal(str(row['normalized_weight']))]))
    cells_1km = list(grouped.values())
    mass_100m = defaultdict(Fraction)
    mass_1km = defaultdict(Fraction)
    for row in cells_100m:
        mass_100m[str(row['estimate_id'])] += Fraction(str(row['normalized_weight']))
        row.pop('_parent')
    for row in cells_1km:
        mass_1km[str(row['estimate_id'])] += Fraction(str(row['normalized_weight']))
    if set(mass_100m) != set(mass_1km) or any((value != Decimal(1) or mass_1km[key] != Decimal(1) for key, value in mass_100m.items())):
        raise ValueError('100 m/1 km estimate mass reconciliation failed')
    input_country_counts = Counter((str(countries[str(row['observation_id'])][1]) for row in temporal_rows if str(row['observation_id']) in routes))
    estimate_countries = {row['estimate_id']: str(row['country_group']) for row in cells_1km}
    estimated_country_counts = Counter(estimate_countries[estimate_id] for estimate_id in mass_1km)
    withheld_country_counts = Counter((str(row['country_group']) for row in withheld))
    country_reconciliation = {group: {'input_links': input_country_counts[group], 'estimated_links': estimated_country_counts[group], 'withheld_links': withheld_country_counts[group], 'reconciled': input_country_counts[group] == estimated_country_counts[group] + withheld_country_counts[group]} for group in sorted({str(row['country_group']) for row in country_rows})}
    if not all((value['reconciled'] for value in country_reconciliation.values())):
        raise ValueError('country-group temporal-link reconciliation failed')
    audit = {'schema_version': 'imputation_final_grid_audit_v1', 'run_id': cross_stage_release['run_id'], 'grid_identity': dict(grid), 'epoch_policy': 'select latest published GHSL epoch no later than patent_year; withhold unless that exact epoch is available', 'scenario_id': SCENARIO_ID, 'scenario_version': SCENARIO_VERSION, 'temporal_links': len(temporal_rows), 'scoped_temporal_links': sum(links_by_observation.values()), 'skipped_out_of_scope_temporal_links': skipped_temporal_rows, 'observations_with_links': len(links_by_observation), 'estimates': len(mass_1km), 'support_rows_100m': len(cells_100m), 'final_rows_1km': len(cells_1km), 'withheld_rows': len(withheld), 'all_100m_estimate_mass_one': all((value == Decimal(1) for value in mass_100m.values())), 'all_1km_estimate_mass_one': all((value == Decimal(1) for value in mass_1km.values())), 'country_group_reconciliation': country_reconciliation, 'network_requests': 0}
    audit['postcode_construction_counts'] = dict(Counter(str(s.get('postcode_construction_method', 'unspecified_construction')) for s in support_items if s.get('support_method') == METHODS['postcode_support_request']))
    audit['scenario_ids'] = sorted({str(row['scenario_id']) for row in cells_1km})
    return (cells_100m, cells_1km, withheld, audit)

# Reuse verified spatial vectors while retaining every source link and output row
def _build_reusing_vectors(*, cross_stage_release, routing_rows, country_rows, temporal_rows, support_release, cache, validated_postcodes, prepared=None):
    import copy
    fine, coarse, held = [], [], []
    audit = None
    scoped = set()
    scenarios = set()
    numeric = ('temporal_links', 'scoped_temporal_links', 'skipped_out_of_scope_temporal_links', 'estimates', 'support_rows_100m', 'final_rows_1km', 'withheld_rows')
    seen = set()
    for link in sorted(temporal_rows, key=lambda row: tuple(str(row.get(f, '')) for f in TEMPORAL_FIELDS)):
        identity_key = temporal_link_key(link)
        if identity_key in seen:
            raise ValueError('duplicate temporal child identity')
        seen.add(identity_key)
        year = str(link['patent_year'])
        epoch = select_available_epoch(int(year), support_release['ghsl_epoch_scope']['published_epochs'], support_release['ghsl_epoch_scope']['available_epochs']) if link['patent_year_status'] == 'valid' and year.isdecimal() else ('unparsed', year)
        key = (link['observation_id'], epoch, link['patent_year_status'])
        reused = key in cache
        if reused:
            f, c, h, part = cache.pop(key)
        else:
            f, c, h, part = build(cross_stage_release=cross_stage_release, routing_rows=routing_rows, country_rows=country_rows, temporal_rows=[link], support_release=support_release, _validated_postcodes=validated_postcodes, _prepared=prepared)
        cache[key] = (f, c, h, part)
        while len(cache) > 128:
            cache.popitem(last=False)
        if reused:
            identity = {field: link[field] for field in LINK_FIELDS}
            if c:identity['patent_year'] = int(link['patent_year'])
            estimate = 'estimate-' + uuid4().hex if c else None
            fine.extend({**row, **identity, 'estimate_id': estimate} for row in f)
            coarse.extend({**row, **identity, 'estimate_id': estimate} for row in c)
            held.extend({**row, **link} for row in h)
        else:
            fine.extend(f); coarse.extend(c); held.extend(h)
        if audit is None:
            audit = copy.deepcopy(part)
        else:
            for field in numeric:
                audit[field] += part[field]
            for group, entry in audit['country_group_reconciliation'].items():
                for field in ('input_links', 'estimated_links', 'withheld_links'):
                    entry[field] += part['country_group_reconciliation'][group][field]
                entry['reconciled'] = entry['input_links'] == entry['estimated_links'] + entry['withheld_links']
        if part['scoped_temporal_links']:
            scoped.add(link['observation_id'])
        scenarios.update(part.get('scenario_ids', []))
    if audit is None:
        return build(cross_stage_release=cross_stage_release, routing_rows=routing_rows, country_rows=country_rows, temporal_rows=[], support_release=support_release, _validated_postcodes=validated_postcodes, _prepared=prepared)
    audit['observations_with_links'] = len(scoped)
    audit['scenario_ids'] = sorted(scenarios)
    return fine, coarse, held, audit

# Reweight saved batches through the ordinary allocator, then join their verified outputs
def _run_saved_batches(cross_path, support_path, output_dir, cross, support):
    from geographic_uncertainty.batches import _merge_csv, merge_allocation_audits
    if support.get('sealed') is not True or support.get('run_id') != cross.get('run_id'):
        raise ValueError('sealed cross-stage/support release identity is invalid')
    supports = {item['observation_id']: item for item in support['supports']}
    if len(supports) != len(support['supports']):
        raise ValueError('Duplicate merged support observation')
    crosses = cross.get('batch_manifests', [])
    releases = support.get('batch_support_releases', [])
    if not crosses or len(crosses) != len(releases):
        raise ValueError('Batch handoff and support releases must pair exactly')
    if output_dir.exists():
        raise FileExistsError(f'refusing to overwrite {output_dir}')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{output_dir.name}.', dir=output_dir.parent) as temporary:
        work = Path(temporary)
        parts = []
        seen = set()
        for index, (child_cross, child_support) in enumerate(zip(crosses, releases)):
            child_cross, child_support = Path(child_cross), Path(child_support)
            child = json.loads(child_cross.read_text())
            release = json.loads(child_support.read_text())
            if child['scope_countries'] != cross['scope_countries'] or release['grid_identity'] != support['grid_identity']:
                raise ValueError('Batch scope or grid differs from the merged release')
            for item in release['supports']:
                if supports.pop(item['observation_id'], None) != item:
                    raise ValueError('Merged support differs from its saved batch')
            ids = {row['observation_id'] for row in _read_exact(_sealed_path(child['routing'], 'routing'), ROUTING_FIELDS)}
            if seen & ids:
                raise ValueError('Overlapping saved batches')
            seen.update(ids)
            target = work/f'batch-{index:08d}'
            run(cross_stage_release_path=child_cross, support_release_path=child_support, output_dir=target)
            parts.append(target)
        expected = {row['observation_id'] for row in _read_exact(_sealed_path(cross['routing'], 'routing'), ROUTING_FIELDS)}
        if seen != expected or supports:
            raise ValueError('Saved batches do not cover the merged cohort')
        merged = work/'merged'
        merged.mkdir()
        first = json.loads((parts[0]/'final_grid_manifest.json').read_text())
        names = sorted({name for part in parts for name in json.loads((part/'final_grid_manifest.json').read_text())['artifacts'] if name.endswith('.csv')})
        counts = {}
        artifacts = {}
        for name in names:
            sources = [part/name for part in parts if (part/name).is_file()]
            counts[name] = _merge_csv(sources, merged/name)
            template = next(json.loads((part/'final_grid_manifest.json').read_text())['artifacts'][name] for part in parts if (part/name).is_file())
            artifacts[name] = dict(template, path=name, path_basis='manifest_relative', rows=counts[name])
        counts['temporal.csv'] = len(_read_exact(_sealed_path(cross['temporal_child'], 'temporal_child'), TEMPORAL_FIELDS))
        audit_paths = [part/'audit.json' for part in parts]
        audit = merge_allocation_audits(audit_paths, cross['run_id'], counts)
        # Keep the reweighting audits after temporary cell tables are removed
        audit_dir = merged/'batch-audits'
        audit_dir.mkdir()
        for index, path in enumerate(audit_paths):
            (audit_dir/f'{index:08d}.json').write_bytes(path.read_bytes())
        audit['batch_audits'] = [str((output_dir/'batch-audits'/f'{index:08d}.json').resolve()) for index in range(len(parts))]
        atomic_write(merged/'audit.json', _json_bytes(audit))
        artifacts['audit.json'] = dict(first['artifacts']['audit.json'], path='audit.json', path_basis='manifest_relative')
        manifest = dict(first, cross_stage_release=dict(path=str(cross_path.resolve()), path_basis='absolute', schema_version=cross['schema_version'], run_id=cross['run_id']),
                        support_release=dict(path=str(support_path.resolve()), path_basis='absolute', schema_version=support['schema_version'], run_id=support['run_id'], sealed=support['sealed']), artifacts=artifacts)
        manifest['resource_estimate'] = dict(output_bytes=sum(p.stat().st_size for p in merged.iterdir()), network_requests=0)
        atomic_write(merged/'final_grid_manifest.json', _json_bytes(manifest))
        os.replace(merged, output_dir)
        return manifest


# Bound expanded cell rows in memory and stream the unchanged allocation outputs
def run(*, cross_stage_release_path: Path, support_release_path: Path, output_dir: Path) -> dict:
    cross = json.loads(cross_stage_release_path.read_text())
    support = json.loads(support_release_path.read_text())
    if support.get('batch_support_releases'):
        return _run_saved_batches(cross_stage_release_path, support_release_path, output_dir, cross, support)
    routing_path = _sealed_path(cross['routing'], 'routing')
    country_path = _sealed_path(cross['country_groups'], 'country_groups')
    temporal_path = _sealed_path(cross['temporal_child'], 'temporal_child')
    routing = _read_exact(routing_path, ROUTING_FIELDS)
    countries = _read_exact(country_path, COUNTRY_FIELDS)
    temporal = _read_exact(temporal_path, TEMPORAL_FIELDS)
    if output_dir.exists():
        raise FileExistsError(f'refusing to overwrite {output_dir}')
    # Bound working memory by the expanded cell count
    # build() retains all qualification, epoch selection and exact mass checks
    temporal = sorted(temporal, key=lambda row: tuple(str(row.get(f, '')) for f in TEMPORAL_FIELDS))
    keys = [temporal_link_key(row) for row in temporal]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate temporal child identity')
    cells_per_observation = {str(item['observation_id']): len(item.get('accepted_cells', [])) for item in support['supports']}
    chunks = []
    start = 0
    expanded = 0
    for i, row in enumerate(temporal):
        cost = max(1, cells_per_observation.get(str(row.get('observation_id', '')), 1))
        if i > start and expanded + cost > MAX_EXPANDED_CELL_ROWS:
            chunks.append((start, i))
            start, expanded = i, 0
        expanded += cost
    chunks.append((start, len(temporal)))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f'.{output_dir.name}.', dir=output_dir.parent))
    try:
        fields_1km = tuple(f for f in CELL_FIELDS if f != 'support_id')
        schemas = {'support_weights_100m.csv': CELL_FIELDS, 'estimated_grid_1km.csv': fields_1km, 'withheld.csv': WITHHELD_FIELDS}
        variants = any(item.get('output_variant') in {'postcode_point_neighbourhood_sensitivity', 'postcode_1km_approximation'} for item in support['supports'])
        if variants:
            schemas['postcode_point_neighbourhood_1km.csv'] = fields_1km
            schemas['polygon_and_other_support_1km.csv'] = fields_1km
        from contextlib import ExitStack
        counts = Counter()
        audit = None
        numeric = ('temporal_links', 'scoped_temporal_links', 'skipped_out_of_scope_temporal_links', 'estimates', 'support_rows_100m', 'final_rows_1km', 'withheld_rows')
        scenarios = set()
        validated_postcodes = set()
        vector_cache, prepared = OrderedDict(), {}
        with ExitStack() as stack:
            writers = {}
            for name, fields in schemas.items():
                handle = stack.enter_context((work / name).open('w', newline=''))
                writers[name] = csv.DictWriter(handle, fields, lineterminator='\n', extrasaction='raise')
                writers[name].writeheader()
            for lo, hi in chunks:
                fine, coarse, held, part = _build_reusing_vectors(cross_stage_release=cross, routing_rows=routing, country_rows=countries, temporal_rows=temporal[lo:hi], support_release=support, cache=vector_cache, validated_postcodes=validated_postcodes, prepared=prepared)
                groups = {'support_weights_100m.csv': fine, 'estimated_grid_1km.csv': coarse, 'withheld.csv': held}
                if variants:
                    groups['postcode_point_neighbourhood_1km.csv'] = (row for row in coarse if row['evidence_type'] == 'postcode_neighbourhood')
                    groups['polygon_and_other_support_1km.csv'] = (row for row in coarse if row['evidence_type'] != 'postcode_neighbourhood')
                for name, rows in groups.items():
                    for row in rows:
                        writers[name].writerow(row)
                        counts[name] += 1
                if audit is None:
                    audit = part
                else:
                    for key in numeric:
                        audit[key] += part[key]
                    for group, entry in audit['country_group_reconciliation'].items():
                        for key in ('input_links', 'estimated_links', 'withheld_links'):
                            entry[key] += part['country_group_reconciliation'][group][key]
                        entry['reconciled'] = entry['input_links'] == entry['estimated_links'] + entry['withheld_links']
                scenarios.update(part.get('scenario_ids', []))
                del fine, coarse, held, groups, part
        route_ids = {str(row['observation_id']) for row in routing}
        audit['observations_with_links'] = len({str(row['observation_id']) for row in temporal if str(row['observation_id']) in route_ids})
        audit['scenario_ids'] = sorted(scenarios)
        atomic_write(work / 'audit.json', _json_bytes(audit))
        files = {name: None for name in schemas}
        files['audit.json'] = None
        artifact_identity = {
            name: {'role': 'support_weights_100m' if name == 'support_weights_100m.csv' else 'withheld' if name == 'withheld.csv' else name.removesuffix('.csv'),
                   'schema_version': 'imputation_support_weights_100m_v1' if name == 'support_weights_100m.csv' else 'imputation_final_grid_withheld_v1' if name == 'withheld.csv' else 'imputation_estimated_grid_1km_v1',
                   'fields': list(fields), 'rows': counts[name]}
            for name, fields in schemas.items()}
        artifact_identity['audit.json'] = {'role': 'audit', 'schema_version': audit['schema_version'], 'fields': None, 'rows': 1}
        artifacts = {name: {**artifact_identity[name], 'path': name, 'path_basis': 'manifest_relative'} for name, data in sorted(files.items())}
        manifest = {'schema_version': SCHEMA_VERSION, 'manifest_contract_version': 'imputation-final-grid-manifest-v2', 'scenario_id': SCENARIO_ID, 'scenario_version': SCENARIO_VERSION, 'cross_stage_release': {'path': str(cross_stage_release_path.resolve()), 'path_basis': 'absolute', 'schema_version': cross['schema_version'], 'run_id': cross['run_id']}, 'support_release': {'path': str(support_release_path.resolve()), 'path_basis': 'absolute', 'schema_version': support['schema_version'], 'run_id': support['run_id'], 'sealed': support['sealed']}, 'grid_identity': audit['grid_identity'], 'grid_contract': {**support['grid_contract'], 'path_basis': 'absolute'}, 'epoch_policy': audit['epoch_policy'], 'ghsl_built_assets': support['ghsl_built_assets'], 'artifacts': artifacts, 'resource_estimate': {'output_bytes': sum((work / name).stat().st_size for name in files), 'network_requests': 0}}
        atomic_write(work / 'final_grid_manifest.json', _json_bytes(manifest))
        os.replace(work, output_dir)
        return manifest
    except Exception:
        for child in work.iterdir():
            child.unlink()
        work.rmdir()
        raise

def main(argv: Sequence[str] | None=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--cross-stage-release', required=True, type=Path)
    parser.add_argument('--support-release', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    run(cross_stage_release_path=args.cross_stage_release, support_release_path=args.support_release, output_dir=args.output_dir)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
