"""Synthetic allocations exercise the complete weighting-to-family-grid join"""
from contextlib import closing
import csv
import json
import sqlite3
from decimal import Decimal
from copy import deepcopy
import unittest
import tempfile
from pathlib import Path
from aggregation.aggregate_grid_activity import build
from weighting.allocate_to_grid import run
from geographic_uncertainty.contracts import COUNTRY_FIELDS, TEMPORAL_FIELDS
from geographic_uncertainty.routing import ROUTING_FIELDS
from grid_fixture import _fixture

def fixture(tmp_path, *, conflicting_year=False, unresolved=False, family_locations=False, family_copies=1, anonymous=False, missing_shares=False, shared_direct=False, postcode_weights=None):
    cross, routing, countries, temporal, support = _fixture(tmp_path)
    for path in tmp_path.glob('*.json'):
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        path.write_text(json.dumps(value))
    if family_locations:
        originals = [temporal[index] for index in (0, 1, 2, 4)]
        if family_locations == 'complete':
            originals = originals[:3]
        temporal = []
        for actor in ('applicant', 'inventor'):
            for original in originals:
                row = deepcopy(original)
                row.update(family_id='docdb:900', appln_id='900', person_id='' if anonymous else 'person-' + row['observation_id'], actor=actor, patent_year='2020', patent_year_status='valid')
                if shared_direct and row['observation_id'] == 'o-postcode':
                    row['observation_id'] = 'o-address'
                temporal.append(row)
        temporal = [{**row, 'family_id': f'docdb:{900 + copy}', 'appln_id': str(900 + copy)} for copy in range(family_copies) for row in temporal]
        values_path = tmp_path / 'values.json'
        values = json.loads(values_path.read_text())
        for cells in values['values_by_support'].values():
            for epochs in cells.values():
                epochs['2020'] = next(iter(epochs.values()))
        values_path.write_text(json.dumps(values))
    if postcode_weights is not None:
        values_path = tmp_path / 'values.json'
        values = json.loads(values_path.read_text())
        for cell, weight in zip(('r90001c180411', 'r90009c180419', 'r90010c180420'), postcode_weights):
            values['values_by_support']['s-postcode'][cell]['2020'] = str(weight)
        values_path.write_text(json.dumps(values))
    duplicate = deepcopy(temporal[0])
    duplicate['person_id'] = 'duplicate-person'
    if conflicting_year:
        duplicate['patent_year'] = '2022'
    temporal.append(duplicate)
    for row in temporal:
        row['location_evidence_id'] = 'fle:' + json.dumps([row[field] for field in ('family_id', 'observation_id', 'person_id', 'actor')], separators=(',', ':'))
    for name, fields, rows in (('routing', ROUTING_FIELDS, routing), ('country_groups', COUNTRY_FIELDS, countries), ('temporal_child', TEMPORAL_FIELDS, temporal)):
        path = tmp_path / (name + '.csv')
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        cross[name] = dict(path=str(path))
    source_path = tmp_path / 'source-links.csv'
    people = {}
    for row in temporal:
        people.setdefault((row['appln_id'], row['actor']), set()).add(row['person_id'] or 'anonymous-' + row['observation_id'])
    source_fields = ['appln_id', 'person_id', 'observation_id', 'actor', 'inv_name', 'app_name', 'inv_share', 'app_share']
    with source_path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=source_fields)
        writer.writeheader()
        for row in temporal:
            name = row['person_id'] or 'anonymous-' + row['observation_id']
            share = '' if missing_shares else str(Decimal(1) / len(people[row['appln_id'], row['actor']]))
            writer.writerow({**{field: row[field] for field in source_fields[:4]}, 'inv_name': name, 'app_name': name, 'inv_share': share, 'app_share': share})
    cross['inputs'] = {'address_links.csv': {'path': str(source_path)}}
    cross['file_input_mode'] = 'plain_paths'
    support['file_input_mode'] = 'plain_paths'
    cross_path = tmp_path / 'cross.json'
    cross_path.write_text(json.dumps(cross))
    support_path = tmp_path / 'support.json'
    support_path.write_text(json.dumps(support))
    allocation = tmp_path / 'allocation'
    run(cross_stage_release_path=cross_path, support_release_path=support_path, output_dir=allocation)
    backbone = tmp_path / 'backbone.sqlite'
    with closing(sqlite3.connect(backbone)) as db, db:
        db.execute('CREATE TABLE regpat_family_backbone(appln_id TEXT PRIMARY KEY, canonical_family_id TEXT, resolution_status TEXT, family_id_kind TEXT, family_resolution_conflict INTEGER)')
        db.executemany('INSERT INTO regpat_family_backbone VALUES (?, ?, ?, ?, ?)', [(r['appln_id'], r['family_id'], 'unresolved' if unresolved and r['observation_id'] == temporal[0]['observation_id'] else 'resolved', 'docdb', 0) for r in {row['appln_id']: row for row in temporal}.values()])
    return (allocation / 'final_grid_manifest.json', backbone)


