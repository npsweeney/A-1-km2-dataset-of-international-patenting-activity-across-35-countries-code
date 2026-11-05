"""Build an exact OPS publication queue for REGPAT applications missing PATSTAT families"""
from __future__ import annotations
import argparse
import csv
import sqlite3
from pathlib import Path
FIELDS = ('appln_id', 'regpat_source', 'source_application_number', 'source_publication_number', 'ops_reference_primary', 'ops_reference_alternate')

def wo_references(publication: str) -> tuple[str, str]:
    digits = publication.strip().upper().removeprefix('WO')
    if len(digits) != 10 or not digits.isdigit():
        return ('', '')
    year = int(digits[:4])
    serial = int(digits[4:])
    if year <= 2001:
        return (f'WO.{year % 100:02d}{serial:05d}', '')
    if year == 2002:
        five = f'WO.{year % 100:02d}{serial:05d}'
        six = f'WO.{year % 100:02d}{serial:06d}'
        return (five, six if six != five else '')
    if year == 2003:
        return (f'WO.{year % 100:02d}{serial:06d}', '')
    return (f'WO.{year:04d}{serial:06d}', '')

# Queue unresolved publication references without duplicating completed requests
def scan(path: Path, *, source_name: str, pending: set[str], output: dict[str, dict[str, str]]) -> None:
    is_pct = 'PCT' in source_name
    with path.open(newline='', encoding='utf-8-sig') as handle:
        for raw in csv.DictReader(handle, delimiter='|'):
            appln_id = (raw.get('appln_id') or '').strip()
            if appln_id not in pending:
                continue
            if is_pct:
                publication = (raw.get('pct_nbr') or '').strip()
                primary, alternate = wo_references(publication)
                application = (raw.get('internat_appln_nr') or '').strip()
            else:
                publication = (raw.get('pub_nbr') or '').strip()
                primary = f'EP.{publication}' if publication else ''
                alternate = ''
                application = (raw.get('app_nbr') or '').strip()
            output[appln_id] = {'appln_id': appln_id, 'regpat_source': source_name, 'source_application_number': application, 'source_publication_number': publication, 'ops_reference_primary': primary, 'ops_reference_alternate': alternate}
            pending.remove(appln_id)
            if not pending:
                return

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--family-db', type=Path, help='optional PATSTAT application_family database')
    parser.add_argument('--application-ids', type=Path, help='optional newline application IDs selecting any desired cohort')
    parser.add_argument('--epo-app', required=True, type=Path)
    parser.add_argument('--epo-inv', required=True, type=Path)
    parser.add_argument('--pct-app', required=True, type=Path)
    parser.add_argument('--pct-inv', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    paths = (args.epo_app, args.epo_inv, args.pct_app, args.pct_inv)
    pending = set()
    for path in paths:
        with path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle, delimiter='|')
            if 'appln_id' not in (reader.fieldnames or []):
                raise ValueError(f'Missing appln_id in {path.name}')
            pending.update((value for row in reader if (value := (row.get('appln_id') or '').strip())))
    if args.application_ids is not None:
        selected = {line.strip() for line in args.application_ids.read_text(encoding='utf-8').splitlines() if line.strip()}
        pending &= selected
    if args.family_db is not None:
        connection = sqlite3.connect(args.family_db.resolve().as_uri() + '?mode=ro', uri=True)
        resolved = {row[0] for row in connection.execute("SELECT appln_id FROM application_family WHERE docdb_family_id IS NOT NULL AND docdb_family_id NOT IN ('', '0')")}
        connection.close()
        pending -= resolved
    initial_unmatched = len(pending)
    output: dict[str, dict[str, str]] = {}
    for path, name in ((args.epo_app, 'EPO_App'), (args.pct_app, 'PCT_App'), (args.epo_inv, 'EPO_Inv'), (args.pct_inv, 'PCT_Inv')):
        scan(path, source_name=name, pending=pending, output=output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows((output[key] for key in sorted(output, key=int)))
    print(f'unmatched={initial_unmatched} queued={len(output)} not_found_in_regpat={len(pending)} output={args.output}')
if __name__ == '__main__':
    main()
