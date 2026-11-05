"""Evaluate completed independent masked allocations against held-out coordinates"""

import argparse, json, math, re
from collections import defaultdict
from pathlib import Path
from validation.validate import cell_index, compare_grids, read, resolve, write


# Evaluate independent masked searches against held-out truth, including unresolved cases
def evaluate(manifest_path, truth_path, output, query_manifest):
    allocation = json.loads(manifest_path.read_text())
    grid = allocation['grid_identity']
    cell = cell_index(grid)
    transport = json.loads(query_manifest.read_text())
    if (
        int(
            transport.get('client_telemetry', transport.get('telemetry', {})).get(
                'initial_cache_entries', -1
            )
        )
        != 0
    ):
        raise ValueError(
            'Independent masking requires a fresh cache recorded in the query run manifest'
        )
    if not transport.get('configuration', {}).get('endpoint', transport.get('endpoint')):
        raise ValueError('Masked query endpoint identity is required')
    if (
        transport.get('partial')
        or int(transport.get('telemetry', {}).get('attempted_requests', 0)) <= 0
    ):
        raise ValueError('Require a completed actual masked-query run')
    support_path = resolve(allocation['support_release'], manifest_path.parent)
    support = json.loads(support_path.read_text())
    if support['grid_identity'] != grid:
        raise ValueError('Support and allocation grids differ')
    cross = json.loads(resolve(allocation['cross_stage_release'], manifest_path.parent).read_text())
    recorded = cross.get('geocoder_manifest', {}).get('path')
    if not recorded and cross.get('inputs', {}).get('observations.csv'):
        recorded = str(
            Path(cross['inputs']['observations.csv']['path']).parent / 'run_manifest.json'
        )
    if not recorded or Path(recorded).resolve() != query_manifest.resolve():
        raise ValueError('Query manifest does not belong to the evaluated allocation')
    cohort = {
        (r['observation_id'], r['patent_year'])
        for r in read(resolve(cross['temporal_child'], manifest_path.parent))
    }
    supports = {
        r['observation_id']: {c['cell_id_100m'] for c in r['accepted_cells']}
        for r in support['supports']
    }
    parents = {r['observation_id']: {f'r{int(m[1])//10}c{int(m[2])//10}' for c in r['accepted_cells'] for m in [re.fullmatch(r'r(\d+)c(\d+)',c['cell_id_100m'])]} for r in support['supports']}
    methods = {
        r['observation_id']: r.get('postcode_construction_method')
        or r.get('support_method', 'street_geometry')
        for r in support['supports']
    }
    weights = read(resolve(allocation['artifacts']['estimated_grid_1km.csv'], manifest_path.parent))
    vectors = defaultdict(dict)
    types = {}
    for r in weights:
        k = (r['observation_id'], r['patent_year'], r['estimate_id'])
        c = r['grid_cell_id']
        if c in vectors[k]:
            raise ValueError('Duplicate estimate cell')
        w = float(r['normalized_weight'])
        if not math.isfinite(w) or w < 0:
            raise ValueError('Invalid spatial weight')
        vectors[k][c] = w
        types[k] = r['evidence_type']
    grouped = defaultdict(list)
    for k, v in vectors.items():
        if not math.isclose(sum(v.values()), 1, abs_tol=1e-9):
            raise ValueError('Estimate weights do not sum to one')
        grouped[k[:2]].append((types[k], v))
    truth = read(truth_path)
    rows = []
    cases = set()
    for r in truth:
        if r['case_id'] in cases:
            raise ValueError('Duplicate truth case')
        cases.add(r['case_id'])
        k = (r['masked_observation_id'], r['patent_year'])
        versions = grouped[k]
        if k not in cohort:
            raise ValueError('Truth case is absent from the evaluated cohort')
        result = dict(
            case_id=r['case_id'],
            source_observation_id=r['source_observation_id'],
            masked_observation_id=k[0],
            patent_year=k[1],
            mask=r['mask'],
            sample_partition=r['sample_partition'],
            country_code=r.get('country_code', ''),
            construction_method=methods.get(k[0], 'unavailable'),
            status='withheld_or_unmatched',
            in_100m_support=0,
            in_1km_support=0,
            reference_cell_spatial_weight=0,
            grid_disagreement_1km=None,
            grid_disagreement_10km=None,
        )
        if versions:
            first = versions[0]
            if any(v != first for v in versions):
                raise ValueError('One masked input/year has inconsistent estimates')
            route, v = first
            if ('postcode' if route == 'postcode_neighbourhood' else route) != r['mask']:
                result['status'] = 'unexpected_precision_route'
            elif k[0] not in supports:
                raise ValueError('Accepted mask lacks support')
            else:
                lat, lon = float(r['true_latitude']), float(r['true_longitude'])
                result.update(
                    status='evaluated',
                    in_100m_support=int(cell(lat, lon, 100) in supports[k[0]]),
                    in_1km_support=int(cell(lat, lon, 1000) in parents[k[0]]),
                    reference_cell_spatial_weight=v.get(cell(lat, lon, 1000), 0),
                )
                # Reuse the external comparison's equal-mass metric at both grid resolutions
                own = {(r['mask'], k[1], c): w for c, w in v.items()}
                reference = {(r['mask'], k[1], cell(lat, lon, 1000)): 1}
                _, metrics = compare_grids(own, reference)
                for metric in metrics:
                    result[f"grid_disagreement_{metric['resolution_m']//1000}km"] = metric[
                        'redistributed_mass_fraction'
                    ]
        rows.append(result)
    summary = []
    for route, partition in sorted({(r['mask'], r['sample_partition']) for r in rows}):
        subset = [r for r in rows if (r['mask'], r['sample_partition']) == (route, partition)]
        accepted = [r for r in subset if r['status'] == 'evaluated']
        summary.append(
            dict(
                mask=route,
                partition=partition,
                cases=len(subset),
                unique_masked_inputs=len({r['masked_observation_id'] for r in subset}),
                evaluated=len(accepted),
                withheld_or_wrong_route=len(subset) - len(accepted),
                support_coverage_1km_all_cases=sum(r['in_1km_support'] for r in subset)/len(subset),
                support_coverage_1km_evaluated=sum(r['in_1km_support'] for r in accepted)/len(accepted) if accepted else None,
                mean_reference_cell_weight_evaluated=sum(r['reference_cell_spatial_weight'] for r in accepted)/len(accepted) if accepted else None,
                support_coverage_all_cases=sum(r['in_100m_support'] for r in subset) / len(subset),
                support_coverage_evaluated=(
                    sum(r['in_100m_support'] for r in accepted) / len(accepted)
                    if accepted
                    else None
                ),
                mean_reference_cell_weight_all_cases=sum(
                    r['reference_cell_spatial_weight'] for r in subset
                )
                / len(subset),
            )
        )
        for resolution in (1, 10):
            summary[-1][f'mean_grid_disagreement_{resolution}km_evaluated'] = (
                sum(r[f'grid_disagreement_{resolution}km'] for r in accepted) / len(accepted)
                if accepted
                else None
            )
    breakdown = []
    for key in sorted(
        {
            (r['mask'], r['sample_partition'], r['country_code'], r['construction_method'])
            for r in rows
        }
    ):
        subset = [
            r
            for r in rows
            if (r['mask'], r['sample_partition'], r['country_code'], r['construction_method'])
            == key
        ]
        evaluated = sum(r['status'] == 'evaluated' for r in subset)
        breakdown.append(
            dict(
                zip(('mask', 'partition', 'country_code', 'construction_method'), key),
                cases=len(subset),
                evaluated=evaluated,
                withheld_or_wrong_route=len(subset) - evaluated,
            )
        )
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'masking_results.csv', rows, list(rows[0]))
    (output / 'summary.json').write_text(
        json.dumps(
            dict(
                stage='07/01_internal/evaluate_masks',
                allocation_manifest=str(manifest_path.resolve()),
                query_manifest=str(query_manifest.resolve()),
                truth_used_only_for_evaluation=True,
                metrics=summary,
                country_method_counts=breakdown,
            ),
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--allocation-manifest', type=Path, required=True)
    p.add_argument('--query-manifest', type=Path, required=True)
    p.add_argument('--truth', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    a = p.parse_args()
    evaluate(a.allocation_manifest, a.truth, a.output_dir, a.query_manifest)


if __name__ == '__main__':
    main()
