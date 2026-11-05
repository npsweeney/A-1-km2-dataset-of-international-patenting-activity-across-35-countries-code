"""Sample GHSL built surface within geographic support"""
from __future__ import annotations
import argparse
import csv
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from weighting.allocate_to_grid import HEIGHT_100M, METHODS, OFFICIAL_GHSL_EPOCHS, WIDTH_100M
from weighting.register_ghsl import SCHEMA_VERSION as REGISTRY_SCHEMA_VERSION
from weighting.register_ghsl import _read_contract, _validate_raster, inspect_raster, raster_coverage, official_tile_stem, official_tile_url
from geographic_uncertainty.routing import ROUTING_FIELDS, atomic_write
INPUT_SCHEMA_VERSION = 'imputation_100m_support_inputs_v1'
RELEASE_SCHEMA_VERSION = 'imputation_100m_support_release_v1'
VALUES_SCHEMA_VERSION = 'ghsl_100m_values_v1'
CELL_ID = re.compile('^r(\\d+)c(\\d+)$')
SELECTION_RULE = 'latest_published_epoch_no_later_than_patent_year_then_require_available'
OUTCOME_FIELDS = ('observation_id', 'validation_id', 'input_route', 'status', 'support_method', 'support_id', 'reason', 'pending_request_ids', 'source_artifact_role')
ValueReader = Callable[[Path, Sequence[tuple[int, int]]], Mapping[tuple[int, int], int | None]]

def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()

def _verified_artifact(entry: Mapping[str, object], role: str) -> Path:
    path = Path(str(entry.get('path', '')))
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f'sealed artifact mismatch: {role}')
    return path

def _read_routing(cross_stage: Mapping[str, object]) -> dict[str, Mapping[str, str]]:
    entry = cross_stage.get('routing')
    if not isinstance(entry, Mapping):
        raise ValueError('cross-stage routing artifact is missing')
    path = Path(str(entry.get('artifact_path', entry.get('path', ''))))
    if not path.is_absolute() or not path.is_file():
        raise ValueError('cross-stage routing artifact mismatch')
    with path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != ROUTING_FIELDS:
            raise ValueError('routing artifact schema/order is not exact')
        rows = list(reader)
    result = {row['observation_id']: row for row in rows}
    if '' in result or len(result) != len(rows):
        raise ValueError('routing observation identities are invalid')
    return result

def _read_stage_a_outcomes(inputs: Mapping[str, object], run_id: object) -> tuple[dict[str, Mapping[str, str]], list[dict[str, object]]]:
    manifest_entry = inputs.get('stage_a_outcomes_manifest')
    outcomes_entry = inputs.get('stage_a_outcomes')
    if not isinstance(manifest_entry, Mapping) or not isinstance(outcomes_entry, Mapping):
        raise ValueError('Stage-A outcomes manifest and artifact bindings are required')
    manifest_path = _verified_artifact(manifest_entry, 'stage_a_outcomes_manifest')
    outcomes_path = _verified_artifact(outcomes_entry, 'stage_a_outcomes')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema_version') != 'imputation_stage_a_outcomes_v1' or manifest.get('run_id') != run_id:
        raise ValueError('Stage-A outcomes manifest identity is invalid')
    artifacts = manifest.get('artifacts')
    sealed = artifacts.get('support_outcomes.csv') if isinstance(artifacts, Mapping) else None
    if not isinstance(sealed, Mapping) or sealed.get('bytes') != outcomes_entry.get('bytes'):
        raise ValueError('Stage-A manifest does not bind the supplied outcomes artifact')
    with outcomes_path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != OUTCOME_FIELDS:
            raise ValueError('Stage-A outcomes schema/order is not exact')
        rows = list(reader)
    by_observation = {row['observation_id']: row for row in rows}
    validation_ids = [row['validation_id'] for row in rows]
    if '' in by_observation or len(by_observation) != len(rows) or '' in validation_ids or (len(set(validation_ids)) != len(validation_ids)):
        raise ValueError('Stage-A outcome observation identities are invalid')
    if any((row['status'] == 'pending' for row in rows)):
        raise ValueError('pending Stage-A outcomes prevent support-release sealing')
    if any((row['status'] not in {'support_ready', 'withheld'} for row in rows)):
        raise ValueError('Stage-A outcome status is invalid')
    for row in rows:
        if json.loads(row.get('pending_request_ids', '[]')):
            raise ValueError('spatial support has pending requests')
        if row['status'] == 'support_ready' and (not row['support_method'] or not row['support_id'] or (not row['source_artifact_role']) or row['reason']):
            raise ValueError('Stage-A ready outcome fields are invalid')
        if row['status'] == 'withheld' and (not row['reason'] or row['support_method'] or row['support_id'] or row['source_artifact_role']):
            raise ValueError('Stage-A withheld outcome fields are invalid')
    ledger = [{**dict(manifest_entry), 'role': 'stage_a_outcomes_manifest'}, {**dict(outcomes_entry), 'role': 'stage_a_outcomes'}]
    return (by_observation, ledger)

