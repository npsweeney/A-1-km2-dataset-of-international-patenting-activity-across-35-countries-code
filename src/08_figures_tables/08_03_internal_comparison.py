"""Write masking overlap and weight statistics"""
import argparse
import json
import math
from pathlib import Path


# Combine evaluated cases across sample partitions
def write_table(summary, output):
    data = json.loads(summary.read_text())
    if data.get('stage') != '07/01_internal/evaluate_masks':
        raise ValueError('Expected internal masking evaluation metrics')
    groups = {'street': [], 'postcode': []}
    seen = set()
    for row in data['metrics']:
        key = (row['mask'], row['partition'])
        if key in seen or row['mask'] not in groups:
            raise ValueError('Duplicate or unknown masking group')
        seen.add(key)
        n = row['evaluated']
        if not 0 <= n <= row['cases']:
            raise ValueError('Invalid evaluated case count')
        if n and any(not isinstance(row[field], (int, float)) or not math.isfinite(row[field]) or not 0 <= row[field] <= 1
                     for field in ('support_coverage_1km_evaluated', 'mean_reference_cell_weight_evaluated')):
            raise ValueError('Invalid masking overlap or weight')
        groups[row['mask']].append(row)
    lines = [r'\begin{tabular}{lrrr}', r'\hline',
             r'Mask & Evaluated & \shortstack{Direct 1 km cell\\included (\%)} & \shortstack{Weight in\\direct 1 km cell (\%)} \\',
             r'\hline']
    macros = []
    for mask, rows in groups.items():
        if not rows:
            raise ValueError('Both street and postcode masking results are required')
        n = sum(row['evaluated'] for row in rows)
        values = [f"{100 * sum(row['evaluated'] * row[field] for row in rows if row['evaluated']) / n:.2f}" if n else '--'
                  for field in ('support_coverage_1km_evaluated', 'mean_reference_cell_weight_evaluated')]
        lines.append(f'{mask.title()} & {n:,} & {values[0]} & {values[1]}' + r' \\')
        for name, value in zip(('Inclusion', 'CellWeight'), values):
            macros.append(r'\newcommand{\Masking' + mask.title() + name + '}{' + value + '}')
    lines.extend([r'\hline', r'\end{tabular}'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.tex').write_text('\n'.join(lines) + '\n')
    output.with_suffix('.json').write_text(json.dumps(data, indent=2) + '\n')
    output.with_name('masking-macros.tex').write_text('\n'.join(macros) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    write_table(args.summary, args.output)


if __name__ == '__main__':
    main()
