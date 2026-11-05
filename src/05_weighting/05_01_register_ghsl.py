"""Register GHSL built-surface rasters and their grid and epochs"""
from __future__ import annotations
import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from geographic_uncertainty.routing import atomic_write
SCHEMA_VERSION = 'gpd_ghsl_built_s_100m_registry_v1'
SOURCE_SCHEMA_VERSION = 'gpd_ghsl_built_s_100m_source_manifest_v1'
EPOCHS = tuple(range(1975, 2035, 5))
BASE_URL = 'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_BUILT_S_GLOBE_R2023A'

def stem(epoch: int) -> str:
    return f'GHS_BUILT_S_E{epoch}_GLOBE_R2023A_54009_100_V1_0'

def official_url(epoch: int) -> str:
    return f'{BASE_URL}/GHS_BUILT_S_E{epoch}_GLOBE_R2023A_54009_100/V1-0/{stem(epoch)}.zip'

def official_tile_stem(epoch: int, row: int, column: int) -> str:
    if not isinstance(row, int) or isinstance(row, bool) or (not 1 <= row <= 18):
        raise ValueError('official GHSL tile row is invalid')
    if not isinstance(column, int) or isinstance(column, bool) or (not 1 <= column <= 36):
        raise ValueError('official GHSL tile column is invalid')
    return f'{stem(epoch)}_R{row}_C{column}'

def official_tile_url(epoch: int, row: int, column: int) -> str:
    return f'{BASE_URL}/GHS_BUILT_S_E{epoch}_GLOBE_R2023A_54009_100/V1-0/tiles/{official_tile_stem(epoch, row, column)}.zip'

def raster_coverage(metadata: Mapping[str, object]) -> dict[str, int]:
    """Map aligned raster cells to global grid indices"""
    if str(metadata.get('crs', '')).upper() != 'ESRI:54009' or metadata.get('count') != 1 or metadata.get('dtype') != 'uint16' or (metadata.get('nodata') != 65535.0):
        raise ValueError('GHSL tile encoding or CRS is not canonical')
    transform = metadata.get('transform')
    if not isinstance(transform, list) or len(transform) != 6 or [transform[i] for i in (0, 1, 3, 4)] != [100.0, 0.0, 0.0, -100.0]:
        raise ValueError('GHSL tile must retain the original unrotated 100 m pixels')
    column = (transform[2] + 18041000) / 100
    row = (9000000 - transform[5]) / 100
    width, height = (metadata.get('width'), metadata.get('height'))
    if not isinstance(width, int) or isinstance(width, bool) or (not isinstance(height, int)) or isinstance(height, bool) or (min(width, height) <= 0) or (row != int(row)) or (column != int(column)) or (row < 0) or (column < 0) or (row + height > 180000) or (column + width > 360820):
        raise ValueError('GHSL tile is off-grid or outside the canonical global grid')
    if metadata.get('bounds') != [transform[2], transform[5] - height * 100, transform[2] + width * 100, transform[5]]:
        raise ValueError('GHSL tile bounds differ from its pixel transform')
    return dict(row_offset=int(row), column_offset=int(column), height=height, width=width)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()

def _read_contract(path: Path) -> dict[str, object]:
    contract = json.loads(path.read_text())
    if contract.get('schemaVersion') != 'gpd-ghsl-grid-contract-v1' or contract.get('gridVersion') != 'gpd-ghsl-mollweide-100m-1km-v2' or contract.get('ghslRegistryVersion') != 'GHS-BUILT-S-R2023A' or (contract.get('crs') != 'ESRI:54009') or (contract.get('originX') != -18041000) or (contract.get('originY') != 9000000) or (contract.get('extent') != [-18041000, -9000000, 18041000, 9000000]) or (contract.get('resolution100m') != 100) or (contract.get('resolution1km') != 1000):
        raise ValueError('canonical GHSL grid contract content mismatch')
    return contract

def inspect_raster(path: Path) -> dict[str, object]:
    try:
        import rasterio
    except ImportError as exc:
        raise RuntimeError('install patent-code[raster] to inspect GHSL assets') from exc
    with rasterio.open(path) as dataset:
        bounds = dataset.bounds
        transform = dataset.transform
        crs = dataset.crs.to_string() if dataset.crs else ''
        return {'crs': crs, 'width': dataset.width, 'height': dataset.height, 'count': dataset.count, 'transform': [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f], 'bounds': [bounds.left, bounds.bottom, bounds.right, bounds.top], 'dtype': dataset.dtypes[0] if dataset.dtypes else '', 'nodata': dataset.nodata}

def _validate_raster(metadata: Mapping[str, object]) -> None:
    if str(metadata.get('crs', '')).upper() != 'ESRI:54009':
        raise ValueError('GHSL raster CRS is not ESRI:54009')
    if metadata.get('width') != 360820 or metadata.get('height') != 180000:
        raise ValueError('GHSL raster dimensions are not the canonical global 100 m grid')
    if metadata.get('count') != 1:
        raise ValueError('GHSL raster must have exactly one band')
    if metadata.get('dtype') != 'uint16' or metadata.get('nodata') != 65535.0:
        raise ValueError('GHSL raster encoding is not canonical uint16 with nodata 65535')
    if metadata.get('transform') != [100.0, 0.0, -18041000.0, 0.0, -100.0, 9000000.0]:
        raise ValueError('GHSL raster transform is not the canonical 100 m grid')
    if metadata.get('bounds') != [-18041000.0, -9000000.0, 18041000.0, 9000000.0]:
        raise ValueError('GHSL raster extent is not canonical')