class GridAggregationTests(unittest.TestCase):

    def test_highest_weight_cell_uses_summed_1km_weight_and_keeps_address_share(self):
        for weights in ((4, 3, 6), (2, 3, 5)):
            with self.subTest(weights=weights), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                allocation, backbone = fixture(root, family_locations='complete', postcode_weights=weights)
                result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
                expected = {f'{role}_{tier}{suffix}.csv'
                            for role in ('inventors', 'applicants')
                            for tier in ('direct', 'direct_street', 'direct_street_pcdb', 'direct_street_pcdb_rg')
                            for suffix in ('', '_highest_weight_cell')}
                self.assertEqual(set(result['output_totals']), expected)
                with (root / 'families/family_highest_weight_cell_grid.csv').open() as handle:
                    rows = list(csv.DictReader(handle))
                postcode = [row for row in rows if row['evidence_type'] == 'postcode']
                self.assertEqual(len(postcode), 2)
                self.assertEqual({row['grid_cell_id'] for row in postcode}, {'r9000c18041'})
                self.assertTrue(all(row['weight'] == row['address_share'] for row in rows))
                for name, total in result['output_totals'].items():
                    if name.endswith('_highest_weight_cell.csv'):
                        self.assertEqual(total, result['output_totals'][name.replace('_highest_weight_cell', '')])

    def test_neighbourhood_label_requires_its_separate_scenario_and_support(self):
        for valid_support, scenario in [(True,'postcode_1km_centre_built_surface_approximation'),
                                         (False,'postcode_1km_centre_built_surface_approximation'),
                                         (True,'ghsl_built_surface_100m_to_1km')]:
            with self.subTest(valid_support=valid_support,scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                allocation,backbone=fixture(root)
                manifest=json.loads(allocation.read_text())
                support_path=Path(manifest['support_release']['path'])
                support=json.loads(support_path.read_text())
                if valid_support:
                    postcode=next(s for s in support['supports'] if s['observation_id']=='o-postcode')
                    postcode.update(postcode_construction_method='postcode_1km_centre_flood_fill',
                        output_variant='postcode_1km_approximation',
                        support_authority_label='approximate_neighbourhood_not_postcode_boundary')
                    support_path.write_text(json.dumps(support))
                path=allocation.parent/'estimated_grid_1km.csv'
                with path.open() as f:
                    reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
                for row in rows:
                    if row['evidence_type']=='postcode':row.update(evidence_type='postcode_neighbourhood',scenario_id=scenario)
                with path.open('w') as f:
                    writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
                if valid_support and scenario=='postcode_1km_centre_built_surface_approximation':
                    result=build(allocation_manifest=allocation,family_backbone=backbone,output_dir=root/'families')
                    self.assertEqual(Decimal(result['allocated_mass']),4)
                    with (root/'families/family_grid.csv').open() as f:
                        self.assertIn('postcode_neighbourhood',{r['evidence_type'] for r in csv.DictReader(f)})
                else:
                    with self.assertRaisesRegex(ValueError,'evidence type'):
                        build(allocation_manifest=allocation,family_backbone=backbone,output_dir=root/'families')

    def test_absent_roles_are_reported_without_inventing_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(result['missing_role_counts'], {'applicant': 6, 'inventor': 0})
            self.assertEqual(result['family_role_units'], 6)
            self.assertEqual(Decimal(result['allocated_mass']), 4)
            with (root / 'families/missing_roles.csv').open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 6)

    def test_highest_weight_cell_compares_saved_weights_without_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations='complete')
            path = allocation.parent / 'estimated_grid_1km.csv'
            with path.open() as handle:
                reader = csv.DictReader(handle)
                fields, rows = reader.fieldnames, list(reader)
            for row in rows:
                if row['evidence_type'] == 'postcode':
                    row['normalized_weight'] = ('0.499999999999999999999999999999' if row['grid_cell_id'] == 'r9000c18041'
                                                else '0.500000000000000000000000000001')
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            with (root / 'families/family_highest_weight_cell_grid.csv').open() as handle:
                rows = [row for row in csv.DictReader(handle) if row['evidence_type'] == 'postcode']
            self.assertEqual({row['grid_cell_id'] for row in rows}, {'r9001c18042'})

    def test_weighting_to_family_activity_counts_duplicates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            allocation, backbone = fixture(Path(directory))
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=Path(directory) / 'families')
            assert result['source_application_links'] == 7
            assert result['family_role_units'] == 6
            assert result['accepted_facts'] == 4
            assert result['withheld_facts'] == 2
            assert Decimal(result['allocated_mass']) == 4
            with (Path(directory) / 'families' / 'family_grid.csv').open() as handle:
                rows = list(csv.DictReader(handle))
            assert 'person_ids' not in rows[0]
            assert {r['evidence_type'] for r in rows} == {'observed', 'street', 'postcode'}

    def test_inconsistent_representative_year_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            allocation, backbone = fixture(Path(directory), conflicting_year=True)
            with unittest.TestCase().assertRaisesRegex(ValueError, 'one representative application and filing year'):
                build(allocation_manifest=allocation, family_backbone=backbone, output_dir=Path(directory) / 'families')

    def test_unresolved_family_is_preserved_and_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            allocation, backbone = fixture(Path(directory), unresolved=True)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=Path(directory) / 'families')
            assert result['accepted_facts'] == 3
            assert result['withheld_facts'] == 3
            assert Decimal(result['allocated_mass']) == 3

    def test_invalid_allocation_mass_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            allocation, backbone = fixture(Path(directory))
            path = allocation.parent / 'estimated_grid_1km.csv'
            with path.open(newline='') as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]['normalized_weight'] = '0.125'
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with unittest.TestCase().assertRaisesRegex(ValueError, 'conserve one unit'):
                build(allocation_manifest=allocation, family_backbone=backbone, output_dir=Path(directory) / 'families')

