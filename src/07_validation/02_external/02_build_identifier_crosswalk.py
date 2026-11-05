"""Bridge PATSTAT releases by exact application identity, preserving ambiguity"""

import argparse, csv, datetime, json, sqlite3
from collections import Counter
from pathlib import Path

FIELDS = ('appln_auth', 'appln_nr', 'appln_kind', 'appln_filing_date')


def identity(row):
    values = [row[k].strip().upper() for k in FIELDS]
    date = values[3]
    if len(date) == 8 and date.isdigit():
        date = f'{date[:4]}-{date[4:6]}-{date[6:]}'
    parsed = datetime.date.fromisoformat(date)
    if parsed.year == 9999:
        raise ValueError('Unknown filing date')
    return (*values[:3], parsed.isoformat())


# Bridge historical and current IDs using exact application identity; exclude ambiguous links
def build(old, new, output):
    output.mkdir(parents=True, exist_ok=False)
    counts = Counter()
    d = sqlite3.connect(output / 'identifier-index.sqlite')
    d.execute(
        'CREATE TABLE ids(release TEXT,aid TEXT,authority TEXT,number TEXT,kind TEXT,date TEXT,PRIMARY KEY(release,aid))'
    )
    for release, path in [('reference', old), ('regpat', new)]:
        with path.open(newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not {'appln_id', *FIELDS} <= set(reader.fieldnames or []):
                raise ValueError(
                    'Identifier extracts require appln_id and all four PATSTAT identity fields'
                )
            for row in reader:
                counts[release + '_rows'] += 1
                try:
                    k = identity(row)
                except ValueError:
                    counts[release + '_invalid_date'] += 1
                    continue
                if not row['appln_id'].strip() or not all(k):
                    raise ValueError('Empty application identity')
                previous = d.execute(
                    'SELECT authority,number,kind,date FROM ids WHERE release=? AND aid=?',
                    (release, row['appln_id'].strip()),
                ).fetchone()
                if previous is not None and previous != k:
                    raise ValueError('One application ID has conflicting identities')
                d.execute(
                    'INSERT OR IGNORE INTO ids VALUES(?,?,?,?,?,?)',
                    (release, row['appln_id'].strip(), *k),
                )
        d.commit()
    d.execute('CREATE INDEX identity ON ids(authority,number,kind,date)')
    sql = """
        SELECT max(CASE WHEN release='reference' THEN aid END),
               max(CASE WHEN release='regpat' THEN aid END),
               authority,number,kind,date
        FROM ids
        GROUP BY authority,number,kind,date
        HAVING count(CASE WHEN release='reference' THEN 1 END)=1
           AND count(CASE WHEN release='regpat' THEN 1 END)=1
        ORDER BY authority,number,kind,date
    """
    with (output / 'application_ids.csv').open('x', newline='') as f:
        w = csv.writer(f)
        w.writerow(['reference_appln_id', 'regpat_appln_id', *FIELDS])
        for row in d.execute(sql):
            w.writerow(row)
            counts['one_to_one_matches'] += 1
    (output / 'manifest.json').write_text(
        json.dumps(
            dict(
                method='exact_authority_number_kind_full_filing_date',
                reference_extract=str(old),
                regpat_extract=str(new),
                counts=counts,
                ambiguity_policy='Only one application in each release per exact identity; all identities remain in identifier-index.sqlite',
            ),
            indent=2,
        )
    )
    d.close()


# Check each supplied match against the generated source identifier index
def verified_rows(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not {'reference_appln_id', 'regpat_appln_id', *FIELDS} <= set(reader.fieldnames or []):
            raise ValueError('Unverified numeric-ID crosswalk: use build-reference-ids')
        manifest = path.parent / 'manifest.json'
        index = path.parent / 'identifier-index.sqlite'
        if (
            not manifest.is_file()
            or not index.is_file()
            or json.loads(manifest.read_text()).get('method')
            != 'exact_authority_number_kind_full_filing_date'
        ):
            raise ValueError('Crosswalk requires its generated manifest and identifier index')
        from contextlib import closing

        with closing(sqlite3.connect(index.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for row in reader:
                expected = identity(row)
                for release, key in [
                    ('reference', 'reference_appln_id'),
                    ('regpat', 'regpat_appln_id'),
                ]:
                    found = db.execute(
                        'SELECT authority,number,kind,date FROM ids WHERE release=? AND aid=?',
                        (release, row[key].strip()),
                    ).fetchone()
                    count = db.execute(
                        'SELECT count(*) FROM ids WHERE release=? AND authority=? AND number=? AND kind=? AND date=?',
                        (release, *expected),
                    ).fetchone()[0]
                    if found != expected or count != 1:
                        raise ValueError(
                            'Crosswalk identity differs from its source index or is ambiguous'
                        )
                yield row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference-identifiers', type=Path, required=True)
    p.add_argument('--regpat-identifiers', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    a = p.parse_args()
    build(a.reference_identifiers, a.regpat_identifiers, a.output_dir)


if __name__ == '__main__':
    main()
