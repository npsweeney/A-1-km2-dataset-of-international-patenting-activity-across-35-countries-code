"""Save selected DOCDB families' inputs and stage outputs as JSON"""
import argparse
import csv
import json
from pathlib import Path


def csv_rows(path):
    if path.is_file():
        with path.open(newline='', encoding='utf-8-sig') as handle:
            yield from csv.DictReader(handle)


def json_rows(path):
    if path.is_file():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                yield json.loads(line)


# Collect the saved source, query and selection records for each requested patent
def export(run, families):
    run = Path(run)
    source = run / '01_addresses.links.csv'
    if not source.is_file():
        raise FileNotFoundError(source)
    families = list(dict.fromkeys(families))
    wanted = set(families)
    inputs = [row for row in csv_rows(source) if row['family_id'] in wanted]
    absent = wanted - {row['family_id'] for row in inputs}
    if absent:
        raise ValueError('Families absent from intake: ' + ', '.join(sorted(absent)))
    observations = {row['observation_id'] for row in inputs}
    addresses = {oid: dict(observation_id=oid, queries=[], returned_candidates=[],
                           support_responses=[], spatial_weights=[]) for oid in observations}
    for filename, key in [('02_cleaned.csv', 'cleaned'),
                          ('03_geocoding/decisions.csv', 'geocoding')]:
        for row in csv_rows(run / filename):
            if row['observation_id'] in addresses:
                addresses[row['observation_id']][key] = row
    for row in json_rows(run / '03_geocoding/selections.jsonl'):
        if row['observation_id'] in addresses:
            addresses[row['observation_id']]['selection'] = row
    for row in json_rows(run / '03_geocoding/queries.jsonl'):
        if row['observation_id'] in addresses:
            addresses[row['observation_id']]['queries'].append(row)
    handoff = run / '04_inputs/handoff.json'
    manifests = json.loads(handoff.read_text()).get('batch_manifests') if handoff.is_file() else None
    geographic_roots = []
    for entry in manifests or []:
        path = Path(entry)
        path = path if path.is_absolute() else handoff.parent / path
        geographic_roots.append(path.parent.parent)
    if not geographic_roots:
        geographic_roots = [run]
    for root in geographic_roots:
        for filename, key in [('04_requests/support_requests.csv', 'request'),
                              ('04_support/support_outcomes.csv', 'support_outcome')]:
            for row in csv_rows(root / filename):
                if row['observation_id'] in addresses:
                    addresses[row['observation_id']][key] = row
        values_path = root / '05_sampled/ghsl_values.json'
        values = json.loads(values_path.read_text()).get('values_by_support', {}) if values_path.is_file() else {}
        for row in csv_rows(root / '04_support/support_outcomes.csv'):
            address = addresses.get(row['observation_id'])
            sid = row.get('support_id')
            if address is not None and sid:
                address['support'] = json.loads((root / '04_support/supports' / (sid + '.json')).read_text())
                address['ghsl_values'] = values.get(sid, {})
        for row in csv_rows(root / '04_responses/response_ledger.csv'):
            if row['observation_id'] in addresses:
                path = Path(row['response_path']) if row.get('response_path') else None
                if path is not None and not path.is_absolute():
                    path = root / '04_responses' / path
                response = json.loads(path.read_text()) if path is not None and path.is_file() else None
                addresses[row['observation_id']]['support_responses'].append(dict(
                    cell_id_100m=row['cell_id_100m'], http_status=row['http_status'],
                    error_class=row['error_class'], response=response))
    for row in json_rows(run / '03_geocoding/candidates.jsonl'):
        address = addresses.get(row['observation_id'])
        if address is not None:
            selected = set(address.get('selection', {}).get('selected_candidate_ids', []))
            seed = address.get('request', {}).get('seed_candidate_id')
            if row['candidate_id'] in selected or row['candidate_id'] == seed:
                address['returned_candidates'].append(row)
    allocation = [row for row in csv_rows(run / '05_grid/estimated_grid_1km.csv') if row['family_id'] in wanted]
    final = {}
    for filename, key in [('family_grid.csv', 'weighted'),
                          ('family_highest_weight_cell_grid.csv', 'highest_weight_cell'),
                          ('withheld_families.csv', 'withheld'), ('missing_roles.csv', 'missing_roles')]:
        path = run / '06_activity' / filename
        if key != 'missing_roles' and not path.is_file():
            raise FileNotFoundError(path)
        final[key] = [row for row in csv_rows(path) if row['family_id'] in wanted]
    cases = []
    for number, family in enumerate(families, 1):
        rows = [row for row in inputs if row['family_id'] == family]
        case_addresses = []
        for oid in sorted({row['observation_id'] for row in rows}):
            address = dict(addresses[oid])
            address['spatial_weights'] = [row for row in allocation if row['observation_id'] == oid and row['family_id'] == family]
            case_addresses.append(address)
        cases.append(dict(case=number, family_id=family,
                          application_id=rows[0]['representative_appln_id'],
                          filing_date=rows[0]['representative_filing_date'], year=rows[0]['patent_year'],
                          input_rows=rows, addresses=case_addresses,
                          **{key: [row for row in records if row['family_id'] == family] for key, records in final.items()}))
    return dict(cases=cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path, help='Completed pipeline output directory')
    parser.add_argument('--family', required=True, action='append', help='DOCDB family ID; repeat for several families')
    parser.add_argument('--output', required=True, type=Path, help='New JSON file')
    args = parser.parse_args()
    result = export(args.run, args.family)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
