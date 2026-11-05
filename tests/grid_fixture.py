"""Synthetic address, street and postcode allocations for integration tests"""
import csv, json
from copy import deepcopy
from pathlib import Path
from weighting.allocate_to_grid import OFFICIAL_GHSL_EPOCHS, OUTCOME_FIELDS
from geographic_uncertainty.routing import ROUTING_FIELDS
from geographic_uncertainty.contracts import COUNTRY_FIELDS, TEMPORAL_FIELDS
GRID_CONTRACT = Path(__file__).resolve().parents[1] / 'config/ghsl_grid.json'

def _grid():
    return {'ghsl_registry_version': 'GHS-BUILT-S-R2023A', 'crs': 'ESRI:54009', 'origin_x': -18041000, 'origin_y': 9000000, 'extent': [-18041000, -9000000, 18041000, 9000000], 'cell_indexing': 'zero_based_row_column', 'resolution_100m': 100, 'resolution_1km': 1000, 'parent_mapping': 'parent_row=floor(row_100m/10);parent_col=floor(col_100m/10)'}

def _fixture(tmp_path):
    routes = [{'observation_id': 'o-address', 'route': 'address_passthrough', 'status': 'accepted'}, {'observation_id': 'o-street', 'route': 'street_support_request', 'status': 'accepted'}, {'observation_id': 'o-postcode', 'route': 'postcode_support_request', 'status': 'accepted'}, {'observation_id': 'o-locality', 'route': 'withheld', 'status': 'withheld', 'withholding_reason': 'locality_not_supported'}]
    countries = [{'observation_id': 'o-address', 'ctry_code': 'GB', 'country_group': 'GB'}, {'observation_id': 'o-street', 'ctry_code': 'US', 'country_group': 'US'}, {'observation_id': 'o-postcode', 'ctry_code': 'FR', 'country_group': 'FR'}, {'observation_id': 'o-locality', 'ctry_code': 'JP', 'country_group': 'JP'}]
    temporal = []
    for index, (oid, year, status) in enumerate([('o-address', '2021', 'valid'), ('o-street', '2018', 'valid'), ('o-postcode', '2014', 'valid'), ('o-postcode', '2019', 'valid'), ('o-locality', '2020', 'valid'), ('o-street', '', 'missing')]):
        temporal.append({'family_id': f'f{index}', 'location_evidence_id': f'l{index}', 'observation_id': oid, 'appln_id': f'a{index}', 'person_id': f'p{index}', 'actor': 'inventor', 'patent_year': year, 'patent_year_status': status})
    grid = _grid()
    artifact_entries = []

    def artifact(role, name, content):
        path = tmp_path / name
        path.write_text(content)
        artifact_entries.append({'role': role, 'path': str(path)})
        return name
    epochs = {}
    for epoch in (2010, 2015, 2020):
        role = f'ghsl_raster_{epoch}'
        artifact(role, f'raster-{epoch}.tif', f'fixture-raster-{epoch}')
        epochs[str(epoch)] = {'artifact_role': role}
    registry_grid = dict(grid)
    artifact('ghsl_registry', 'registry.json', json.dumps({'product': 'GHS-BUILT-S', 'release': 'R2023A', 'measure': 'built_surface_m2', 'grid_identity': registry_grid, 'published_epochs': list(OFFICIAL_GHSL_EPOCHS), 'available_epochs': [2010, 2015, 2020], 'complete_temporal_registry': False}, sort_keys=True))
    for asset in epochs.values():
        asset['grid_identity'] = grid
    supports = [{'observation_id': 'o-address', 'support_id': 's-address', 'support_method': 'observed_coordinate_passthrough', 'closed': True, 'support_cell_count': 1, 'support_area_m2': 10000, 'grid_identity': grid, 'source_artifacts': {'observed_coordinate': 'observed_coordinate_o_address'}, 'accepted_cells': [{'cell_id_100m': 'r101c101', 'parent_cell_id_1km': 'r10c10'}]}, {'observation_id': 'o-street', 'support_id': 's-street', 'support_method': 'actual_street_geometry_100m_support', 'closed': True, 'support_cell_count': 2, 'support_area_m2': 20000, 'street_length_m': 150.0, 'grid_identity': grid, 'source_artifacts': {'street_geometry': 'street_geometry_o_street'}, 'geometry_type': 'LineString', 'accepted_cells': [{'cell_id_100m': 'r201c201', 'parent_cell_id_1km': 'r20c20'}, {'cell_id_100m': 'r201c202', 'parent_cell_id_1km': 'r20c20'}]}, {'observation_id': 'o-postcode', 'support_id': 's-postcode', 'support_method': 'nominatim_derived_postcode_support', 'closed': True, 'support_cell_count': 3, 'support_area_m2': 30000, 'support_authority_label': 'non_authoritative_postcode_support', 'grid_identity': grid, 'source_artifacts': {'query_ledger': 'postcode_query_ledger_o_postcode', 'accepted_support': 'postcode_accepted_support_o_postcode'}, 'bounded_flood_fill': False, 'postcode_construction_method': 'qualified_osm_postcode_polygon', 'accepted_cells': [{'cell_id_100m': 'r90001c180411', 'parent_cell_id_1km': 'r9000c18041'}, {'cell_id_100m': 'r90009c180419', 'parent_cell_id_1km': 'r9000c18041'}, {'cell_id_100m': 'r90010c180420', 'parent_cell_id_1km': 'r9001c18042'}]}]
    artifact('observed_coordinate_o_address', 'observed.json', 'observed-coordinate')
    artifact('street_geometry_o_street', 'street.geojson', 'actual-linestring')
    # Three small polygons establish the exact saved postcode cells independently
    from pyproj import Transformer
    from geographic_uncertainty.retrieve_and_rasterise import cell_centroid
    inverse = Transformer.from_crs('ESRI:54009', 'EPSG:4326', always_xy=True)
    polygons = []
    for row, column in ((90001,180411),(90009,180419),(90010,180420)):
        x, y = -18041000 + column*100, 9000000 - row*100
        ring = [inverse.transform(px,py) for px,py in ((x+10,y-10),(x+90,y-10),(x+90,y-90),(x+10,y-90),(x+10,y-10))]
        polygons.append([ring])
    request = dict(observation_id='o-postcode',country_code='FR',postcode='75001',
                   seed_longitude=cell_centroid(90001,180411)[0],seed_latitude=cell_centroid(90001,180411)[1],max_cells=5000)
    candidate = dict(type='postcode',osm_type='relation',osm_id=1,address=dict(country_code='fr',postcode='75001'),
                     geojson=dict(type='MultiPolygon',coordinates=polygons))
    response = tmp_path/'postcode-response.json';response.write_text(json.dumps([candidate]))
    ledger = [dict(request_kind='search',cell_id_100m='',subrequest_id='postcode-search',attempt=1,error_class='',response_path=str(response))]
    supports[-1].update(source=dict(boundary_request=request),boundary_provenance=dict(osm_type='relation',osm_id='1'))
    artifact('postcode_query_ledger_o_postcode', 'postcode-ledger.json', json.dumps(ledger))
    artifact('postcode_accepted_support_o_postcode', 'postcode-support.json', json.dumps(supports[-1]['accepted_cells']))
    values = {'schema_version': 'ghsl_100m_values_v1', 'ghsl_source': {'product': 'GHS-BUILT-S', 'release': 'R2023A', 'measure': 'built_surface_m2'}, 'grid_identity': grid, 'values_by_support': {'s-address': {'r101c101': {'2020': '0'}}, 's-street': {'r201c201': {'2015': '3'}, 'r201c202': {'2015': '1'}}, 's-postcode': {'r90001c180411': {'2010': '2', '2015': '1'}, 'r90009c180419': {'2010': '3', '2015': '1'}, 'r90010c180420': {'2010': '5', '2015': '8'}}}, 'support_failures_by_epoch': {}}
    artifact('ghsl_values', 'values.json', json.dumps(values, sort_keys=True))
    outcome_rows = [['o-address', 'v-address', 'address_passthrough', 'support_ready', 'observed_coordinate_passthrough', 's-address', '', '[]', 'observed_coordinate_o_address'], ['o-street', 'v-street', 'street_support_request', 'support_ready', 'actual_street_geometry_100m_support', 's-street', '', '[]', 'street_geometry_o_street'], ['o-postcode', 'v-postcode', 'postcode_support_request', 'support_ready', 'nominatim_derived_postcode_support', 's-postcode', '', '[]', 'postcode_accepted_support_o_postcode'], ['o-locality', 'v-locality', 'withheld', 'withheld', '', '', 'locality_not_supported', '[]', '']]
    outcome_content = ','.join(OUTCOME_FIELDS) + '\n' + ''.join((','.join(row) + '\n' for row in outcome_rows))
    artifact('stage_a_outcomes', 'stage-a-outcomes.csv', outcome_content)
    outcome_entry = next((item for item in artifact_entries if item['role'] == 'stage_a_outcomes'))
    artifact('stage_a_outcomes_manifest', 'stage-a-outcomes-manifest.json', json.dumps({'schema_version': 'imputation_stage_a_outcomes_v1', 'run_id': 'run-fixture', 'artifacts': {'support_outcomes.csv': {}}}, sort_keys=True))
    release = {'schema_version': 'imputation_100m_support_release_v1', 'sealed': True, 'run_id': 'run-fixture', 'grid_contract': {'path': str(GRID_CONTRACT.resolve()), 'schema_version': 'gpd-ghsl-grid-contract-v1', 'version': 'gpd-ghsl-mollweide-100m-1km-v2'}, 'grid_identity': grid, 'ghsl_source': values['ghsl_source'], 'ghsl_built_assets': epochs, 'ghsl_epoch_scope': {'published_epochs': list(OFFICIAL_GHSL_EPOCHS), 'available_epochs': [2010, 2015, 2020], 'selection_rule': 'latest_published_epoch_no_later_than_patent_year_then_require_available'}, 'supports': supports, 'support_failures_by_epoch': {}, 'artifacts': artifact_entries}
    cross = {'schema_version': 'imputation_v3_cross_stage_release_v1', 'run_id': 'run-fixture'}
    return (cross, routes, countries, temporal, release)
