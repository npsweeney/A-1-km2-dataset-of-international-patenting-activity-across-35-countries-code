"""Prepare street and postcode masks for 10,000 direct addresses"""

import argparse
import csv
import json
import math
from pathlib import Path

SOURCE_FIELDS = (
    'source_observation_id', 'house_number', 'street', 'city', 'postal_code',
    'ctry_code', 'reg_code', 'true_latitude', 'true_longitude',
    'ground_truth_precision', 'appln_id', 'person_id', 'actor',
    'patent_year', 'sample_partition',
)
REQUIRED_VALUES = (
    'source_observation_id', 'appln_id', 'person_id', 'house_number',
    'street', 'postal_code', 'ctry_code',
)


def write_csv(path, rows):
    with path.open('x', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


# Validate the full sample before creating output files
def prepare(source, output, truth_output, *, expected_cases=None):
    construction, heldout = output.resolve(), truth_output.resolve()
    if construction == heldout or construction in heldout.parents or heldout in construction.parents:
        raise ValueError('Truth must be outside construction')
    with source.open(newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not set(SOURCE_FIELDS) <= rows[0].keys():
        raise ValueError('Missing labelled source fields')
    if expected_cases is not None and (
        len(rows) != expected_cases
        or len({row['source_observation_id'] for row in rows}) != expected_cases
    ):
        raise ValueError(f'Require exactly {expected_cases} distinct source addresses')

    masks = {'street': [], 'postcode': []}
    truth, seen, partitions = [], set(), {}
    for number, row in enumerate(rows, 1):
        if row['ground_truth_precision'] != 'address' or not all(
            (row.get(k) or '').strip() for k in REQUIRED_VALUES
        ):
            raise ValueError('Require direct addresses with complete IDs and components')
        case = row['source_observation_id'], row['patent_year']
        if case in seen:
            raise ValueError('Duplicate source address/year case')
        seen.add(case)
        lat, lon = float(row['true_latitude']), float(row['true_longitude'])
        if (
            not math.isfinite(lat + lon) or not -90 <= lat <= 90
            or not -180 <= lon <= 180 or (lat, lon) == (0, 0)
        ):
            raise ValueError('Invalid direct-coordinate truth')
        if (
            row['actor'] not in {'inventor', 'applicant'}
            or row['sample_partition'] not in {'calibration', 'holdout'}
            or not row['patent_year'].isdigit()
            or not 1000 <= int(row['patent_year']) <= 9998
        ):
            raise ValueError('Invalid role, year or partition')

        # Remove building detail and keep coordinates out of both masks
        for route, items in masks.items():
            values = {k: row[k] for k in ('appln_id', 'person_id', 'actor', 'patent_year')}
            values.update(
                patent_year_status='valid',
                address=row['street'].strip() if route == 'street' else '',
                city=row['city'].strip(), postal_code=row['postal_code'].strip(),
                ctry_code=row['ctry_code'].strip().upper(), reg_code=row['reg_code'].strip(),
            )
            oid = 'address:' + json.dumps(
                [values[k] for k in ('address', 'city', 'postal_code', 'ctry_code', 'reg_code')],
                ensure_ascii=False, separators=(',', ':'),
            )
            group = route, oid
            if group in partitions and partitions[group] != row['sample_partition']:
                raise ValueError('One masked input spans calibration and holdout')
            partitions[group] = row['sample_partition']
            values['observation_id'] = oid
            items.append(values)
            truth.append(dict(
                case_id=f'{route}-{number:06d}', source_observation_id=row['source_observation_id'],
                masked_observation_id=oid, patent_year=row['patent_year'], mask=route,
                sample_partition=row['sample_partition'], country_code=row['ctry_code'].strip().upper(),
                true_latitude=lat, true_longitude=lon,
            ))

    # Write masked inputs and held-out coordinates separately
    output.mkdir(parents=True, exist_ok=False)
    truth_output.mkdir(parents=True, exist_ok=False)
    for route, items in masks.items():
        write_csv(output / f'{route}.csv', items)
    write_csv(output / 'all-masks.csv', masks['street'] + masks['postcode'])
    write_csv(truth_output / 'truth.csv', truth)
    (output / 'manifest.json').write_text(json.dumps(dict(
        stage='07/01_internal/prepare_masks', source_cases=len(rows),
        cases_per_mask={route: len(items) for route, items in masks.items()},
        queries_executed=False, construction_fields=list(masks['street'][0]),
        requires_independent_queries=True, cache_policy='fresh', truth_excluded=True,
    ), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--truth-output-dir', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.input, args.output_dir, args.truth_output_dir, expected_cases=10_000)


if __name__ == '__main__':
    main()
