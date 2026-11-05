"""Retrieve street geometry and construct street and postcode support on the 100 m grid"""
from __future__ import annotations
import argparse
import csv
from uuid import uuid4
import http.client
import json
import math
import os
import tempfile
from functools import lru_cache
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from collections import Counter, OrderedDict
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from geographic_uncertainty.routing import ROUTING_FIELDS, RUNNER_FIELDS, atomic_write
SCHEMA_VERSION = 'imputation_stage_a_acquisition_v1'
MOLLWEIDE_RADIUS = 6378137.0
OUTCOMES_VERSION = 'imputation_stage_a_outcomes_v1'
REQUEST_FIELDS = ('request_id', 'observation_id', 'validation_id', 'route', 'request_kind', 'country_code', 'seed_candidate_id', 'selected_query_id', 'seed_latitude', 'seed_longitude', 'osm_type', 'osm_id', 'road', 'postcode', 'locality', 'expected_tl3', 'observed_address', 'max_cells', 'postcode_boundary_mode', 'max_attempts', 'retry_backoff_ms', 'query_timeout_seconds')
REQUEST_FIELDS = (*REQUEST_FIELDS, 'osm_objects')
SUPPORT_SEED_FIELDS = ('observation_id', 'validation_id', 'candidate_id', 'selected_query_id', 'query_index', 'query_variant', 'query_rank', 'support_route', 'seed_latitude', 'seed_longitude', 'seed_coordinate_role', 'seed_status', 'seed_failure_reason', 'nominatim_place_id', 'osm_type', 'osm_id', 'returned_country_code', 'returned_postcode', 'returned_road', 'display_name', 'raw_source_address', 'source_road', 'source_postcode', 'source_locality')
RESPONSE_FIELDS = ('request_id', 'subrequest_id', 'observation_id', 'request_kind', 'cell_id_100m', 'url', 'cache_key', 'http_status', 'error_class', 'retryable', 'response_path', 'response_id', 'latency_ms', 'attempt', 'cache_status')
OUTCOME_FIELDS = ('observation_id', 'validation_id', 'input_route', 'status', 'support_method', 'support_id', 'reason', 'pending_request_ids', 'source_artifact_role')
EARTH_RADIUS = 6371007.181
CANONICAL_GRID = {'crs': 'ESRI:54009', 'origin_x': -18041000, 'origin_y': 9000000, 'extent': [-18041000, -9000000, 18041000, 9000000], 'cell_indexing': 'zero_based_row_column', 'resolution_100m': 100, 'resolution_1km': 1000, 'parent_mapping': 'parent_row=floor(row_100m/10);parent_col=floor(col_100m/10)'}

def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()

def _read_csv(path: Path, fields: Sequence[str]) -> list[dict[str, str]]:
    with path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise ValueError(f'{path.name} schema/order is not exact')
        return [{field: row.get(field, '') for field in fields} for row in reader]

def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, list(fields), lineterminator='\n', extrasaction='raise')
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, '') for field in fields})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _grid_contract(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    expected = {'schemaVersion': 'gpd-ghsl-grid-contract-v1', 'gridVersion': 'gpd-ghsl-mollweide-100m-1km-v2', 'crs': 'ESRI:54009', 'originX': -18041000, 'originY': 9000000, 'extent': [-18041000, -9000000, 18041000, 9000000], 'cellIndexing': 'zero_based_row_column', 'resolution100m': 100, 'resolution1km': 1000}
    if any((value.get(key) != item for key, item in expected.items())):
        raise ValueError('canonical grid contract content mismatch')
    return value

def mollweide(longitude: float, latitude: float) -> tuple[float, float]:
    """Project WGS84 degrees to the spherical Mollweide used by ESRI:54009"""
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError('coordinate is outside WGS84 bounds')
    lam, phi = (math.radians(longitude), math.radians(latitude))
    theta = phi
    if abs(abs(latitude) - 90) < 1e-12:
        theta = math.copysign(math.pi / 2, phi)
    else:
        for _ in range(20):
            delta = (2 * theta + math.sin(2 * theta) - math.pi * math.sin(phi)) / (2 + 2 * math.cos(2 * theta))
            theta -= delta
            if abs(delta) < 1e-13:
                break
    return (2 * math.sqrt(2) * MOLLWEIDE_RADIUS * lam * math.cos(theta) / math.pi, math.sqrt(2) * MOLLWEIDE_RADIUS * math.sin(theta))

def grid_cell(longitude: float, latitude: float) -> tuple[int, int]:
    x, y = mollweide(longitude, latitude)
    column = math.floor((x + 18041000) / 100)
    row = math.floor((9000000 - y) / 100)
    if not 0 <= column < 360820 or not 0 <= row < 180000:
        raise ValueError('coordinate is outside canonical GHSL raster')
    return (row, column)

def cell_centroid(row: int, column: int, *, resolution_m: int=100) -> tuple[float, float]:
    """Inverse spherical Mollweide for a canonical 100 m or 1 km centroid"""
    if resolution_m not in (100, 1000):
        raise ValueError('centroid resolution must be 100 or 1000 metres')
    x = -18041000 + (column + 0.5) * resolution_m
    y = 9000000 - (row + 0.5) * resolution_m
    theta = math.asin(max(-1.0, min(1.0, y / (math.sqrt(2) * MOLLWEIDE_RADIUS))))
    latitude = math.degrees(math.asin((2 * theta + math.sin(2 * theta)) / math.pi))
    denominator = 2 * math.sqrt(2) * MOLLWEIDE_RADIUS * math.cos(theta)
    longitude = math.degrees(math.pi * x / denominator) if denominator else 0.0
    return (longitude, latitude)

def _geometry_lines(geometry: Mapping[str, object]) -> list[list[tuple[float, float]]]:
    kind, coordinates = (geometry.get('type'), geometry.get('coordinates'))
    if kind == 'LineString' and isinstance(coordinates, list):
        candidates = [coordinates]
    elif kind == 'MultiLineString' and isinstance(coordinates, list):
        candidates = coordinates
    else:
        raise ValueError('street lookup lacks LineString/MultiLineString geometry')
    lines = []
    for line in candidates:
        if not isinstance(line, list) or len(line) < 2:
            raise ValueError('street geometry line is invalid')
        parsed = []
        for point in line:
            if not isinstance(point, list) or len(point) < 2:
                raise ValueError('street geometry coordinate is invalid')
            parsed.append((float(point[0]), float(point[1])))
        lines.append(parsed)
    return lines

def _segment_cells(start: tuple[float, float], end: tuple[float, float]) -> set[tuple[int, int]]:
    """Return every canonical grid cell crossed by one projected line segment"""
    x0, y0 = mollweide(*start)
    x1, y1 = mollweide(*end)
    c0, r0 = ((x0 + 18041000) / 100, (9000000 - y0) / 100)
    c1, r1 = ((x1 + 18041000) / 100, (9000000 - y1) / 100)
    column, row = (math.floor(c0), math.floor(r0))
    end_column, end_row = (math.floor(c1), math.floor(r1))
    cells = {(row, column)}
    dc, dr = (c1 - c0, r1 - r0)
    step_c = 1 if dc > 0 else -1 if dc < 0 else 0
    step_r = 1 if dr > 0 else -1 if dr < 0 else 0
    t_delta_c = abs(1 / dc) if dc else math.inf
    t_delta_r = abs(1 / dr) if dr else math.inf
    next_c = column + 1 if step_c > 0 else column
    next_r = row + 1 if step_r > 0 else row
    t_max_c = (next_c - c0) / dc if dc else math.inf
    t_max_r = (next_r - r0) / dr if dr else math.inf
    while (column, row) != (end_column, end_row):
        if t_max_c < t_max_r:
            column += step_c
            t_max_c += t_delta_c
        elif t_max_r < t_max_c:
            row += step_r
            t_max_r += t_delta_r
        else:
            column += step_c
            row += step_r
            t_max_c += t_delta_c
            t_max_r += t_delta_r
        if not 0 <= column < 360820 or not 0 <= row < 180000:
            raise ValueError('street geometry exits canonical GHSL raster')
        cells.add((row, column))
    return cells

# Combine all street segments into a unique set of crossed 100 m cells
def geometry_cells(geometry: Mapping[str, object]) -> list[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for line in _geometry_lines(geometry):
        for start, end in zip(line, line[1:], strict=False):
            cells.update(_segment_cells(start, end))
    return sorted(cells)

def geometry_length_m(geometry: Mapping[str, object]) -> float:
    total = 0.0
    for line in _geometry_lines(geometry):
        for (lon1, lat1), (lon2, lat2) in zip(line, line[1:], strict=False):
            phi1, phi2 = (math.radians(lat1), math.radians(lat2))
            dphi, dlam = (phi2 - phi1, math.radians(lon2 - lon1))
            value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
            total += 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(value)))
    if not math.isfinite(total) or total <= 0:
        raise ValueError('street geometry has no positive finite length')
    return total

