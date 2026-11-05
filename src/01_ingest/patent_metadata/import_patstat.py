"""Import TLS201 CSV or ZIP parts for the applications present in REGPAT"""
import argparse
import csv
import io
import sqlite3
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from ingest.import_regpat import _release_tables, _source_rows

FIELDS = ('appln_id', 'docdb_family_id', 'appln_filing_year', 'appln_filing_date')


# Index the PATSTAT application and family fields needed by intake
def build(inputs, regpat, output):
    if output.exists():
        raise FileExistsError(output)
    tables, _ = _release_tables(regpat)
    ids = {row['appln_id'] for row in _source_rows(tables)}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(output)) as db, db:
            db.execute('CREATE TABLE tls201_appln(appln_id INTEGER PRIMARY KEY, '
                       'docdb_family_id INTEGER, appln_filing_year INTEGER, appln_filing_date TEXT)')
            for path in inputs:
                with ExitStack() as stack:
                    if zipfile.is_zipfile(path):
                        bundle = stack.enter_context(zipfile.ZipFile(path))
                        streams = [stack.enter_context(io.TextIOWrapper(bundle.open(name), encoding='utf-8-sig', newline=''))
                                   for name in bundle.namelist() if name.lower().endswith('.csv')]
                        if not streams:
                            raise ValueError(f'No CSV in {path}')
                    else:
                        streams = [stack.enter_context(path.open(encoding='utf-8-sig', newline=''))]
                    for stream in streams:
                        reader = csv.DictReader(stream)
                        if not set(FIELDS).issubset(reader.fieldnames or ()):
                            raise ValueError(f'{path} requires columns: {", ".join(FIELDS)}')
                        batch = []
                        for row in reader:
                            if row['appln_id'].strip() in ids:
                                batch.append(tuple(row[field].strip() for field in FIELDS))
                                if len(batch) == 10000:
                                    db.executemany('INSERT INTO tls201_appln VALUES(?,?,?,?)', batch)
                                    batch.clear()
                        db.executemany('INSERT INTO tls201_appln VALUES(?,?,?,?)', batch)
            count = db.execute('SELECT count(*) FROM tls201_appln').fetchone()[0]
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return dict(regpat_applications=len(ids), imported_applications=count, unmatched_applications=len(ids) - count)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, nargs='+', required=True)
    parser.add_argument('--regpat', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(build(args.input, args.regpat, args.output))


if __name__ == '__main__':
    main()