# Register official built-surface assets and check their grid alignment and available epochs
def build_registry(*, source_manifest_path: Path, grid_contract_path: Path, output_path: Path) -> dict[str, object]:
    contract = _read_contract(grid_contract_path.resolve(strict=True))
    source_path = source_manifest_path.resolve(strict=True)
    source = json.loads(source_path.read_text())
    if source.get('schema_version', SOURCE_SCHEMA_VERSION) != SOURCE_SCHEMA_VERSION or source.get('product', 'GHS-BUILT-S') != 'GHS-BUILT-S' or source.get('release', 'R2023A') != 'R2023A' or (source.get('resolution_m', 100) != 100) or (source.get('crs', 'ESRI:54009') != 'ESRI:54009'):
        raise ValueError('GHSL source manifest identity is invalid')
    assets = source.get('assets')
    scope = source.get('registry_scope', 'full_1975_2030')
    expected_epochs = {
        'full_1975_2030': EPOCHS,
        'through_2025': EPOCHS[:-1],
    }.get(scope, ())
    if not expected_epochs:
        raise ValueError('GHSL registry scope is invalid')
    if not isinstance(assets, list) or len(assets) != len(expected_epochs):
        raise ValueError('GHSL source manifest does not match its declared epoch scope')
    by_epoch: dict[int, Mapping[str, object]] = {}
    for item in assets:
        if not isinstance(item, Mapping) or not isinstance(item.get('epoch'), int):
            raise ValueError('GHSL asset entry is invalid')
        epoch = int(item['epoch'])
        if epoch in by_epoch:
            raise ValueError('GHSL epochs must be unique')
        by_epoch[epoch] = item
    if tuple(sorted(by_epoch)) != expected_epochs:
        raise ValueError('GHSL assets do not match the declared epoch scope')
    sealed_assets: list[dict[str, object]] = []
    for epoch in expected_epochs:
        item = by_epoch[epoch]
        layout = item.get('storage_layout', 'global')
        if layout not in {'global', 'official_tile'}:
            raise ValueError('GHSL storage layout is invalid')
        expected_stem = official_tile_stem(epoch, item.get('tile_row'), item.get('tile_column')) if layout == 'official_tile' else stem(epoch)
        expected_url = official_tile_url(epoch, item['tile_row'], item['tile_column']) if layout == 'official_tile' else official_url(epoch)
        if item.get('archive_filename', f'{expected_stem}.zip') != f'{expected_stem}.zip' or item.get('url', expected_url) != expected_url:
            raise ValueError(f'GHSL official archive identity mismatch for epoch {epoch}')
        archive_path = Path(str(item.get('archive_path', ''))).resolve(strict=True)
        archive_bytes = archive_path.stat().st_size
        if archive_path.name != f'{expected_stem}.zip' or not archive_path.is_absolute() or (not archive_path.is_file()) or (archive_bytes <= 0) or (item.get('archive_bytes', archive_bytes) != archive_bytes):
            raise ValueError(f'GHSL archive byte identity mismatch for epoch {epoch}')
        raster_path = Path(str(item.get('raster_path', ''))).resolve(strict=True)
        expected_bytes = raster_path.stat().st_size
        if raster_path.name != f'{expected_stem}.tif' or not raster_path.is_absolute() or (not raster_path.is_file()) or (expected_bytes <= 0) or (item.get('raster_bytes', expected_bytes) != expected_bytes):
            raise ValueError(f'GHSL raster byte identity mismatch for epoch {epoch}')
        metadata = inspect_raster(raster_path)
        if layout == 'official_tile':
            coverage = raster_coverage(metadata)
        else:
            _validate_raster(metadata)
        sealed_assets.append({'epoch': epoch, 'artifact_role': f'ghsl_built_s_100m_{epoch}', 'path': str(raster_path), 'archive_filename': expected_stem + '.zip', 'url': expected_url, 'raster_metadata': metadata, 'archive_path': str(archive_path)})
        if layout == 'official_tile':
            sealed_assets[-1].update(storage_layout=layout, tile_row=item['tile_row'], tile_column=item['tile_column'], coverage=coverage)
    grid_identity = {'ghsl_registry_version': contract['ghslRegistryVersion'], 'crs': contract['crs'], 'origin_x': contract['originX'], 'origin_y': contract['originY'], 'extent': contract['extent'], 'cell_indexing': contract['cellIndexing'], 'resolution_100m': contract['resolution100m'], 'resolution_1km': contract['resolution1km'], 'parent_mapping': contract['parentMapping']}
    registry = {'schema_version': SCHEMA_VERSION, 'product': 'GHS-BUILT-S', 'release': 'R2023A', 'measure': 'built_surface_m2', 'registry_scope': scope, 'published_epochs': list(EPOCHS), 'available_epochs': list(expected_epochs), 'complete_temporal_registry': scope == 'full_1975_2030', 'epoch_unavailable_policy': 'select from published epochs by latest epoch no later than patent year; withhold unless that selected epoch is available', 'grid_contract_path': str(grid_contract_path.resolve()), 'source_manifest_path': str(source_path), 'grid_identity': grid_identity, 'assets': sealed_assets}
    if output_path.exists():
        raise FileExistsError(f'refusing to overwrite {output_path}')
    atomic_write(output_path, _canonical_json(registry))
    return registry

def main(argv: Sequence[str] | None=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest', required=True, type=Path)
    parser.add_argument('--grid-contract', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    build_registry(source_manifest_path=args.source_manifest, grid_contract_path=args.grid_contract, output_path=args.output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