# Deduplicate street references by OSM type and ID before requesting their geometries
def _street_objects(request):
    """Pinned, deduplicated same-road objects; old single-object requests still work"""
    raw = request.get('osm_objects')
    values = json.loads(raw) if raw else []
    if not values:
        values = [dict(osm_type=request.get('osm_type'), osm_id=request.get('osm_id'))]
    refs = {(str(v.get('osm_type', '')).casefold(), str(v.get('osm_id', ''))) for v in values}
    if not refs or any(kind not in {'way', 'relation'} or not ident.isdigit() for kind, ident in refs):
        raise ValueError('street request requires pinned OSM ways or relations')
    return sorted(refs)

def _support_seed_index(manifest_path: Path, final_evidence_path: Path) -> tuple[dict[tuple[str, str], dict[str, str]], Path]:
    manifest = json.loads(manifest_path.read_text())
    seed_path = Path(str(manifest.get('artifact_path', '')))
    if manifest.get('schema_version') not in {'geocoder_v3_restricted_support_seed_manifest_v1', 'patent_support_seed_v1'} or manifest.get('artifact_schema_version') != ('patent_support_seed_v1' if manifest.get('schema_version') == 'patent_support_seed_v1' else 'geocoder_v3_restricted_support_seed_v1') or manifest.get('artifact_fields') != list(SUPPORT_SEED_FIELDS) or (manifest.get('access_classification') != 'restricted') or (manifest.get('public_export_allowed') is not False) or (manifest.get('route_scope') != ['street', 'postcode']) or (not seed_path.is_absolute()) or (not seed_path.is_file()):
        raise ValueError('restricted support-seed manifest identity is invalid')
    rows = _read_csv(seed_path, SUPPORT_SEED_FIELDS)
    if manifest.get('row_count') != len(rows):
        raise ValueError('restricted support-seed row count is invalid')
    result = {(row['observation_id'], row['candidate_id']): row for row in rows}
    if len(result) != len(rows) or any((not all(key) for key in result)):
        raise ValueError('restricted support-seed identities are invalid')
    return (result, seed_path)