class FamilyCountingTests(unittest.TestCase):

    def test_roles_have_cumulative_outputs_and_withheld_shares_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations=True)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(result['docdb_families'], 1)
            self.assertEqual(result['family_role_units'], 2)
            self.assertEqual(result['distinct_family_addresses'], 8)
            self.assertEqual(result['partially_allocated_family_roles'], 2)
            self.assertEqual(Decimal(result['allocated_mass']), Decimal('1.55'))
            self.assertEqual(Decimal(result['withheld_mass']), Decimal('0.45'))
            for role in ('applicants', 'inventors'):
                previous = {}
                tiers = [('direct', '0.4'), ('direct_street', '0.6'), ('direct_street_pcdb', '0.8')] if role == 'applicants' else [('direct', '0.25'), ('direct_street', '0.5'), ('direct_street_pcdb', '0.75')]
                for tier, total in tiers:
                    with (root / 'families' / f'{role}_{tier}.csv').open() as handle:
                        rows = list(csv.DictReader(handle))
                    values = {(row['grid_cell_id'], row['patent_year']): Decimal(row['activity']) for row in rows}
                    self.assertEqual(sum(values.values()), Decimal(total))
                    self.assertTrue(all((values.get(key, 0) >= value for key, value in previous.items())))
                    self.assertEqual({row['patent_year'] for row in rows}, {'2020'})
                    previous = values
            with (root / 'families' / 'family_grid.csv').open() as handle:
                cells = list(csv.DictReader(handle))
            self.assertEqual({row['address_share'] for row in cells}, {'0.25', '0.2', '0.4'})
            self.assertTrue(any((row['people_at_address'] == '2' and row['address_share'] == '0.4' for row in cells)))
            self.assertNotIn('person_ids', cells[0])

    def test_three_supported_addresses_total_one_for_each_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations='complete')
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(Decimal(result['allocated_mass']), 2)
            self.assertEqual(Decimal(result['withheld_mass']), 0)
            for role in ('applicants', 'inventors'):
                self.assertEqual(Decimal(result['output_totals'][f'{role}_direct_street_pcdb.csv']), 1)

    def test_later_applications_are_rejected_at_aggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root)
            cross = json.loads((root / 'cross.json').read_text())
            temporal_path = Path(cross['temporal_child']['path'])
            with temporal_path.open() as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows = list(reader)
            rows[-1]['appln_id'] = 'later-application'
            cross.pop('inputs', None)
            (root / 'cross.json').write_text(json.dumps(cross))
            with temporal_path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            for name in ('estimated_grid_1km.csv', 'withheld.csv'):
                path = allocation.parent / name
                with path.open() as handle:
                    reader = csv.DictReader(handle)
                    columns = reader.fieldnames
                    allocated_rows = list(reader)
                for row in allocated_rows:
                    if row['person_id'] == 'duplicate-person':
                        row['appln_id'] = 'later-application'
                with path.open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(allocated_rows)
            with closing(sqlite3.connect(backbone)) as db, db:
                db.execute("INSERT INTO regpat_family_backbone SELECT 'later-application',canonical_family_id,resolution_status,family_id_kind,family_resolution_conflict FROM regpat_family_backbone WHERE appln_id=?", (rows[0]['appln_id'],))
            with self.assertRaisesRegex(ValueError, 'one representative application'):
                build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')

    def test_many_families_with_thirds_conserve_mass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations='complete', family_copies=100)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(result['docdb_families'], 100)
            self.assertEqual(result['family_role_units'], 200)
            self.assertEqual(Decimal(result['allocated_mass']), 200)
            self.assertEqual(Decimal(result['withheld_mass']), 0)
            for role in ('applicants', 'inventors'):
                self.assertEqual(Decimal(result['output_totals'][f'{role}_direct_street_pcdb.csv']), 100)

    def test_source_names_determine_weights_without_source_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations=True, anonymous=True)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(Decimal(result['output_totals']['applicants_direct.csv']), Decimal('0.4'))
            self.assertEqual(Decimal(result['output_totals']['inventors_direct.csv']), Decimal('0.25'))
            self.assertEqual(Decimal(result['allocated_mass']) + Decimal(result['withheld_mass']), 2)
            source_path = root / 'source-links.csv'
            with source_path.open() as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows = list(reader)
            for row in rows:
                if not row['person_id']:
                    row['inv_name'] = row['app_name'] = ''
            with source_path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            invalid = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'invalid_fractions')
            self.assertEqual(Decimal(invalid['allocated_mass']), 0)
            self.assertEqual(Decimal(invalid['withheld_mass']), 2)

    def test_names_count_without_ids_or_source_share_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations=True, anonymous=True, missing_shares=True)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(Decimal(result['allocated_mass']), Decimal('1.55'))
            self.assertEqual(Decimal(result['withheld_mass']), Decimal('0.45'))
            with (root / 'families' / 'withheld_families.csv').open() as handle:
                self.assertEqual({row['reason'] for row in csv.DictReader(handle)}, {'locality_not_supported'})

    def test_two_of_three_inventors_at_one_direct_address_receive_two_thirds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation, backbone = fixture(root, family_locations='complete', shared_direct=True)
            result = build(allocation_manifest=allocation, family_backbone=backbone, output_dir=root / 'families')
            self.assertEqual(Decimal(result['output_totals']['inventors_direct.csv']), Decimal(2) / 3)
            self.assertEqual(Decimal(result['output_totals']['inventors_direct_street.csv']), 1)
            self.assertEqual(Decimal(result['output_totals']['inventors_direct_street_pcdb.csv']), 1)
            with (root / 'families' / 'family_grid.csv').open() as handle:
                direct = [row for row in csv.DictReader(handle) if row['actor'] == 'inventor' and row['evidence_type'] == 'observed']
            self.assertEqual({row['people_at_address'] for row in direct}, {'2'})
            self.assertEqual({row['total_people'] for row in direct}, {'3'})
