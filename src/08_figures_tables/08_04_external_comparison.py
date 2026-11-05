"""Plot within-patent 1 km cell-distribution overlap"""
import argparse
import json
from pathlib import Path

from validation.validate import application_key, read

NAMES = ('morrison', 'de_rassenfosse')


# Calculate reference comparisons over explicitly shared cohorts and preserve their denominators
def summarise(activity_manifest, directories, *, names=NAMES):
    if len(names)!=len(directories) or len(names)<2 or len(set(names))!=len(names):raise ValueError("Supply distinct named references")
    activity = json.loads(activity_manifest.read_text())
    summaries = [json.loads((p/'summary.json').read_text()) for p in directories]
    for summary in summaries:
        if summary['grid_identity'] != activity['grid_identity'] or Path(summary['activity_manifest']).resolve() != activity_manifest.resolve():
            raise ValueError('Both references must use the supplied stage 06 run and grid')
    qualification={name:summary.get('id_crosswalk_status','not_recorded') for name,summary in zip(names,summaries)}
    if 'de_rassenfosse' in qualification and qualification['de_rassenfosse'] not in ('exact_application_identity_crosswalk','stable_patstat_id_office_full_filing_date'):raise ValueError('de Rassenfosse crosswalk is unverified; do not present a historical alias comparison as validated identity')
    matches=[]
    for p in directories:
        records=read(p/'04_patent_cell_agreement.csv')
        indexed={application_key(r):r for r in records}
        if len(indexed)!=len(records):
            raise ValueError('Duplicate application-role comparison')
        matches.append(indexed)
    shared=set.intersection(*(set(m) for m in matches))
    if any(s['filing_years']!=summaries[0]['filing_years'] for s in summaries):
        raise ValueError('Reference filing-year windows differ')
    if not shared:
        raise ValueError('No shared eligible applications and roles')
    result=dict(filing_years=summaries[0]['filing_years'], shared_pairs=len(shared),
                shared_families=len({k[0] for k in shared}),
                comparison_unit='application and role',
                id_crosswalk_qualification=qualification,
                eligible_pairs=dict(zip(names,map(len,matches))), references={})
    for name, matched in zip(names,matches):
        cells=[]
        for actor in ('applicant','inventor'):
            rows=[matched[k] for k in sorted(shared) if k[2]==actor]
            if not rows:
                continue
            values=[float(r['agreement_percent']) for r in rows]
            if any(not 0 <= v <= 100 + 1e-9 for v in values):
                raise ValueError('Invalid within-patent agreement')
            cells.append(dict(actor=actor,n=len(rows),
                source_addresses=sum(int(r['source_addresses']) for r in rows),
                mean_agreement_percent=sum(values)/len(values)))
        result['references'][name]=dict(cells=cells)
    return result


# Plot the matched-patent cell agreement
def plot(data, output):
    import subprocess

    qualification=data.get('id_crosswalk_qualification',{})
    if 'de_rassenfosse' in data['references'] and qualification.get('de_rassenfosse') not in ('exact_application_identity_crosswalk','stable_patstat_id_office_full_filing_date'):raise ValueError('Saved figure data lacks verified crosswalk qualification')
    output.parent.mkdir(parents=True, exist_ok=True)
    data_path = output.with_suffix('.json')
    data_path.write_text(json.dumps(data, indent=2)+'\n')
    renderer = Path(__file__).parent/'renderers'/'validation.R'
    subprocess.run(['Rscript', str(renderer), str(data_path), str(output)], check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity-manifest',type=Path)
    parser.add_argument('--morrison',type=Path)
    parser.add_argument('--de-rassenfosse',type=Path)
    parser.add_argument('--data',type=Path,help='saved aggregate figure data')
    parser.add_argument('--output',type=Path,required=True,help='output stem for PNG and JSON')
    args=parser.parse_args()
    if args.data:
        data=json.loads(args.data.read_text())
    else:
        if not all((args.activity_manifest,args.morrison,args.de_rassenfosse)):
            parser.error('Supply --data or the stage 06 manifest and both stage 07 directories')
        directories=(args.morrison,args.de_rassenfosse)
        data=summarise(args.activity_manifest,directories,names=NAMES)
    plot(data,args.output)


if __name__=='__main__':
    main()