def prepare(*, runner_path: Path, routing_path: Path, support_seed_manifest_path: Path, final_evidence_manifest_path: Path, grid_contract_path: Path, policy: Mapping[str, object]) -> tuple[list[dict[str, object]], dict[str, object]]:
    _grid_contract(grid_contract_path)
    if policy.get('schema_version') != 'imputation_stage_a_policy_v1':
        raise ValueError('Stage-A policy schema is invalid')
    for field in ('request_budget', 'max_cells_per_observation', 'max_queue', 'max_attempts'):
        if not isinstance(policy.get(field), int) or isinstance(policy.get(field), bool) or policy[field] <= 0:
            raise ValueError(f'policy {field} must be a positive integer')
    boundary_mode = policy.get('postcode_boundary_mode', 'polygon_first_1km')
    if boundary_mode != 'polygon_first_1km':
        raise ValueError('unsupported postcode_boundary_mode')
    postcode_cap = policy.get('max_postcode_cells_per_observation', policy['max_cells_per_observation'])
    if type(postcode_cap) is not int or postcode_cap <= 0:
        raise ValueError('policy max_postcode_cells_per_observation must be a positive integer')
    area_km2 = policy.get('max_postcode_area_km2', 50)
    if area_km2 is not None:
        if isinstance(area_km2, bool) or not isinstance(area_km2, (int, float)) or not math.isfinite(area_km2) or area_km2 < 1:
            raise ValueError('max_postcode_area_km2 must be a finite number of at least one square kilometre')
        postcode_cap = min(postcode_cap, math.floor(area_km2 * 100))
    if 'max_ring' in policy:
        raise ValueError('max_ring is no longer supported; bound postcode searches with max_cells_per_observation')
    timeout = policy.get('query_timeout_seconds')
    if not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise ValueError('query timeout must be an integer from 1 to 60 seconds')
    if not isinstance(policy.get('retry_backoff_ms'), int) or not 0 <= int(policy['retry_backoff_ms']) <= 10000:
        raise ValueError('retry backoff must be an integer from 0 to 10000 milliseconds')
    runner = _read_csv(runner_path, RUNNER_FIELDS)
    routing = _read_csv(routing_path, ROUTING_FIELDS)
    evidence = json.loads(final_evidence_manifest_path.read_text())
    seeds, seed_path = _support_seed_index(support_seed_manifest_path.resolve(strict=True), final_evidence_manifest_path)
    street_objects = json.loads(support_seed_manifest_path.read_text()).get('street_objects', {})
    routes = {row['observation_id']: row for row in routing}
    if len(routes) != len(routing) or {row['observation_id'] for row in runner} != set(routes):
        raise ValueError('runner/routing observation reconciliation failed')
    requests: list[dict[str, object]] = []
    for row in sorted(runner, key=lambda item: item['observation_id']):
        route = routes[row['observation_id']]
        if route['status'] != 'accepted':
            continue
        kind = {'address_passthrough': 'observed_coordinate_map', 'street_support_request': 'osm_street_geometry', 'postcode_support_request': 'postcode_seed_cell'}.get(route['route'])
        if kind is None:
            raise ValueError('accepted route is unsupported')
        seed = None
        if kind == 'observed_coordinate_map':
            latitude, longitude = (row['observed_latitude'], row['observed_longitude'])
        else:
            seed = seeds.get((row['observation_id'], route['seed_candidate_id']))
            expected_route = 'street' if kind == 'osm_street_geometry' else 'postcode'
            if seed is None or seed['validation_id'] != row['validation_id'] or seed['support_route'] != expected_route or (seed['seed_status'] != 'available') or (seed['seed_coordinate_role'] != 'coarse_support_seed_not_accepted_address_coordinate') or (seed['returned_country_code'].upper() != row['country_code']) or (not seed['selected_query_id']) or (kind == 'osm_street_geometry' and (not seed['osm_type'])) or (kind == 'osm_street_geometry' and (not seed['osm_id'])) or (kind == 'postcode_seed_cell' and (not seed['source_postcode'])):
                raise ValueError('accepted support route lacks its exact approved seed provenance')
            latitude, longitude = (seed['seed_latitude'], seed['seed_longitude'])
        if latitude == '' or longitude == '':
            raise ValueError('accepted support request lacks a sealed seed coordinate')
        request_id = 'request-' + uuid4().hex
        requests.append({'osm_objects': json.dumps(street_objects.get(row['observation_id'], [])) if kind == 'osm_street_geometry' else '', 'request_id': request_id, 'observation_id': row['observation_id'], 'validation_id': row['validation_id'], 'route': route['route'], 'request_kind': kind, 'country_code': row['country_code'], 'seed_candidate_id': route['seed_candidate_id'], 'selected_query_id': seed['selected_query_id'] if seed else row['selected_query_id'], 'seed_latitude': latitude, 'seed_longitude': longitude, 'osm_type': seed['osm_type'] if seed else '', 'osm_id': seed['osm_id'] if seed else '', 'road': seed['source_road'] if seed else row['observed_road'], 'postcode': seed['source_postcode'] if seed else row['observed_postcode'], 'locality': seed['source_locality'] if seed else row['observed_locality'], 'expected_tl3': row['expected_tl3'], 'observed_address': seed['raw_source_address'] if seed else row['observed_address'], 'max_cells': postcode_cap if kind == 'postcode_seed_cell' else policy['max_cells_per_observation'], 'postcode_boundary_mode': boundary_mode if kind == 'postcode_seed_cell' else 'not_applicable', 'max_attempts': policy['max_attempts'], 'retry_backoff_ms': policy['retry_backoff_ms'], 'query_timeout_seconds': timeout})
    maximum_calls = sum((0 if row['request_kind'] == 'observed_coordinate_map' else max(1, (len(_street_objects(row)) + 49)//50) if row['request_kind'] == 'osm_street_geometry' else min(1000, int(row['max_cells'])) + 1 for row in requests))
    maximum_queue = max((int(row['max_cells']) for row in requests if row['request_kind'] == 'postcode_seed_cell'), default=0)
    maximum_attempts = maximum_calls * int(policy['max_attempts'])
    if maximum_attempts > policy['request_budget'] or maximum_queue > policy['max_queue']:
        raise ValueError('prepared acquisition exceeds request/queue budget')
    manifest = {'schema_version': SCHEMA_VERSION, 'run_id': evidence.get('run_id'), 'runner_input_path': str(runner_path.resolve()), 'routing_path': str(routing_path.resolve()), 'support_seed_manifest_path': str(support_seed_manifest_path.resolve()), 'support_seed_path': str(seed_path.resolve()), 'final_evidence_manifest_path': str(final_evidence_manifest_path.resolve()), 'grid_contract_path': str(grid_contract_path.resolve()), 'policy': dict(policy), 'request_count': len(requests), 'maximum_logical_queries': maximum_calls, 'maximum_network_attempts': maximum_attempts, 'network_requests': 0, 'access_classification': 'restricted', 'public_export_allowed': False}
    return (requests, manifest)

def _service(service: Mapping[str, object]) -> tuple[str, int]:
    if service.get('schema_version', 'gpd_stage_a_service_identity_v1') != 'gpd_stage_a_service_identity_v1':
        raise ValueError('Unrecognised Nominatim configuration format')
    if not str(service.get('endpoint', '')).startswith(('http://', 'https://')):
        raise ValueError('Nominatim endpoint must start with http:// or https://')
    for field in ('version', 'source_label'):
        if not str(service.get(field, '')).strip():
            raise ValueError(f'Nominatim configuration requires {field}')
    for field, low, high in (('max_queue', 1, None), ('request_budget', 1, None),
                             ('max_attempts', 1, 5), ('retry_backoff_ms', 0, 10000)):
        value = service.get(field)
        if type(value) is not int or value < low or (high is not None and value > high):
            raise ValueError(f'Nominatim {field} must be an integer from {low}' +
                             (f' to {high}' if high is not None else ' upward'))
    return (str(service['endpoint']).rstrip('/'), int(service.get('request_budget', 0)))

# Check the measured service approval before issuing production requests
def check_capacity(service, *, workers=None, max_in_flight=None):
    _service(service)
    if service.get('allocated_cpus',0)<16 or service.get('allocated_memory_gb',0)<64:
        raise ValueError('Batch service requires at least 16 CPUs and 64 GB RAM')
    if not service.get('timeout_version'):raise ValueError('Record the versioned query/statement timeout profile')
    approval_path=service.get('capacity_manifest')
    if not approval_path:raise ValueError('Supply the measured capacity_manifest in the service identity')
    approval=json.loads(Path(approval_path).read_text())
    if not approval.get('passed') or approval.get('selected_client_concurrency')!=service.get('client_workers') or approval.get('delay_seconds')!=0:
        raise ValueError('Service workers and zero pacing must match the passed capacity gate')
    for field in ('endpoint','database_system_identifier','data_revision','version'):
        if approval.get('service_identity',{}).get(field)!=service.get(field):raise ValueError('Capacity approval does not bind this endpoint/data identity')
    if service.get('delay_seconds',0)!=0:raise ValueError('Private batched processing requires explicit zero pacing')
    if workers is not None and workers != approval['selected_client_concurrency']:
        raise ValueError('Geocoder workers must match the approved concurrency')
    limit=approval.get('queues',{}).get('max_in_flight',approval['selected_client_concurrency'])
    if (max_in_flight if max_in_flight is not None else service['max_in_flight']) > limit:
        raise ValueError('In-flight requests exceed the approved capacity')

def _http_get(url: str, timeout: int) -> tuple[int, bytes]:
    if not url.startswith(('http://', 'https://')):
        raise ValueError('Stage-A service URL must use HTTP or HTTPS')
    scheme, remainder = url.split('://', 1)
    authority, separator, target = remainder.partition('/')
    if not authority or not separator:
        raise ValueError('Stage-A service URL is invalid')
    transport = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
    connection = transport(authority, timeout=timeout)
    try:
        connection.request('GET', f'/{target}', headers={'User-Agent': 'GPD-stage-a/1'})
        response = connection.getresponse()
        return (int(response.status), response.read())
    finally:
        connection.close()

def _urlencode(parameters: Mapping[str, str]) -> str:
    safe = b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~'

    def quote(value: str) -> str:
        return ''.join((chr(byte) if byte in safe else f'%{byte:02X}' for byte in value.encode()))
    return '&'.join((f'{quote(key)}={quote(value)}' for key, value in sorted(parameters.items())))

# Prefer a qualified matching postcode polygon within the configured area limit
def _postcode_polygon_cells(payload, request, area_cache=None, area_identity=None):
    """Qualify one postcode polygon, then rasterise its interior cell centres"""
    from shapely.geometry import shape, Point, mapping, box
    from shapely import transform
    from shapely.errors import GEOSException
    from rasterio.features import rasterize
    from rasterio.transform import Affine
    import numpy as np
    if not isinstance(payload, list):
        return None
    normalize = lambda value: ''.join(c for c in str(value).upper() if c.isalnum())
    qualified = {}
    for candidate in payload:
        if not isinstance(candidate, Mapping):
            continue
        address = candidate.get('address', {})
        geometry = candidate.get('geojson', {})
        if not isinstance(address, Mapping) or not isinstance(geometry, Mapping):
            continue
        returned_postcode = address.get('postcode') or candidate.get('name', '')
        if (str(address.get('country_code', '')).upper() != request['country_code'] or
            normalize(returned_postcode) != normalize(request['postcode']) or
            candidate.get('type') not in {'postcode', 'postal_code'} or
            geometry.get('type') not in {'Polygon', 'MultiPolygon'} or
            candidate.get('osm_type') not in {'way', 'relation'} or not candidate.get('osm_id')):
            continue
        identity = (candidate['osm_type'], str(candidate['osm_id']))
        qualified[identity] = candidate
    if len(qualified) != 1:
        return None
    candidate = next(iter(qualified.values()))
    try:
        polygon = shape(candidate['geojson'])
        if not polygon.is_valid or polygon.is_empty or polygon.bounds[2] - polygon.bounds[0] > 180:
            return None
        if not polygon.covers(Point(float(request['seed_longitude']), float(request['seed_latitude']))):
            return None
    except (ValueError, TypeError, OverflowError, GEOSException):
        return None
    geometry = json.dumps(candidate['geojson'], sort_keys=True)
    raster = _cached_area(area_cache, area_identity, 'polygon_100m_centres_v1',
                          [geometry, int(request['max_cells'])],
                          lambda: _rasterise_postcode_polygon(geometry, int(request['max_cells'])))
    return {'candidate': candidate, **raster} if raster is not None else None

@lru_cache(maxsize=32)
# Convert the accepted postcode polygon into cells under the recorded area policy
def _rasterise_postcode_polygon(geometry_json, max_cells):
    """Reuse the polygon grid area and qualify each postcode seed"""
    from shapely.geometry import shape, mapping, box
    from shapely import transform
    from shapely.errors import GEOSException
    from rasterio.features import rasterize
    from rasterio.transform import Affine
    import numpy as np
    polygon = shape(json.loads(geometry_json))
    try:
        projected = transform(polygon, lambda coordinates: np.asarray([mollweide(float(x), float(y)) for x, y in coordinates]))
        if not projected.is_valid or projected.is_empty:
            return None
    except (ValueError, TypeError, OverflowError, GEOSException):
        return None
    left, bottom, right, top = projected.bounds
    min_row, max_row = math.floor((9000000 - top) / 100), math.floor((9000000 - bottom) / 100)
    min_column, max_column = math.floor((left + 18041000) / 100), math.floor((right + 18041000) / 100)
    if min_row < 0 or min_column < 0 or max_row >= 180000 or max_column >= 360820:
        return None
    cells = []
    for row in range(min_row, max_row + 1, 256):
        for column in range(min_column, max_column + 1, 256):
            height, width = min(256, max_row - row + 1), min(256, max_column - column + 1)
            x, y = -18041000 + column * 100, 9000000 - row * 100
            if not projected.intersects(box(x, y - height * 100, x + width * 100, y)):
                continue
            pixels = rasterize([(mapping(projected), 1)], out_shape=(height, width),
                               transform=Affine(100, 0, x, 0, -100, y), all_touched=False, dtype='uint8')
            cells.extend((row + int(dr), column + int(dc)) for dr, dc in np.argwhere(pixels))
            if len(cells) > max_cells:
                return {'cells': [], 'cell_cap_reached': True}
    return {'cells': tuple(sorted(cells)), 'cell_cap_reached': False} if cells else None

@lru_cache(maxsize=2)
def _point_tl3_index(path, code_property, modified_ns, size):
    from geocoding.validation.spatial_checks import TL3Index
    return TL3Index.from_geojson(Path(path), code_property)

def _postcode_point(payload, request):
    """Qualify one postcode Point by exact country, postcode and source TL3"""
    if request.get('postcode_boundary_mode') != 'polygon_first_1km' or not isinstance(payload, list):
        return None
    normalize = lambda value: ''.join(c for c in str(value).upper() if c.isalnum())
    matches = {}
    for candidate in payload:
        if not isinstance(candidate, Mapping):
            continue
        address, geometry = candidate.get('address', {}), candidate.get('geojson', {})
        if not isinstance(address, Mapping) or not isinstance(geometry, Mapping):
            continue
        if (str(address.get('country_code', '')).upper() != request['country_code'] or
            normalize(address.get('postcode') or candidate.get('name', '')) != normalize(request['postcode']) or
            candidate.get('type') not in {'postcode', 'postal_code'}):
            continue


        identity = (candidate.get('osm_type'), str(candidate.get('osm_id', '')),
                    json.dumps(geometry, sort_keys=True))
        matches[identity] = candidate
    if len(matches) != 1:
        return None
    candidate = next(iter(matches.values()))
    geometry = candidate['geojson']
    coordinates = geometry.get('coordinates')
    if geometry.get('type') != 'Point' or not isinstance(coordinates, list) or len(coordinates) != 2:
        return None
    try:
        lon, lat = map(float, coordinates)
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            return None
        boundary = request.get('point_tl3_boundaries', {})
        if not isinstance(boundary, Mapping):
            return None
        path = Path(str(boundary.get('path', '')))
        code_property = str(boundary.get('code_property', ''))
        expected = sorted({code.strip() for code in str(request.get('expected_tl3', '')).split(';') if code.strip()})
        if not path.is_absolute() or not path.is_file() or not code_property or not expected:
            return None
        stat = path.stat()
        index = _point_tl3_index(str(path), code_property, stat.st_mtime_ns, stat.st_size)
        derived = index.locate(lon, lat)
        if not set(expected).intersection(derived):
            return None
        slon, slat = float(request['seed_longitude']), float(request['seed_latitude'])
        h = math.sin(math.radians(lat-slat)/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(slat))*math.sin(math.radians(lon-slon)/2)**2
        return dict(longitude=lon, latitude=lat, expected_tl3=expected, derived_tl3=derived,
                    tl3_agreement=True, boundaries=dict(boundary),
                    distance_from_original_seed_m=round(EARTH_RADIUS*2*math.asin(min(1,math.sqrt(h))), 3),
                    centre_rule='exact_country_postcode_point_passing_source_tl3')
    except (ValueError, TypeError, OverflowError, OSError):
        return None

APPROXIMATION_METHODS = {'postcode_1km_centre_flood_fill', 'postcode_point_single_cell'}

def _coarse_relation(payload, request):
    """None is missing evidence, never a conclusive postcode boundary"""
    address = payload.get('address', {}) if isinstance(payload, Mapping) else {}
    if not isinstance(address, Mapping):
        return None
    country, postcode = address.get('country_code'), address.get('postcode')
    norm = lambda value: ''.join(c for c in value.upper() if c.isalnum())
    if not isinstance(country, str) or not country.strip() or not isinstance(postcode, str) or not norm(postcode):
        return None
    return country.upper() == request['country_code'] and norm(postcode) == norm(request['postcode'])


def _coarse_matches(payload, request):
    return _coarse_relation(payload, request) is True

# Estimate point-only postcode areas from bounded 1 km centre checks
def _coarse_postcode_cells(payload, request, evidence, area_cache=None, area_identity=None):
    """Reconstruct confirmed queen-connected 1 km cells from saved responses.

    A qualified postcode Point establishes its containing cell. Only neighbouring
    cell centres are queried; missing evidence cannot close their frontier.
    Returned cells retain the canonical 100 m GHSL grid
    """
    point = _postcode_point(payload, request)
    if point is None:
        return None
    fine = grid_cell(point['longitude'], point['latitude'])
    start = (fine[0] // 10, fine[1] // 10)
    reverse = {str(row['cell_id_100m']): row for row in _terminal_evidence(evidence) if row['request_kind'] == 'reverse'}
    relations = tuple(sorted((cell, None if row.get('error_class') else _coarse_relation(_response_json(row), request), bool(row.get('error_class'))) for cell, row in reverse.items()))
    component = _cached_area(area_cache, area_identity, 'postcode_1km_component_confirmed_v2',
                             [start, int(request['max_cells']), relations],
                             lambda: _coarse_postcode_component(start, int(request['max_cells']), relations))
    return {**component, 'point': point} if component is not None else None

@lru_cache(maxsize=32)
def _coarse_postcode_component(start, max_cells, relations):
    reverse = {cell: (relation, failed) for cell, relation, failed in relations}
    queue = list(_neighbours(start))
    if any(not (0 <= r < 18000 and 0 <= c < 36082) for r,c in queue):
        return None
    seen, accepted = {start, *queue}, {start}
    missing_fields, incomplete = [], False
    if 100 > max_cells:
        return {'cells': [], 'method': 'postcode_point_single_cell', 'queried_cells': 0, 'area_limit_exceeded': True}
    while queue:
        cell = queue.pop(0)
        row = reverse.get(f'km:r{cell[0]}c{cell[1]}')
        if row is None or row[1]:
            incomplete = True
            continue
        relation = row[0]
        if relation is None:
            missing_fields.append(f'r{cell[0]}c{cell[1]}')
            continue
        if not relation:
            continue
        accepted.add(cell)
        if len(accepted) * 100 > max_cells:
            return {'cells': [], 'method': 'postcode_1km_centre_flood_fill', 'queried_cells': len(seen) - 1, 'area_limit_exceeded': True}
        for neighbour in _neighbours(cell):
            if not (0 <= neighbour[0] < 18000 and 0 <= neighbour[1] < 36082):
                return None
            if neighbour not in seen:
                if len(seen) - 1 >= min(1000, max_cells):
                    return None
                seen.add(neighbour)
                queue.append(neighbour)
    if incomplete:
        return None
    # Retain confirmed cells without treating missing evidence as a closed boundary
    method = 'postcode_point_single_cell' if len(accepted) == 1 else 'postcode_1km_centre_flood_fill'
    cells = sorted((r, c) for row, column in accepted for r in range(row*10, row*10+10) for c in range(column*10, column*10+10))
    return {'cells': cells, 'method': method, 'queried_cells': len(seen) - 1, 'seed_cell_id_1km': f'r{start[0]}c{start[1]}', 'extent_status': 'unresolved_missing_neighbour_postcode' if missing_fields else 'closed_matching_centres', 'missing_frontier_cells': missing_fields, 'known_matching_cells_not_allocated': 0}

@contextmanager
# Keep response bodies and derived areas together under a single-writer cache lock
def _geographic_cache(service):
    directory = service.get('response_cache_dir')
    if not directory:
        yield None, None
        return
    import fcntl
    from geocoding.geocoder.client import NominatimClient
    fields = ('database_system_identifier', 'data_revision', 'version')
    if any(not str(service.get(field, '')).strip() for field in fields):
        raise ValueError('geographic cache requires database identity, data_revision and version')
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('response_cache_dir must be an absolute task-owned path')
    directory.mkdir(parents=True, exist_ok=True)
    identity = {field: service[field] for field in fields}
    with (directory / '.geographic-writer.lock').open('a') as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cache = NominatimClient(endpoint=str(service['endpoint']), cache_dir=directory,
                                user_agent='patent-code/geographic-cache', delay_seconds=0)
        cache._cache_connection.execute('CREATE TABLE IF NOT EXISTS geographic_bodies(file_id INTEGER PRIMARY KEY, cache_key TEXT UNIQUE NOT NULL)')
        cache._cache_connection.commit()
        try:
            yield cache, identity
        finally:
            cache._cache_connection.close()

def _cached_area(cache, identity, kind, parameters, build):
    if cache is None:
        return build()
    key = json.dumps({'namespace': 'geographic_area_v1', 'data': identity,
                      'algorithm': kind, 'grid': CANONICAL_GRID, 'parameters': parameters}, sort_keys=True)
    saved = cache._get_cached(key)
    if saved is not None:
        cache._count('cache_hits')
        return saved[0][0]['area']
    area = build()
    cache._store_cached(key, 'derived:' + kind, [{'area': area}])
    return area

# Reuse compatible cached evidence and request only what is missing
def acquire(requests, service, *, raw_dir, getter=_http_get):
    """Reuse successful geographic responses under an explicit data revision"""
    with _geographic_cache(service) as (cache, identity):
        return _acquire(requests, service, raw_dir=raw_dir, getter=getter,
                        response_cache=cache, cache_identity=identity)

# Reuse geographic evidence and request only the missing responses
def _acquire(requests: Sequence[Mapping[str, object]], service: Mapping[str, object], *, raw_dir: Path, getter: Callable[[str, int], tuple[int, bytes]]=_http_get, response_cache=None, cache_identity=None) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Retrieve geographic evidence from the configured service"""
    endpoint, service_budget = _service(service)
    if raw_dir.exists():
        raise FileExistsError(raw_dir)
    raw_dir.mkdir(parents=True)
    os.chmod(raw_dir, 448)
    lock_fd = os.open(raw_dir / '.single-writer.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    os.write(lock_fd, str(service.get('cache_writer_id', 'stage-a-' + str(os.getpid()))).encode())
    os.close(lock_fd)
    ledger: list[dict[str, object]] = []
    calls = 0
    recent = OrderedDict()
    recent_bytes = 0
    recent_lock = Lock()
    verified_bodies = OrderedDict()
    body_lock = Lock()
    budget_lock = Lock()
    workers = int(service.get("client_workers", 1))
    if not 1 <= workers <= 8:
        raise ValueError("postcode client_workers must be between one and eight and capacity approved")

    def fetch(request: Mapping[str, object], subrequest: str, kind: str, params: dict[str, str], cell: str='') -> dict[str, object]:
        nonlocal calls, recent_bytes
        query = _urlencode(params)
        url = f'{endpoint}/{kind}?{query}'
        cache_key = json.dumps({'namespace': 'geographic_response_v1', 'data': cache_identity,
                                'path': kind, 'parameters': sorted(params.items())}, sort_keys=True)
        with recent_lock:
            cached = recent.get(cache_key)
            if cached is not None:
                recent.move_to_end(cache_key)
        if cached is None and response_cache is not None:
            saved = response_cache._get_cached(cache_key)
            if saved is not None:
                cached = saved[0][0]['body'].encode('utf-8')
        final_row = None
        for attempt in range(1, int(service['max_attempts']) + 1):
            if cached is None:
                with budget_lock:
                    if calls >= service_budget:
                        raise ValueError('service request budget exhausted')
                    calls += 1
            status, body, error, retryable = (0, b'', '', False)
            cache_status = 'service_cache_unreported'
            started = time.monotonic()
            try:
                if cached is not None:
                    status, body, cache_status = 200, cached, 'hit'
                else:
                    returned = getter(url, int(request['query_timeout_seconds']))
                    status, body = (returned[0], returned[1])
                    cache_status = returned[2] if len(returned) > 2 else 'miss'
                    if status == 200 and isinstance(json.loads(body), (dict, list)):
                        with recent_lock:
                            previous = recent.pop(cache_key, b'')
                            recent_bytes -= len(previous)
                            if len(body) <= 64 * 1024 * 1024:
                                recent[cache_key] = body
                                recent_bytes += len(body)
                            while len(recent) > 8192 or recent_bytes > 64 * 1024 * 1024:
                                recent_bytes -= len(recent.popitem(last=False)[1])
                        if response_cache is not None:
                            response_cache._store_cached(cache_key, url, [{'body': body.decode('utf-8')}])
                if status >= 400:
                    error, retryable = (f'http_{status}', status == 429 or status >= 500)
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                error, retryable = (type(exc).__name__, True)
            attempt_id = f'{subrequest}-a{attempt}'
            if status == 200 and not error and response_cache is not None:
                with response_cache._cache_lock:
                    cache_row = response_cache._cache_connection.execute(
                        'SELECT file_id FROM geographic_bodies WHERE cache_key=?', (cache_key,)).fetchone()
                    if cache_row is None:
                        cursor = response_cache._cache_connection.execute(
                            'INSERT INTO geographic_bodies(cache_key) VALUES(?)', (cache_key,))
                        number = cursor.lastrowid
                        response_cache._cache_connection.commit()
                    else:
                        number = cache_row[0]
                path = Path(response_cache.cache_dir) / 'raw' / str(number // 10000) / f'response-{number}.json'
                with body_lock:
                    if cache_key not in verified_bodies:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if path.exists():
                            if path.read_bytes() != body:
                                raise ValueError('saved geographic response body differs from cache')
                        else:
                            atomic_write(path, body)
                            os.chmod(path, 384)
                        verified_bodies[cache_key] = True
                        if len(verified_bodies) > 8192:
                            verified_bodies.popitem(last=False)
                    else:
                        verified_bodies.move_to_end(cache_key)
            else:
                path = raw_dir / f'{attempt_id}.json'
                atomic_write(path, body)
                os.chmod(path, 384)
            final_row = {'request_id': request['request_id'], 'subrequest_id': attempt_id, 'observation_id': request['observation_id'], 'request_kind': kind, 'cell_id_100m': cell, 'url': url, 'cache_key': cache_key, 'http_status': status, 'error_class': error, 'retryable': 'true' if retryable else 'false', 'response_path': str(path.resolve()), 'response_id': str(path.resolve()), 'latency_ms': round((time.monotonic() - started) * 1000, 3), 'attempt': attempt, 'cache_status': cache_status}
            ledger.append(final_row)
            if not retryable or attempt == int(service['max_attempts']):
                break
            time.sleep(int(service['retry_backoff_ms']) * attempt / 1000)
        assert final_row is not None
        return final_row
    for request in requests:
        if int(request['max_attempts']) != int(service['max_attempts']) or int(request['retry_backoff_ms']) != int(service['retry_backoff_ms']):
            raise ValueError('prepared retry policy differs from service identity')
        kind = str(request['request_kind'])
        if kind == 'observed_coordinate_map':
            continue
        if kind == 'osm_street_geometry':
            refs = _street_objects(request)
            for start in range(0, len(refs), 50):
                ids = ','.join({'way': 'W', 'relation': 'R'}[kind] + ident for kind, ident in refs[start:start+50])
                fetch(request, f"{request['request_id']}-lookup-{start//50}", 'lookup', {'osm_ids': ids, 'format': 'jsonv2', 'polygon_geojson': '1', 'addressdetails': '1'})
        elif kind == 'postcode_seed_cell':
            if request.get('postcode_boundary_mode') == 'polygon_first_1km':
                probe = fetch(request, f"{request['request_id']}-postcode-boundary", 'search',
                              {'postalcode': str(request['postcode']), 'countrycodes': str(request['country_code']).lower(),
                               'format': 'jsonv2', 'addressdetails': '1', 'polygon_geojson': '1', 'limit': '5'})
                if not probe['error_class']:
                    polygon = _postcode_polygon_cells(_response_json(probe), request, response_cache, cache_identity)
                    if polygon is not None:
                        continue
                point_request = {**request, 'point_tl3_boundaries': dict(service.get('point_tl3_boundaries', {}))}
                point = _postcode_point(_response_json(probe), point_request) if not probe['error_class'] else None
                if point is None:
                    continue
                fine = grid_cell(point['longitude'], point['latitude'])
                start = (fine[0] // 10, fine[1] // 10)
                if 100 > int(request['max_cells']):
                    continue
                queue = [cell for cell in _neighbours(start) if 0 <= cell[0] < 18000 and 0 <= cell[1] < 36082]
                queued = {start, *queue}
                def fetch_coarse(cell_pair):
                    row, column = cell_pair
                    longitude, latitude = cell_centroid(row, column, resolution_m=1000)
                    cell = f'km:r{row}c{column}'
                    return fetch(request, f"{request['request_id']}-{cell}", 'reverse',
                                 {'lat': format(latitude, '.12f'), 'lon': format(longitude, '.12f'), 'format': 'jsonv2', 'addressdetails': '1', 'zoom': '18'}, cell)
                matching_coarse_cells = 1
                area_limit_exceeded = False
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    while queue and not area_limit_exceeded:
                        if len(queue) > int(service['max_queue']):
                            raise ValueError('postcode frontier exceeds service queue bound')
                        batch, queue = queue[:workers], queue[workers:]
                        for (row, column), terminal in zip(batch, pool.map(fetch_coarse, batch)):
                            if terminal['error_class'] or not _coarse_matches(_response_json(terminal), request):
                                continue
                            matching_coarse_cells += 1
                            if matching_coarse_cells * 100 > int(request['max_cells']):
                                area_limit_exceeded = True
                                break
                            for neighbour in _neighbours((row, column)):
                                if neighbour not in queued and 0 <= neighbour[0] < 18000 and 0 <= neighbour[1] < 36082 and len(queued) - 1 < min(1000, int(request['max_cells'])):
                                    queued.add(neighbour)
                                    queue.append(neighbour)
                continue

            raise ValueError('Only polygon_first_1km postcode requests are supported')
        else:
            raise ValueError('unknown request kind')
    latencies = sorted((float(row['latency_ms']) for row in ledger))

    def percentile(fraction: float) -> float:
        if not latencies:
            return 0.0
        return latencies[math.ceil(fraction * len(latencies)) - 1]
    manifest = {'schema_version': 'imputation_stage_a_response_ledger_v1', 'service_identity': dict(service), 'source_label': service.get('source_label', service.get('osm_snapshot_path', '')), 'cache_endpoint': service.get('cache_endpoint', str(raw_dir)), 'cache_writer_id': service.get('cache_writer_id', 'stage-a-' + str(os.getpid())), 'request_parameters_version': service.get('request_parameters_version', service.get('version', '')), 'response_rows': len(ledger), 'network_requests': calls, 'queried_cells': len({row['cell_id_100m'] for row in ledger if row['cell_id_100m']}), 'attempt_rows': len(ledger), 'latency_ms_total': round(sum(latencies), 3), 'latency_ms_p50': percentile(0.5), 'latency_ms_p95': percentile(0.95), 'cache_status_counts': dict(sorted(Counter((str(row['cache_status']) for row in ledger)).items())), 'cache_hits': sum((str(row['cache_status']).casefold() == 'hit' for row in ledger)), 'retry_attempts': sum((int(row['attempt']) > 1 for row in ledger)), 'max_attempts': service['max_attempts'], 'retry_backoff_ms': service['retry_backoff_ms'], 'termination_rule': 'queen_frontier_exhausted_or_queried_cell_cap', 'frontier_order': 'seed_first_fifo_queen_dr_then_dc', 'access_classification': 'restricted', 'public_export_allowed': False, 'single_cache_writer': True}
    manifest.update(client_workers=workers, max_in_flight=workers, delay_seconds=0, response_cache_identity=cache_identity, response_cache_dir=service.get('response_cache_dir'), cache_entries_written=response_cache.telemetry()['cache_writes'] if response_cache else 0, postcode_area_cache_hits=response_cache.telemetry()['cache_hits'] if response_cache else 0)
    manifest['reverse_queries'] = sum(row['request_kind'] == 'reverse' for row in ledger)
    return (ledger, manifest)

def _verified_response_path(row: Mapping[str, object]) -> Path:
    path = Path(str(row['response_path']))
    if not path.is_absolute() or not path.is_file():
        raise ValueError('response bytes do not match sealed ledger')
    return path

def _response_json(row: Mapping[str, object]) -> object:
    path = _verified_response_path(row)
    return json.loads(path.read_text()) if path.stat().st_size else None

def _parent(row: int, column: int) -> str:
    return f'r{row // 10}c{column // 10}'

def _neighbours(cell: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    row, column = cell
    return tuple(((row + dr, column + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc))

def _terminal_evidence(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    latest: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in rows:
        key = (str(row['request_kind']), str(row['cell_id_100m']))
        if key not in latest or int(row['attempt']) > int(latest[key]['attempt']):
            latest[key] = row
    return list(latest.values())

def _seed_provenance(request: Mapping[str, object]) -> dict[str, object]:
    return {'seed_candidate_id': request['seed_candidate_id'], 'selected_query_id': request['selected_query_id'], 'coordinate_role': 'coarse_support_seed_not_accepted_address_coordinate', 'seed_latitude': request['seed_latitude'], 'seed_longitude': request['seed_longitude'], 'osm_type': request['osm_type'], 'osm_id': request['osm_id']}

# Turn verified responses into geographic areas or explicit pending/withheld outcomes
def finalize(requests, routing_rows, responses, *, service_identity=None, run_id=''):
    with _geographic_cache(service_identity or {}) as (cache, identity):
        outcomes, supports, manifest = _finalize(requests, routing_rows, responses,
            service_identity=service_identity, run_id=run_id, area_cache=cache, area_identity=identity)
        manifest['postcode_area_cache_hits'] = cache.telemetry()['cache_hits'] if cache else 0
        return outcomes, supports, manifest

# Build accepted areas and retain the construction method and withholding evidence
def _finalize(requests: Sequence[Mapping[str, object]], routing_rows: Sequence[Mapping[str, object]], responses: Sequence[Mapping[str, object]], *, service_identity: Mapping[str, object] | None=None, run_id: str='', area_cache=None, area_identity=None) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Construct geographic support from service responses"""
    routes = {str(row.get('observation_id', '')): row for row in routing_rows}
    if '' in routes or len(routes) != len(routing_rows):
        raise ValueError('routing observation identities are invalid')
    accepted_ids = {oid for oid, row in routes.items() if row.get('status') == 'accepted'}
    by_request: dict[str, list[Mapping[str, object]]] = {}
    subrequest_ids: set[str] = set()
    for row in responses:
        subrequest_id = str(row.get('subrequest_id', ''))
        if not subrequest_id or subrequest_id in subrequest_ids:
            raise ValueError('response subrequest identities are invalid')
        subrequest_ids.add(subrequest_id)
        by_request.setdefault(str(row['request_id']), []).append(row)
    request_by_observation = {str(row['observation_id']): row for row in requests}
    request_by_id = {str(row['request_id']): row for row in requests}
    if len(request_by_observation) != len(requests) or len(request_by_id) != len(requests) or set(request_by_observation) != accepted_ids:
        raise ValueError('support requests do not exactly cover accepted routing')
    for request in requests:
        route = routes[str(request['observation_id'])]
        if request.get('validation_id') != route.get('validation_id') or request.get('route') != route.get('route'):
            raise ValueError('support request does not bind immutable routing')
    for request_id, rows in by_request.items():
        request = request_by_id.get(request_id)
        if request is None or any((row.get('observation_id') != request.get('observation_id') for row in rows)):
            raise ValueError('response ledger contains foreign request evidence')
    if any((row['request_kind'] != 'observed_coordinate_map' for row in requests)):
        _service(service_identity or {})
    outcomes: list[dict[str, object]] = []
    supports: list[dict[str, object]] = []
    for route in sorted(routing_rows, key=lambda row: str(row['observation_id'])):
        oid = str(route['observation_id'])
        request = request_by_observation.get(oid)
        if route['status'] != 'accepted':
            status, reason, method, support_id, role = ('withheld', str(route.get('withholding_reason', '')) or 'upstream_withheld', '', '', '')
        elif request['request_kind'] == 'observed_coordinate_map':
            row, column = grid_cell(float(request['seed_longitude']), float(request['seed_latitude']))
            method = 'observed_coordinate_passthrough'
            support = {'schema_version': 'imputation_stage_a_support_v1', 'observation_id': oid, 'support_method': method, 'closed': True, 'grid_identity': CANONICAL_GRID, 'accepted_cells': [{'cell_id_100m': f'r{row}c{column}', 'parent_cell_id_1km': _parent(row, column)}], 'support_cell_count': 1, 'support_area_m2': 10000, 'source': {'request_id': request['request_id'], 'coordinate': [request['seed_longitude'], request['seed_latitude']], 'country_code': request['country_code']}, 'source_artifacts': {'observed_coordinate': f"observed_support_{request['request_id']}"}, 'access_classification': 'restricted', 'public_export_allowed': False}
            support_id = 'support-' + uuid4().hex
            support['support_id'] = support_id
            supports.append(support)
            status, reason, role = ('support_ready', '', f"observed_support_{request['request_id']}")
        elif request['request_kind'] == 'osm_street_geometry':
            method = 'actual_street_geometry_100m_support'
            raw_evidence = by_request.get(str(request['request_id']), [])
            evidence = _terminal_evidence(raw_evidence)
            pending = [str(row['subrequest_id']) for row in evidence if row.get('retryable') == 'true']
            for item in evidence:
                if item.get('error_class'):
                    _verified_response_path(item)
                else:
                    _response_json(item)
            lookup_rows = [row for row in evidence if row['request_kind'] == 'lookup']
            support = None
            if pending:
                status, reason = ('pending', 'retryable_or_failed_service_evidence')
            elif any((row.get('error_class') for row in evidence)):
                status, reason = ('withheld', 'conclusive_service_no_result')
            elif len(lookup_rows) != max(1, (len(_street_objects(request)) + 49)//50):
                status, reason = ('pending', 'service_response_ledger_missing')
            elif route.get('seed_tl3_pass') != '1':
                status, reason = ('withheld', 'street_seed_tl3_failed')
            else:
                payloads = [_response_json(item) for item in lookup_rows]
                candidates = [c for payload in payloads if isinstance(payload, list) for c in payload]
                expected_objects = set(_street_objects(request))
                actual_objects = {(str(c.get('osm_type', '')).casefold(), str(c.get('osm_id', ''))) for c in candidates if isinstance(c, Mapping)}
                if len(candidates) != len(expected_objects) or actual_objects != expected_objects:
                    status, reason = ('withheld', 'street_geometry_ambiguous_or_missing')
                elif any(not isinstance(c.get('address'), Mapping) or str(c['address'].get('country_code', '')).upper() != request['country_code'] for c in candidates):
                    status, reason = ('withheld', 'street_seed_identity_mismatch')
                else:
                    geometries = [c.get('geojson') for c in candidates]
                    if any(not isinstance(g, Mapping) or g.get('type') not in {'LineString', 'MultiLineString'} for g in geometries):
                        status, reason = ('withheld', 'street_geometry_invalid_or_unsupported')
                    else:
                        geometry = geometries[0] if len(geometries) == 1 else dict(type='MultiLineString', coordinates=[line for g in geometries for line in ([g['coordinates']] if g['type'] == 'LineString' else g['coordinates'])])
                        try:
                            if not isinstance(geometry, Mapping):
                                raise ValueError('street geometry is invalid')
                            cells = geometry_cells(geometry)
                            length = geometry_length_m(geometry)
                        except (ValueError, TypeError, OverflowError):
                            cells = None
                        if cells is None:
                            status, reason = ('withheld', 'street_geometry_invalid_or_unsupported')
                        elif len(cells) > int(request['max_cells']):
                            status, reason = ('withheld', 'street_cell_cap_reached')
                        elif not cells:
                            status, reason = ('withheld', 'street_geometry_empty')
                        else:
                            accepted = [{'cell_id_100m': f'r{row}c{column}', 'parent_cell_id_1km': _parent(row, column)} for row, column in cells]
                            support = {'schema_version': 'imputation_stage_a_support_v1', 'observation_id': oid, 'support_method': method, 'closed': True, 'grid_identity': CANONICAL_GRID, 'geometry_type': geometry.get('type'), 'street_length_m': length, 'street_length_method': 'spherical_great_circle_r6371007.181', 'support_cell_count': len(accepted), 'support_area_m2': len(accepted) * 10000, 'source_artifacts': {'street_geometry': f"street_support_{request['request_id']}"}, 'accepted_cells': accepted, 'country_rule': 'accepted_seed_identity_only_no_geometry_clip', 'tl3_rule': 'seed_validation_only_not_geometry_clip', 'postcode_rule': 'seed_validation_only_not_geometry_clip', 'seed_provenance': _seed_provenance(request), 'selected_osm_objects': [list(ref) for ref in sorted(expected_objects)], 'source': {'request_id': request['request_id']}, 'access_classification': 'restricted', 'public_export_allowed': False}
                            support_id = 'support-' + uuid4().hex
                            support['support_id'] = support_id
                            supports.append(support)
                            status, reason = ('support_ready', '')
            support_id = str(support.get('support_id', '')) if support else ''
            role = f"street_support_{request['request_id']}" if support else ''
        else:
            if request.get('postcode_boundary_mode') == 'polygon_first_1km':
                request = {**request, 'point_tl3_boundaries': dict((service_identity or {}).get('point_tl3_boundaries', {}))}
            raw_evidence = by_request.get(str(request['request_id']), [])
            evidence = _terminal_evidence(raw_evidence)
            for row in evidence:
                if row.get('error_class'):
                    _verified_response_path(row)
                else:
                    _response_json(row)
            pending = [str(row['subrequest_id']) for row in evidence if row.get('retryable') == 'true']
            method = 'nominatim_derived_postcode_support'
            searches = [row for row in evidence if row['request_kind'] == 'search' and not row.get('error_class')]
            polygon = _postcode_polygon_cells(_response_json(searches[0]), request, area_cache, area_identity) if len(searches) == 1 else None
            if polygon is not None and not pending:
                if polygon['cell_cap_reached']:
                    status, reason, support_id, role = ('withheld', 'postcode_area_limit_exceeded', '', '')
                else:
                    candidate = polygon['candidate']
                    accepted_cells = [{'cell_id_100m': f'r{row}c{column}', 'parent_cell_id_1km': _parent(row, column)} for row, column in polygon['cells']]
                    support_id = 'support-' + uuid4().hex
                    support = dict(schema_version='imputation_stage_a_support_v1', observation_id=oid,
                                   support_method=method, support_id=support_id, closed=True, grid_identity=CANONICAL_GRID,
                                   bounded_flood_fill=False, postcode_construction_method='qualified_osm_postcode_polygon',
                                   support_authority_label='non_authoritative_postcode_support',
                                   source_artifacts={'query_ledger': f"postcode_query_ledger_{request['request_id']}", 'accepted_support': f"postcode_support_{request['request_id']}"},
                                   accepted_cells=accepted_cells, support_cell_count=len(accepted_cells), support_area_m2=len(accepted_cells)*10000,
                                   seed_provenance=_seed_provenance(request),
                                   boundary_provenance={'osm_type': candidate['osm_type'], 'osm_id': str(candidate['osm_id']), 'geometry_type': candidate['geojson']['type'], 'rasterisation': '100m_cell_centres_inside_polygon_with_holes'},
                                   source={'request_id': request['request_id'], 'boundary_request': dict(request)},
                                   access_classification='restricted', public_export_allowed=False)
                    supports.append(support)
                    status, reason, role = ('support_ready', '', f"postcode_support_{request['request_id']}")
            elif request.get('postcode_boundary_mode') == 'polygon_first_1km' and not pending and not any(row.get('error_class') for row in evidence):
                coarse = _coarse_postcode_cells(_response_json(searches[0]), request, evidence, area_cache, area_identity) if len(searches) == 1 else None
                if coarse is None:
                    status, reason, support_id, role = ('withheld', 'postcode_1km_support_not_qualified_or_frontier_incomplete', '', '')
                elif coarse.get('area_limit_exceeded'):
                    status, reason, support_id, role = ('withheld', 'postcode_area_limit_exceeded', '', '')
                else:
                    cells = coarse['cells']
                    support_id = 'support-' + uuid4().hex
                    support = dict(schema_version='imputation_stage_a_support_v1', observation_id=oid,
                                   support_method=method, support_id=support_id, closed=True, grid_identity=CANONICAL_GRID,
                                   bounded_flood_fill=False, postcode_construction_method=coarse['method'],
                                   output_variant='postcode_1km_approximation',
                                   postcode_seed_rule='validated_point_containing_cell_v1',
                                   seed_cell_id_1km=coarse['seed_cell_id_1km'],
                                   postcode_extent_status=coarse['extent_status'],
                                   missing_frontier_cells=coarse['missing_frontier_cells'],
                                   known_matching_cells_not_allocated=coarse['known_matching_cells_not_allocated'],
                                   support_authority_label='approximate_neighbourhood_not_postcode_boundary',
                                   source_artifacts={'query_ledger': f"postcode_query_ledger_{request['request_id']}", 'accepted_support': f"postcode_support_{request['request_id']}", 'tl3_boundaries': f"postcode_tl3_boundaries_{request['request_id']}"},
                                   point_provenance=coarse['point'],
                                   accepted_cells=[{'cell_id_100m': f'r{r}c{c}', 'parent_cell_id_1km': _parent(r,c)} for r,c in cells],
                                   support_cell_count=len(cells), support_area_m2=len(cells)*10000,
                                   seed_provenance=_seed_provenance(request),
                                   source={'request_id': request['request_id'], 'boundary_request': dict(request)},
                                   access_classification='restricted', public_export_allowed=False)
                    supports.append(support)
                    status, reason, role = ('support_ready', '', f"postcode_support_{request['request_id']}")
            elif any(row['request_kind'] == 'frontier_guard' and row.get('error_class') == 'safety_cap_reached' for row in evidence):
                status, reason, support_id, role = ('withheld', 'postcode_search_limit_reached', '', '')
            elif pending:
                status, reason, support_id, role = ('pending', 'retryable_or_failed_service_evidence', '', '')
            elif any((row.get('error_class') for row in evidence)):
                status, reason, support_id, role = ('withheld', 'conclusive_service_no_result', '', '')
            else:
                raise ValueError('Only polygon_first_1km postcode requests are supported')
        pending_ids = []
        if status == 'pending' and request is not None:
            pending_ids = [str(row['subrequest_id']) for row in by_request.get(str(request['request_id']), []) if row.get('retryable') == 'true' or row.get('error_class')] or [str(request['request_id'])]
        if status != 'support_ready':
            method, support_id, role = ('', '', '')
        outcomes.append({'observation_id': oid, 'validation_id': route['validation_id'], 'input_route': route['route'], 'status': status, 'support_method': method, 'support_id': support_id, 'reason': reason, 'pending_request_ids': json.dumps(sorted(pending_ids)) if status == 'pending' else '[]', 'source_artifact_role': role})
    counts = Counter((str(row['status']) for row in outcomes))
    if len(outcomes) != len(routing_rows) or sum(counts.values()) != len(routing_rows):
        raise ValueError('Stage-A outcomes are not exhaustive')
    postcode_countries = {str(row['observation_id']): str(row['country_code'])
                          for row in requests if row['request_kind'] == 'postcode_seed_cell'}
    postcode_outcomes = {}
    for row in outcomes:
        country = postcode_countries.get(str(row['observation_id']))
        if country:
            reason = str(row['reason']).split(':', 1)[0] or str(row['status'])
            tally = postcode_outcomes.setdefault(country, Counter())
            tally[reason] += 1
    manifest = {'schema_version': OUTCOMES_VERSION, 'run_id': run_id, 'rows': len(outcomes), 'counts_by_status': dict(sorted(counts.items())), 'ready_supports': len(supports), 'pending_refused': counts['pending'], 'routing_immutable': True, 'access_classification': 'restricted', 'public_export_allowed': False}
    manifest['postcode_construction_counts'] = dict(Counter(support.get('postcode_construction_method') for support in supports if support['support_method'] == 'nominatim_derived_postcode_support'))
    manifest['postcode_outcomes_by_country'] = {country: dict(sorted(tally.items())) for country, tally in sorted(postcode_outcomes.items())}
    return (outcomes, supports, manifest)

POSTCODE_SIZE_FIELDS = ('observation_id', 'country_code', 'postcode', 'status', 'reason',
                        'construction_method', 'allocated_area_km2', 'extent_status', 'area_cap_km2')

# Retain area estimates, construction methods and exclusion reasons for later analysis
def postcode_size_rows(requests, outcomes, supports):
    """Report allocated postcode area, construction method and extent status.

    Represent unavailable areas with blank values
    """
    by_outcome = {row['observation_id']: row for row in outcomes}
    by_support = {row['observation_id']: row for row in supports}
    result = []
    for request in requests:
        if request['request_kind'] != 'postcode_seed_cell':
            continue
        oid = request['observation_id']
        outcome, support = by_outcome[oid], by_support.get(oid)
        result.append(dict(observation_id=oid, country_code=request['country_code'],
            postcode=request['postcode'], status=outcome['status'], reason=outcome['reason'],
            construction_method=support.get('postcode_construction_method', '') if support else '',
            allocated_area_km2=support['support_area_m2']/1000000 if support else '',
            extent_status=support.get('postcode_extent_status', 'mapped_polygon' if support.get('postcode_construction_method') == 'qualified_osm_postcode_polygon' else 'unspecified_extent') if support else 'unavailable',
            area_cap_km2=int(request['max_cells'])/100))
    return result

def _file_identity(path, *, include_path=True):
    stat = path.stat()
    result = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    if include_path:
        result['path'] = str(path.resolve())
    return result


def _stage_identity(args):
    """Record the input files and settings for a geographic checkpoint"""
    return {key: _file_identity(value) if isinstance(value, Path) else value
            for key, value in vars(args).items() if key not in {'resume', 'output_dir'}}


def _seal_completed(directory, invocation):
    files = {str(p.relative_to(directory)): _file_identity(p, include_path=False)
             for p in directory.rglob('*') if p.is_file()}
    external = {}
    ledger = directory / 'response_ledger.csv'
    if ledger.is_file():
        with ledger.open(newline='') as handle:
            for row in csv.DictReader(handle):
                path = Path(row['response_path'])
                if path.is_file() and directory not in path.parents:
                    external[str(path)] = _file_identity(path, include_path=False)
    atomic_write(directory / 'stage_complete.json', _json_bytes({'invocation': invocation, 'files': files, 'external_response_bodies': external}))


def _verify_completed(directory, invocation):
    marker = directory / 'stage_complete.json'
    if not marker.is_file():
        raise ValueError('stage has no completed checkpoint; preserve it and use a new output directory')
    saved = json.loads(marker.read_text())
    if saved['invocation'] != invocation:
        raise ValueError('completed stage inputs or options changed')
    actual = {str(p.relative_to(directory)): _file_identity(p, include_path=False)
              for p in directory.rglob('*') if p.is_file() and p != marker}
    if any(not Path(path).is_file() or _file_identity(Path(path), include_path=False) != identity
           for path, identity in saved.get('external_response_bodies', {}).items()):
        raise ValueError('completed stage cached response bodies changed or are missing')
    if actual != saved['files']:
        raise ValueError('completed stage artifacts changed or are missing')


def main(argv: Sequence[str] | None=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare_parser = sub.add_parser('prepare')
    for flag in ('runner', 'routing', 'support-seed-manifest', 'final-evidence', 'grid-contract', 'policy', 'output-dir'):
        prepare_parser.add_argument(f'--{flag}', required=True, type=Path)
    acquire_parser = sub.add_parser('acquire')
    for flag in ('requests', 'request-manifest', 'service-identity', 'output-dir'):
        acquire_parser.add_argument(f'--{flag}', required=True, type=Path)
    finalize_parser = sub.add_parser('finalize')
    for flag in ('requests', 'request-manifest', 'runner', 'routing', 'final-evidence', 'responses', 'response-manifest', 'service-identity', 'cross-stage-release', 'grid-contract', 'output-dir'):
        finalize_parser.add_argument(f'--{flag}', required=True, type=Path)
    for command in (prepare_parser, acquire_parser, finalize_parser):
        command.add_argument('--resume', action='store_true', help='Reuse a verified completed stage; interrupted acquisition reuses its response cache')
    args = parser.parse_args(argv)
    invocation = _stage_identity(args)
    if args.output_dir.exists():
        if args.resume:
            _verify_completed(args.output_dir, invocation)
            return 0
        raise FileExistsError(args.output_dir)
    args.output_dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f'.{args.output_dir.name}.', dir=args.output_dir.parent))
    try:
        if args.mode == 'prepare':
            rows, manifest = prepare(runner_path=args.runner, routing_path=args.routing, support_seed_manifest_path=args.support_seed_manifest, final_evidence_manifest_path=args.final_evidence, grid_contract_path=args.grid_contract, policy=json.loads(args.policy.read_text()))
            request_path = work / 'support_requests.csv'
            _write_csv(request_path, REQUEST_FIELDS, rows)
            manifest['artifacts'] = {'support_requests.csv': {'path': str((args.output_dir / 'support_requests.csv').resolve()), 'rows': len(rows), 'fields': list(REQUEST_FIELDS)}}
            atomic_write(work / 'stage_a_request_manifest.json', _json_bytes(manifest))
        elif args.mode == 'acquire':
            request_manifest = json.loads(args.request_manifest.read_text())
            request_artifact = request_manifest.get('artifacts', {}).get('support_requests.csv')
            if request_manifest.get('schema_version') != SCHEMA_VERSION or not isinstance(request_artifact, Mapping) or request_artifact.get('fields') != list(REQUEST_FIELDS):
                raise ValueError('request manifest does not bind exact Stage-A requests')
            service = json.loads(args.service_identity.read_text())
            rows = _read_csv(args.requests, REQUEST_FIELDS)
            ledger, manifest = acquire(rows, service, raw_dir=work / 'raw')
            for row in ledger:
                source_path = Path(str(row['response_path']))
                if work in source_path.parents:
                    row['response_path'] = str((args.output_dir / 'raw' / source_path.name).resolve())
                row['response_id'] = row['response_path']
            if 'cache_endpoint' not in service:
                manifest['cache_endpoint'] = str((args.output_dir / 'raw').resolve())
            response_path = work / 'response_ledger.csv'
            _write_csv(response_path, RESPONSE_FIELDS, ledger)
            manifest['artifacts'] = {'response_ledger.csv': {'path': str((args.output_dir / 'response_ledger.csv').resolve()), 'rows': len(ledger), 'fields': list(RESPONSE_FIELDS)}}
            atomic_write(work / 'response_ledger_manifest.json', _json_bytes(manifest))
        else:
            _grid_contract(args.grid_contract)
            request_manifest = json.loads(args.request_manifest.read_text())
            response_manifest = json.loads(args.response_manifest.read_text())
            service = json.loads(args.service_identity.read_text())
            cross = json.loads(args.cross_stage_release.read_text())
            request_artifact = request_manifest.get('artifacts', {}).get('support_requests.csv')
            response_artifact = response_manifest.get('artifacts', {}).get('response_ledger.csv')
            if request_manifest.get('schema_version') != SCHEMA_VERSION or not isinstance(request_artifact, Mapping) or response_manifest.get('schema_version') != 'imputation_stage_a_response_ledger_v1' or (not isinstance(response_artifact, Mapping)) or (cross.get('schema_version') not in {'imputation_v3_cross_stage_release_v1', 'patent_geographic_handoff_v1'}) or (cross.get('run_id') != request_manifest.get('run_id')):
                raise ValueError('finalize inputs refer to different runs or unsupported file formats')
            requests = _read_csv(args.requests, REQUEST_FIELDS)
            response_rows = _read_csv(args.responses, RESPONSE_FIELDS)
            outcomes, supports, manifest = finalize(requests, _read_csv(args.routing, ROUTING_FIELDS), response_rows, service_identity=service, run_id=str(cross['run_id']))
            size_rows = postcode_size_rows(requests, outcomes, supports)
            _write_csv(work / 'postcode_sizes.csv', POSTCODE_SIZE_FIELDS, size_rows)
            outcomes_path = work / 'support_outcomes.csv'
            _write_csv(outcomes_path, OUTCOME_FIELDS, outcomes)
            support_dir = work / 'supports'
            support_dir.mkdir()
            source_artifacts: list[dict[str, object]] = []
            support_artifacts: dict[str, dict[str, object]] = {}
            for support in supports:
                request_id = str(support['source']['request_id'])
                method = str(support['support_method'])
                if method == 'observed_coordinate_passthrough':
                    source_path = support_dir / f'observed-{request_id}.json'
                    atomic_write(source_path, _json_bytes(support['source']))
                    source_entries = [(f'observed_support_{request_id}', source_path)]
                elif method == 'actual_street_geometry_100m_support':
                    lookup = [row for row in response_rows if row['request_id'] == request_id and row['request_kind'] == 'lookup']
                    if len(lookup) != 1:
                        raise ValueError('ready street support lacks one geometry response')
                    source_entries = [(f'street_support_{request_id}', Path(lookup[0]['response_path']))]
                else:
                    query_path = support_dir / f'postcode-query-ledger-{request_id}.json'
                    accepted_path = support_dir / f'postcode-accepted-{request_id}.json'
                    request_responses = [row for row in response_rows if row['request_id'] == request_id]
                    atomic_write(query_path, _json_bytes(request_responses))
                    atomic_write(accepted_path, _json_bytes(support['accepted_cells']))
                    source_entries = [(f'postcode_query_ledger_{request_id}', query_path), (f'postcode_support_{request_id}', accepted_path)]
                    if support.get('postcode_construction_method') in APPROXIMATION_METHODS:
                        source_entries.append((f'postcode_tl3_boundaries_{request_id}', Path(support['point_provenance']['boundaries']['path'])))
                for role_name, source_path in source_entries:
                    final_source = args.output_dir / source_path.relative_to(work) if work in source_path.parents else source_path
                    source_artifacts.append({'role': role_name, 'path': str(final_source.resolve())})
                support_path = support_dir / f"{support['support_id']}.json"
                atomic_write(support_path, _json_bytes(support))
                support_artifacts[f'stage_a_support_{request_id}'] = {'path': str((args.output_dir / support_path.relative_to(work)).resolve())}
            manifest['artifacts'] = {'postcode_sizes.csv': {'path': str((args.output_dir / 'postcode_sizes.csv').resolve()), 'rows': len(size_rows), 'fields': list(POSTCODE_SIZE_FIELDS)}, 'support_outcomes.csv': {'path': str((args.output_dir / 'support_outcomes.csv').resolve()), 'rows': len(outcomes), 'fields': list(OUTCOME_FIELDS)}, **support_artifacts}
            manifest_path = work / 'stage_a_outcomes_manifest.json'
            atomic_write(manifest_path, _json_bytes(manifest))
            support_inputs = {'schema_version': 'imputation_100m_support_inputs_v1', 'run_id': cross['run_id'], 'stage_a_outcomes_manifest': {'path': str((args.output_dir / 'stage_a_outcomes_manifest.json').resolve())}, 'stage_a_outcomes': {'path': str((args.output_dir / 'support_outcomes.csv').resolve())}, 'supports': supports, 'artifacts': sorted(source_artifacts, key=lambda row: str(row['role'])), 'access_classification': 'restricted', 'public_export_allowed': False}
            atomic_write(work / 'support_inputs.json', _json_bytes(support_inputs))
        _seal_completed(work, invocation)
        os.replace(work, args.output_dir)
        return 0
    except Exception:
        for root, directories, files in os.walk(work, topdown=False):
            for name in files:
                Path(root, name).unlink()
            for name in directories:
                Path(root, name).rmdir()
        work.rmdir()
        raise
if __name__ == '__main__':
    raise SystemExit(main())
