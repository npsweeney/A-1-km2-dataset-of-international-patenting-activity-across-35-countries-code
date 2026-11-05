"""Map exact REGPAT application IDs to PATSTAT DOCDB families"""
from __future__ import annotations
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--regpat-ids', required=True, type=Path)
    parser.add_argument('--patstat-db', required=True, type=Path)
    parser.add_argument('--output-db', required=True, type=Path)
    parser.add_argument('--summary', required=True, type=Path)
    args = parser.parse_args()
    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    if args.output_db.exists():
        raise SystemExit(f'Refusing to overwrite existing output: {args.output_db}')
    connection = sqlite3.connect(args.output_db)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.execute('PRAGMA temp_store=FILE')
    connection.execute('CREATE TABLE regpat_application (appln_id TEXT PRIMARY KEY) WITHOUT ROWID')
    with args.regpat_ids.open(encoding='utf-8') as handle:
        batch: list[tuple[str]] = []
        for line in handle:
            value = line.strip()
            if value:
                batch.append((value,))
            if len(batch) == 100000:
                connection.executemany('INSERT INTO regpat_application VALUES (?)', batch)
                connection.commit()
                batch.clear()
        if batch:
            connection.executemany('INSERT INTO regpat_application VALUES (?)', batch)
            connection.commit()
    connection.execute('ATTACH DATABASE ? AS patstat', (str(args.patstat_db),))
    columns = {row[1] for row in connection.execute('PRAGMA patstat.table_info(tls201_appln)')}
    inpadoc = "NULLIF(NULLIF(TRIM(CAST(p.inpadoc_family_id AS TEXT)), '0'), '')" if 'inpadoc_family_id' in columns else 'NULL'
    connection.execute(f"\n        CREATE TABLE application_family AS\n        SELECT\n            r.appln_id,\n            NULLIF(NULLIF(TRIM(CAST(p.docdb_family_id AS TEXT)), '0'), '') AS docdb_family_id,\n            {inpadoc} AS inpadoc_family_id\n        FROM regpat_application AS r\n        LEFT JOIN patstat.tls201_appln AS p USING (appln_id)\n        ")
    connection.execute('CREATE UNIQUE INDEX application_family_appln_id ON application_family(appln_id)')
    connection.execute('CREATE INDEX application_family_docdb ON application_family(docdb_family_id)')
    connection.execute('CREATE INDEX application_family_inpadoc ON application_family(inpadoc_family_id)')
    connection.commit()
    row = connection.execute('\n        SELECT\n            COUNT(*),\n            COUNT(docdb_family_id),\n            COUNT(DISTINCT docdb_family_id),\n            COUNT(inpadoc_family_id),\n            COUNT(DISTINCT inpadoc_family_id),\n            SUM(docdb_family_id IS NULL AND inpadoc_family_id IS NULL)\n        FROM application_family\n        ').fetchone()
    assert row is not None
    total, docdb_rows, docdb_families, inpadoc_rows, inpadoc_families, unmatched = row
    summary = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'regpat_unique_applications': total, 'applications_with_docdb_family': docdb_rows, 'unique_docdb_families': docdb_families, 'applications_with_inpadoc_family': inpadoc_rows, 'unique_inpadoc_families': inpadoc_families, 'applications_without_family_match': unmatched, 'regpat_ids': str(args.regpat_ids), 'patstat_db': str(args.patstat_db), 'output_db': str(args.output_db), 'join_key': 'appln_id', 'zero_family_ids_treated_as_missing': True}
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(args.summary.read_text(encoding='utf-8'), end='')
    connection.close()
if __name__ == '__main__':
    main()