def _read_epoch_values(raster_path: Path, cells: Sequence[tuple[int, int]]) -> Mapping[tuple[int, int], int | None]:
    """Read each containing raster block once; return None only for canonical nodata"""
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError as exc:
        raise RuntimeError('install patent-code[raster] to extract GHSL values') from exc
    result: dict[tuple[int, int], int | None] = {}
    with rasterio.open(raster_path) as dataset:
        coverage = raster_coverage(inspect_raster(raster_path))
        row_origin, column_origin = (coverage['row_offset'], coverage['column_offset'])
        if any((not (row_origin <= row < row_origin + coverage['height'] and column_origin <= column < column_origin + coverage['width']) for row, column in cells)):
            raise ValueError('requested GHSL cell is outside registered raster coverage')
        block_height, block_width = dataset.block_shapes[0]
        blocks: defaultdict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for row, column in cells:
            blocks[(row - row_origin) // block_height, (column - column_origin) // block_width].append((row, column))
        for (block_row, block_column), members in sorted(blocks.items()):
            row_off, column_off = (block_row * block_height, block_column * block_width)
            height = min(block_height, dataset.height - row_off)
            width = min(block_width, dataset.width - column_off)
            band = dataset.read(1, window=Window(column_off, row_off, width, height))
            for row, column in members:
                value = int(band[row - row_origin - row_off, column - column_origin - column_off])
                result[row, column] = None if value == 65535 else value
    return result

def _validate_registry(registry_path: Path, grid_contract_path: Path) -> tuple[dict[str, object], dict[str, object], dict[int, Mapping[str, object]]]:
    contract = _read_contract(grid_contract_path.resolve(strict=True))
    registry = json.loads(registry_path.read_text())
    if registry.get('schema_version') != REGISTRY_SCHEMA_VERSION or registry.get('product') != 'GHS-BUILT-S' or registry.get('release') != 'R2023A' or (registry.get('measure') != 'built_surface_m2') or (registry.get('published_epochs') != list(OFFICIAL_GHSL_EPOCHS)):
        raise ValueError('GHSL registry identity is invalid')
    available = registry.get('available_epochs')
    assets = registry.get('assets')
    if not isinstance(available, list) or not available or any((not isinstance(epoch, int) for epoch in available)) or (sorted(set(available)) != available) or (not set(available) <= set(OFFICIAL_GHSL_EPOCHS)) or (not isinstance(assets, list)):
        raise ValueError('GHSL registry epoch scope is invalid')
    by_epoch = {int(item['epoch']): item for item in assets if isinstance(item, Mapping) and isinstance(item.get('epoch'), int)}
    if set(by_epoch) != set(available) or len(by_epoch) != len(assets):
        raise ValueError('GHSL registry assets do not exactly cover available epochs')
    expected_grid = {'ghsl_registry_version': contract['ghslRegistryVersion'], 'crs': contract['crs'], 'origin_x': contract['originX'], 'origin_y': contract['originY'], 'extent': contract['extent'], 'cell_indexing': contract['cellIndexing'], 'resolution_100m': contract['resolution100m'], 'resolution_1km': contract['resolution1km'], 'parent_mapping': contract['parentMapping']}
    if registry.get('grid_identity') != expected_grid:
        raise ValueError('GHSL registry grid is not the signed canonical grid')
    for epoch, asset in by_epoch.items():
        if not asset.get('artifact_role') or asset.get('raster_metadata') is None:
            raise ValueError('GHSL registry asset binding is invalid')
        path = _verified_artifact(asset, f'GHSL raster {epoch}')
        metadata = inspect_raster(path)
        if asset.get('storage_layout', 'global') == 'official_tile':
            tile_name = official_tile_stem(epoch, asset.get('tile_row'), asset.get('tile_column'))
            if asset.get('coverage') != raster_coverage(metadata) or path.name != tile_name + '.tif' or asset.get('url') != official_tile_url(epoch, asset['tile_row'], asset['tile_column']):
                raise ValueError('GHSL tile coverage or official identity drifted')
            archive = Path(str(asset.get('archive_path', '')))
            if not archive.is_absolute() or not archive.is_file() or archive.name != tile_name + '.zip':
                raise ValueError('GHSL tile source archive identity drifted')
        elif asset.get('storage_layout', 'global') == 'global':
            _validate_raster(metadata)
        else:
            raise ValueError('GHSL storage layout is invalid')
        if metadata != asset['raster_metadata']:
            raise ValueError('GHSL raster metadata drifted from the sealed registry')
    return (registry, expected_grid, by_epoch)

def _validate_supports(inputs: Mapping[str, object], routes: Mapping[str, Mapping[str, str]], outcomes: Mapping[str, Mapping[str, str]], source_paths: Mapping[str, Path]) -> tuple[list[dict[str, object]], dict[str, list[tuple[int, int]]]]:
    items = inputs.get('supports')
    if not isinstance(items, list):
        raise ValueError('support inputs must contain a support list')
    accepted_routes = {oid: row['route'] for oid, row in routes.items() if row.get('status') == 'accepted' and row.get('route') in METHODS}
    if set(outcomes) != set(routes):
        raise ValueError('Stage-A outcomes do not exactly cover routed observations')
    expected: dict[str, str] = {}
    for oid, outcome in outcomes.items():
        route = routes[oid]
        if outcome.get('input_route') != route.get('route'):
            raise ValueError('Stage-A outcome route does not match immutable routing')
        if outcome.get('status') == 'support_ready':
            if oid not in accepted_routes or outcome.get('support_method') != METHODS[accepted_routes[oid]]:
                raise ValueError('Stage-A ready outcome is inconsistent with routing')
            expected[oid] = str(outcome['support_method'])
    normalized: list[dict[str, object]] = []
    coordinates: dict[str, list[tuple[int, int]]] = {}
    observation_ids: set[str] = set()
    support_ids: set[str] = set()
    used_roles: set[str] = set()
    for supplied in items:
        if not isinstance(supplied, Mapping):
            raise ValueError('support entry is invalid')
        support = dict(supplied)
        oid, sid = (str(support.get('observation_id', '')), str(support.get('support_id', '')))
        method = str(support.get('support_method', ''))
        if not oid or not sid or oid in observation_ids or (sid in support_ids) or (expected.get(oid) != method) or (support.get('closed') is not True) or (outcomes[oid].get('support_id') != sid):
            raise ValueError('support identity, method, or genuine closed status is invalid')
        observation_ids.add(oid)
        support_ids.add(sid)
        required_sources = {'observed_coordinate'} if method == METHODS['address_passthrough'] else {'street_geometry'} if method == METHODS['street_support_request'] else {'query_ledger', 'accepted_support'}
        if support.get('postcode_construction_method') in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}:
            required_sources.add('tl3_boundaries')
        sources = support.get('source_artifacts')
        if not isinstance(sources, Mapping) or set(sources) != required_sources:
            raise ValueError('support source-artifact roles are not exact')
        roles = {str(role) for role in sources.values()}
        if '' in roles or not roles <= set(source_paths):
            raise ValueError('support source artifact is absent from the sealed ledger')
        used_roles.update(roles)
        primary_role = str(sources['observed_coordinate']) if method == METHODS['address_passthrough'] else str(sources['street_geometry']) if method == METHODS['street_support_request'] else str(sources['accepted_support'])
        if outcomes[oid].get('source_artifact_role') != primary_role:
            raise ValueError('Stage-A ready artifact role does not match support provenance')
        if method == METHODS['street_support_request'] and support.get('geometry_type') not in {'LineString', 'MultiLineString'}:
            raise ValueError('street support lacks genuine geometry provenance')
        if method == METHODS['postcode_support_request']:
            construction = support.get('postcode_construction_method')
            if construction not in {'qualified_osm_postcode_polygon', 'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}:
                raise ValueError('postcode support lacks genuine closed-boundary provenance')
            from geographic_uncertainty.retrieve_and_rasterise import _postcode_polygon_cells, _postcode_point, _coarse_postcode_cells, _terminal_evidence, _response_json
            ledger = json.loads(source_paths[str(sources['query_ledger'])].read_text())
            searches = [row for row in _terminal_evidence(ledger) if row['request_kind'] == 'search' and not row.get('error_class')]
            boundary_request = support.get('source', {}).get('boundary_request', {})
            if len(searches) != 1 or boundary_request.get('observation_id') != oid:
                raise ValueError('postcode polygon lacks its actual search evidence')
            payload = _response_json(searches[0])
            polygon = _postcode_polygon_cells(payload, boundary_request)
            if construction in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}:
                coarse = _coarse_postcode_cells(payload, boundary_request, ledger)
                cells = coarse['cells'] if coarse else None
                if coarse and not coarse.get('area_limit_exceeded') and support.get('postcode_extent_status') != coarse.get('extent_status'):
                    raise ValueError('postcode extent status differs from saved neighbour evidence')
                point = _postcode_point(payload, boundary_request)
                if (polygon is not None or cells is None or
                    point is None or support.get('point_provenance') != point or
                    Path(str(point['boundaries']['path'])) != source_paths[str(sources['tl3_boundaries'])] or
                    support.get('output_variant') != 'postcode_1km_approximation' or
                    support.get('support_authority_label') != 'approximate_neighbourhood_not_postcode_boundary' or
                    (coarse is None or coarse.get('area_limit_exceeded') or coarse['method'] != construction) or
                    support.get('seed_provenance', {}).get('seed_longitude') != boundary_request.get('seed_longitude') or
                    support.get('seed_provenance', {}).get('seed_latitude') != boundary_request.get('seed_latitude')):
                    raise ValueError('postcode neighbourhood evidence does not qualify')
            else:
                if polygon is None or polygon['cell_cap_reached']:
                    raise ValueError('postcode polygon evidence does not qualify')
                cells = polygon['cells']
            expected_cells = [{'cell_id_100m': f'r{row}c{column}', 'parent_cell_id_1km': f'r{row // 10}c{column // 10}'} for row, column in cells]
            if support.get('accepted_cells') != expected_cells:
                raise ValueError('postcode polygon raster differs from its actual geometry')
            provenance = support.get('boundary_provenance', {})
            if construction == 'qualified_osm_postcode_polygon' and (provenance.get('osm_type') != polygon['candidate']['osm_type'] or provenance.get('osm_id') != str(polygon['candidate']['osm_id'])):
                raise ValueError('postcode polygon object identity differs')
        cells = support.get('accepted_cells')
        if not isinstance(cells, list) or not cells:
            raise ValueError('closed support has no accepted cells')
        if support.get('support_cell_count') != len(cells) or support.get('support_area_m2') != len(cells) * 10000:
            raise ValueError('Stage-A support cell count or area is inconsistent')
        if method == METHODS['address_passthrough'] and len(cells) != 1:
            raise ValueError('observed coordinate support must contain exactly one cell')
        if method == METHODS['street_support_request'] and (not isinstance(support.get('street_length_m'), (int, float)) or not math.isfinite(float(support['street_length_m'])) or float(support['street_length_m']) <= 0):
            raise ValueError('street support lacks a genuine positive length')
        expected_authority = 'approximate_neighbourhood_not_postcode_boundary' if support.get('postcode_construction_method') in {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'} else 'non_authoritative_postcode_support'
        if method == METHODS['postcode_support_request'] and support.get('support_authority_label') != expected_authority:
            raise ValueError('postcode support authority label is invalid')
        seen: set[str] = set()
        parsed: list[tuple[int, int]] = []
        for cell in cells:
            if not isinstance(cell, Mapping):
                raise ValueError('accepted support cell is invalid')
            cell_id = str(cell.get('cell_id_100m', ''))
            match = CELL_ID.fullmatch(cell_id)
            if match is None or cell_id in seen:
                raise ValueError('accepted 100 m cell IDs must be unique canonical IDs')
            seen.add(cell_id)
            row, column = (int(match[1]), int(match[2]))
            if row >= HEIGHT_100M or column >= WIDTH_100M:
                raise ValueError('accepted 100 m cell is outside the canonical raster')
            if cell.get('parent_cell_id_1km') != f'r{row // 10}c{column // 10}':
                raise ValueError('accepted 100 m cell violates the exact parent mapping')
            parsed.append((row, column))
        support.pop('grid_identity', None)
        normalized.append(support)
        coordinates[sid] = parsed
    if observation_ids != set(expected):
        raise ValueError('closed supports do not exactly cover accepted routed observations')
    if used_roles != set(source_paths):
        raise ValueError('source artifact ledger contains unreferenced or missing roles')
    return (sorted(normalized, key=lambda item: str(item['observation_id'])), coordinates)

# Sample the accepted 100 m cells from the required GHSL epochs and seal the raster evidence
def build_release(*, cross_stage_release_path: Path, ghsl_registry_path: Path, grid_contract_path: Path, support_inputs_path: Path, output_dir: Path, value_reader: ValueReader=_read_epoch_values) -> dict[str, object]:
    """Sample and combine built surface with geographic support"""
    if output_dir.exists():
        raise FileExistsError(f'refusing to overwrite {output_dir}')
    cross_path = cross_stage_release_path.resolve(strict=True)
    registry_path = ghsl_registry_path.resolve(strict=True)
    inputs_path = support_inputs_path.resolve(strict=True)
    cross = json.loads(cross_path.read_text())
    inputs = json.loads(inputs_path.read_text())
    if cross.get('schema_version') not in {'imputation_v3_cross_stage_release_v1', 'patent_geographic_handoff_v1'} or inputs.get('schema_version') != INPUT_SCHEMA_VERSION or (inputs.get('run_id') != cross.get('run_id')):
        raise ValueError('cross-stage/support-input release binding is invalid')
    routes = _read_routing(cross)
    outcomes, outcome_ledger = _read_stage_a_outcomes(inputs, cross.get('run_id'))
    registry, registry_grid, registry_assets = _validate_registry(registry_path, grid_contract_path)
    artifact_entries = inputs.get('artifacts')
    if not isinstance(artifact_entries, list):
        raise ValueError('support source artifact ledger is missing')
    source_ledger: dict[str, dict[str, object]] = {}
    source_paths: dict[str, Path] = {}
    for entry in artifact_entries:
        if not isinstance(entry, Mapping):
            raise ValueError('support source artifact entry is invalid')
        role = str(entry.get('role', ''))
        if not role or role in source_ledger:
            raise ValueError('support source artifact roles must be unique and nonblank')
        path = _verified_artifact(entry, role)
        source_paths[role] = path
        source_ledger[role] = dict(entry)
    supports, coordinates = _validate_supports(inputs, routes, outcomes, source_paths)
    grid = {**registry_grid}
    for support in supports:
        support['grid_identity'] = grid
    unique_cells = sorted({cell for cells in coordinates.values() for cell in cells})
    values_by_support: dict[str, dict[str, dict[str, str | None]]] = {str(support['support_id']): {str(cell['cell_id_100m']): {} for cell in support['accepted_cells']} for support in supports}
    failures: dict[str, dict[str, dict[str, object]]] = {}
    ghsl_assets: dict[str, dict[str, object]] = {}
    raster_ledger: list[dict[str, object]] = []
    methods_by_sid = {str(item['support_id']): str(item['support_method']) for item in supports}
    for epoch in registry['available_epochs']:
        asset = registry_assets[int(epoch)]
        path = Path(str(asset['path']))
        extracted = value_reader(path, unique_cells)
        if set(extracted) != set(unique_cells):
            raise ValueError('GHSL value reader did not return every requested canonical cell')
        epoch_key = str(epoch)
        role = str(asset['artifact_role'])
        ghsl_assets[epoch_key] = {'artifact_role': role, 'grid_identity': grid}
        raster_ledger.append({'role': role, 'path': str(path)})
        for support in supports:
            sid = str(support['support_id'])
            nodata_cells: list[str] = []
            total = Decimal(0)
            for cell, position in zip(support['accepted_cells'], coordinates[sid], strict=True):
                cell_id = str(cell['cell_id_100m'])
                value = extracted[position]
                values_by_support[sid][cell_id][epoch_key] = None if value is None else str(value)
                if value is None:
                    nodata_cells.append(cell_id)
                else:
                    total += Decimal(value)
            if methods_by_sid[sid] == METHODS['address_passthrough']:
                continue
            if nodata_cells:
                failures.setdefault(sid, {})[epoch_key] = {'reason': 'ghsl_nodata_in_support', 'cell_ids': sorted(nodata_cells)}
            elif total == 0:
                failures.setdefault(sid, {})[epoch_key] = {'reason': 'zero_built_surface_mass', 'cell_ids': []}
    values = {'schema_version': VALUES_SCHEMA_VERSION, 'ghsl_source': {'product': registry['product'], 'release': registry['release'], 'measure': registry['measure']}, 'grid_identity': grid, 'values_by_support': values_by_support, 'support_failures_by_epoch': failures}
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f'.{output_dir.name}.', dir=output_dir.parent))
    try:
        values_bytes = _json_bytes(values)
        atomic_write(work / 'ghsl_values.json', values_bytes)
        values_final = output_dir / 'ghsl_values.json'
        ledger = [*source_ledger.values(), *outcome_ledger, {'role': 'ghsl_registry', 'path': str(registry_path)}, *raster_ledger, {'role': 'ghsl_values', 'path': str(values_final.resolve())}]
        roles = [str(entry['role']) for entry in ledger]
        if len(set(roles)) != len(roles):
            raise ValueError('combined support artifact roles collide')
        release = {'schema_version': RELEASE_SCHEMA_VERSION, 'sealed': True, 'run_id': cross['run_id'], 'support_inputs': {'path': str(inputs_path)}, 'grid_contract': {'path': str(grid_contract_path.resolve(strict=True)), 'schema_version': 'gpd-ghsl-grid-contract-v1', 'version': 'gpd-ghsl-mollweide-100m-1km-v2'}, 'grid_identity': grid, 'ghsl_source': values['ghsl_source'], 'ghsl_built_assets': ghsl_assets, 'ghsl_epoch_scope': {'published_epochs': list(OFFICIAL_GHSL_EPOCHS), 'available_epochs': registry['available_epochs'], 'selection_rule': SELECTION_RULE}, 'supports': supports, 'support_failures_by_epoch': failures, 'artifacts': sorted(ledger, key=lambda entry: str(entry['role'])), 'network_requests': 0}
        atomic_write(work / 'support_release.json', _json_bytes(release))
        os.replace(work, output_dir)
        return release
    except Exception:
        for child in work.iterdir():
            child.unlink()
        work.rmdir()
        raise

def main(argv: Sequence[str] | None=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cross-stage-release', required=True, type=Path)
    parser.add_argument('--ghsl-registry', required=True, type=Path)
    parser.add_argument('--grid-contract', required=True, type=Path)
    parser.add_argument('--support-inputs', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    build_release(cross_stage_release_path=args.cross_stage_release, ghsl_registry_path=args.ghsl_registry, grid_contract_path=args.grid_contract, support_inputs_path=args.support_inputs, output_dir=args.output_dir)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
