"""Import REGPAT and select DOCDB representatives by earliest full filing date"""
from __future__ import annotations
import argparse
import csv
from pathlib import Path

# Locate the four native applicant/inventor tables in the supplied REGPAT release
def _release_tables(source):
    tables = []
    fields = []
    for office, code, actor in [('EPO', 'App', 'applicant'), ('EPO', 'Inv', 'inventor'), ('PCT', 'App', 'applicant'), ('PCT', 'Inv', 'inventor')]:
        paths = sorted(source.glob(f'*_{office}_{code}_reg.txt'))
        if len(paths) != 1:
            raise ValueError(f'Expected one {office} {actor} table; found {len(paths)}')
        path = paths[0]
        with path.open(newline='', encoding='utf-8-sig') as handle:
            header = next(csv.reader(handle, delimiter='|'), None)
        if header is None:
            raise ValueError(f'No header in {path.name}')
        header = [value.strip() for value in header]
        if any((not value for value in header)) or len(set(header)) != len(header):
            raise ValueError(f'Empty or duplicate column names in {path.name}')
        required = {'appln_id', 'address', 'city', 'postal_code', 'reg_code', 'ctry_code', 'app_name' if code == 'App' else 'inv_name'}
        if office == 'EPO':
            required.add('person_id')
        if not required.issubset(header):
            raise ValueError(f'Missing columns in {path.name}: {sorted(required - set(header))}')
        if set(header) & {'office', 'actor', 'source_file', 'source_row'}:
            raise ValueError('Source contains reserved provenance columns')
        fields.extend((field for field in header if field not in fields))
        tables.append((path, office, actor, header))
    fields = ['office', 'actor', 'source_file', 'source_row'] + fields
    return (tables, fields)

# Read the four REGPAT release tables

def _source_rows(tables):
    for path, office, actor, header in tables:
        with path.open(newline='', encoding='utf-8-sig') as incoming:
            reader = csv.reader(incoming, delimiter='|')
            next(reader)
            for number, values in enumerate(reader, start=2):
                if len(values) != len(header):
                    raise ValueError(f'Wrong field count in {path.name} at row {number}')
                row = dict(zip(header, (value.strip() for value in values)))
                row.update(office=office, actor=actor, source_file=path.name, source_row=number)
                yield row

# Resolve exact application dates and families before selecting representatives and the year cohort
def prepare_cohort(source, output, *, patstat_db=None, ops_db=None, family_backbone=None, start_year=2000, end_year=None, application_master=None, metadata_cache=None, metadata_cache_backup=None):
    """Select the earliest REGPAT application in each DOCDB family and prepare its addresses"""
    import json
    import sqlite3
    import tempfile
    from datetime import date
    from contextlib import ExitStack
    from functools import lru_cache
    from ingest.prepare_observations import clean_row, stable_id, ADDRESS_KEY_FIELDS
    from ingest.patent_metadata.application_years import resolve_filing_year
    from ingest.patent_metadata.prepare_ops_queue import wo_references
    from ingest.patent_metadata.resolve_ops_families import application_filing_date
    source, output = (Path(source), Path(output))
    if end_year is not None and start_year > end_year:
        raise ValueError('start-year exceeds end-year')
    paths = (output, output.with_suffix('.links.csv'), output.with_suffix('.undated.csv'), output.with_suffix('.excluded.csv'), output.with_suffix('.families.csv'), output.with_suffix('.manifest.json'))
    if len({path.resolve() for path in paths}) != len(paths) or any((path.exists() for path in paths)):
        raise ValueError('All cohort output files must be new and distinct')
    counts = dict(source_rows=0, eligible_links=0, unique_addresses=0, undated_rows=0, outside_years=0, later_application_rows=0, excluded_rows=0, docdb_families=0, selected_families=0)
    created = []
    master_identity = None

    def family_id(value):
        value = str(value).strip() if value is not None else ''
        return value if value not in ('', '0') else None

    def filing_date(value, year):
        text = str(value or '').strip()
        if len(text) == 8 and text.isdigit():
            text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None
        return parsed.isoformat() if parsed.year == year else None
    try:
        with ExitStack() as stack:
            databases = []
            for path in (patstat_db, ops_db, family_backbone):
                if path is None:
                    databases.append(None)
                else:
                    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
                    stack.callback(db.close)
                    databases.append(db)
            patstat, ops, backbone = databases
            canonical = None
            if application_master is not None:
                import duckdb
                release_path = Path(application_master).resolve().parent / 'release.json'
                if not release_path.is_file():
                    raise ValueError('Canonical application master requires adjacent validated release.json')
                master_identity = json.loads(release_path.read_text())
                if master_identity.get('validated') is not True or not master_identity.get('release_id'):
                    raise ValueError('Canonical application master release is not verified')
                canonical_dir = stack.enter_context(tempfile.TemporaryDirectory())
                canonical = sqlite3.connect(str(Path(canonical_dir) / 'canonical-dates.sqlite'))
                stack.callback(canonical.close)
                canonical.execute('PRAGMA journal_mode=OFF')
                canonical.execute('PRAGMA synchronous=OFF')
                canonical.execute('CREATE TABLE dates(appln_id TEXT PRIMARY KEY, filing_date TEXT, filing_year INTEGER, conflict INTEGER) WITHOUT ROWID')
                with duckdb.connect() as reader:
                    reader.execute("SET threads=4")
                    reader.execute("SET memory_limit='2GB'")
                    cursor = reader.execute('SELECT appln_id, CAST(filing_date AS VARCHAR), filing_year, coalesce(date_conflict,false) FROM read_parquet(?)', [str(Path(application_master).resolve(strict=True))])
                    while rows := cursor.fetchmany(50000):
                        canonical.executemany('INSERT INTO dates VALUES(?,?,?,?)', rows)
                canonical.commit()

            patstat_columns = {r[1] for r in patstat.execute('PRAGMA table_info(tls201_appln)')} if patstat else set()
            if patstat:
                plan = patstat.execute('EXPLAIN QUERY PLAN SELECT appln_filing_year FROM tls201_appln WHERE appln_id=?', ('1',)).fetchall()
                if any(('SCAN tls201_appln' in row[-1] for row in plan)):
                    raise ValueError('PATSTAT appln_id must be indexed for cohort preparation')
            ops_tables = {r[0] for r in ops.execute("SELECT name FROM sqlite_master WHERE type='table'")} if ops else set()
            ops_application_columns = {r[1] for r in ops.execute('PRAGMA table_info(application)')} if ops else set()

            # Cache PATSTAT metadata and select dates from the canonical master
            cache_dir = stack.enter_context(tempfile.TemporaryDirectory()) if metadata_cache is None else None
            cache_path = Path(metadata_cache) if metadata_cache else Path(cache_dir) / 'metadata.sqlite'
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            import fcntl
            lock = stack.enter_context(Path(str(cache_path) + '.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backup_path = Path(metadata_cache_backup) if metadata_cache_backup else None
            if backup_path and backup_path.resolve() == cache_path.resolve():
                raise ValueError('Local metadata index and durable backup must be distinct')
            if not cache_path.exists() and backup_path and backup_path.exists():
                with sqlite3.connect(backup_path.resolve().as_uri() + '?mode=ro', uri=True) as saved_index:
                    with sqlite3.connect(str(cache_path)) as restored:
                        saved_index.backup(restored)
            local = sqlite3.connect(str(cache_path), timeout=60)
            stack.callback(local.close)
            local.execute('CREATE TABLE IF NOT EXISTS identity(value TEXT NOT NULL)')
            local.execute('CREATE TABLE IF NOT EXISTS metadata(appln_id TEXT PRIMARY KEY, matches TEXT NOT NULL) WITHOUT ROWID')
            identity = {}
            if patstat_db:
                origin = Path(patstat_db).resolve()
                stat = origin.stat()
                identity = dict(path=str(origin), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                schema=patstat.execute("SELECT sql FROM sqlite_master WHERE name='tls201_appln'").fetchone()[0])
                wal = Path(str(origin) + '-wal')
                if wal.exists():
                    stat = wal.stat()
                    identity['wal'] = [stat.st_size, stat.st_mtime_ns]
            encoded = json.dumps(identity, sort_keys=True)
            saved = local.execute('SELECT value FROM identity').fetchall()
            if saved and saved != [(encoded,)]:
                raise ValueError('Metadata cache source identity changed; use a new cache path')
            if not saved:
                local.execute('INSERT INTO identity VALUES(?)', (encoded,))
                local.commit()
            columns = ['appln_filing_year', 'appln_filing_date', 'docdb_family_id']
            selected = ','.join(name if name in patstat_columns else 'NULL' for name in columns)

            def save_index():
                if backup_path:
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(str(backup_path)) as durable:
                        local.backup(durable)

            def batched_rows(tables):
                from itertools import islice
                incoming = iter(_source_rows(tables))
                since_backup = 0
                while batch := list(islice(incoming, 10000)):
                    ids = list(dict.fromkeys(row['appln_id'] for row in batch))
                    for offset in range(0, len(ids), 500):
                        chunk = ids[offset:offset + 500]
                        marks = ','.join('?' for _ in chunk)
                        found = {r[0] for r in local.execute(f'SELECT appln_id FROM metadata WHERE appln_id IN ({marks})', chunk)}
                        missing = [aid for aid in chunk if aid not in found]
                        if not missing:
                            continue
                        records = {aid: [] for aid in missing}
                        if patstat:
                            marks = ','.join('?' for _ in missing)
                            for record in patstat.execute(f'SELECT appln_id,{selected} FROM tls201_appln WHERE appln_id IN ({marks})', missing):
                                records[str(record[0])].append(record[1:])
                        local.executemany('INSERT INTO metadata VALUES(?,?)',
                                          ((aid, json.dumps(rows)) for aid, rows in records.items()))
                        local.commit()  # Completed batches survive interruption and are reusable
                    since_backup += len(batch)
                    if since_backup >= 1000000:
                        save_index()
                        since_backup = 0
                    yield from batch
                save_index()

            @lru_cache(maxsize=100000)
            def application_metadata(appln_id, office, publication, application):
                values = {}
                if patstat:
                    matches = json.loads(local.execute('SELECT matches FROM metadata WHERE appln_id=?', (appln_id,)).fetchone()[0])
                    if len(matches) > 1:
                        raise ValueError('Conflicting PATSTAT application records')
                    if matches:
                        values = dict(zip(columns, matches[0]))
                ops_evidence = None
                if 'application_date_evidence' in ops_tables:
                    refs = [f'EP.{publication}'] if office == 'EPO' else list(wo_references(publication))
                    ops_evidence = application_filing_date(ops, references=[r for r in refs if r], application_number=application, application_country='EP' if office == 'EPO' else 'WO')
                evidence = resolve_filing_year(values.get('appln_filing_year'), ops_evidence)
                year = evidence['patent_year']
                date_value = filing_date(values.get('appln_filing_date'), year)
                if date_value is None and ops_evidence and (ops_evidence.get('status') == 'valid'):
                    date_value = filing_date(ops_evidence.get('filing_date'), year)
                if canonical is not None:
                    record = canonical.execute('SELECT filing_date,filing_year,conflict FROM dates WHERE appln_id=?', (appln_id,)).fetchone()
                    date_value = filing_date(record[0], record[1]) if record and not record[2] else None
                    year = int(date_value[:4]) if date_value else None
                    evidence = dict(patent_year=year, patent_year_status='valid' if year else 'missing',
                                    year_source='canonical_application_master',
                                    application_master=str(Path(application_master).resolve()),
                                    date_conflict=bool(record and record[2]))
                patstat_family = family_id(values.get('docdb_family_id'))
                ops_family = None
                if 'family_id' in ops_application_columns:
                    rows = ops.execute('SELECT family_id FROM application WHERE appln_id=?', (appln_id,)).fetchall()
                    if len(rows) > 1:
                        raise ValueError('Conflicting OPS application records')
                    ops_family = family_id(rows[0][0]) if rows else None
                fid = 'docdb:' + (patstat_family or ops_family) if patstat_family or ops_family else ''
                conflict = bool(patstat_family and ops_family and (patstat_family != ops_family))
                if backbone:
                    record = backbone.execute('SELECT canonical_family_id,resolution_status,family_id_kind,family_resolution_conflict FROM regpat_family_backbone WHERE appln_id=?', (appln_id,)).fetchone()
                    supplied = str(record[0]) if record and record[1] == 'resolved' and (record[2] == 'docdb') else ''
                    conflict = conflict or bool(record and record[3]) or bool(fid and supplied and (fid != supplied))
                    fid = fid or supplied
                issue = 'family_source_conflict' if conflict else 'unresolved_docdb_family' if not fid else 'missing_filing_date' if date_value is None else ''
                return dict(evidence, family_id=fid, filing_date=date_value or '', selection_issue=issue)
            tables, source_fields = _release_tables(source)
            scratch = stack.enter_context(tempfile.TemporaryDirectory())
            selection = sqlite3.connect(str(Path(scratch) / 'applications.sqlite'))
            stack.callback(selection.close)
            selection.execute('CREATE TABLE applications(appln_id TEXT PRIMARY KEY,family_id TEXT,patent_year INTEGER,filing_date TEXT,issue TEXT)')
            for row in batched_rows(tables):
                evidence = application_metadata(row['appln_id'], row['office'], row.get('pub_nbr', '') if row['office'] == 'EPO' else row.get('pct_nbr', ''), row.get('app_nbr', '') if row['office'] == 'EPO' else row.get('internat_appln_nr', ''))
                value = (evidence['family_id'], evidence['patent_year'], evidence['filing_date'], evidence['selection_issue'])
                previous = selection.execute('SELECT family_id,patent_year,filing_date,issue FROM applications WHERE appln_id=?', (row['appln_id'],)).fetchone()
                if previous is not None and previous != value:
                    raise ValueError(f"Inconsistent family or date evidence for application {row['appln_id']}")
                selection.execute('INSERT OR IGNORE INTO applications VALUES(?,?,?,?,?)', (row['appln_id'], *value))
            selection.execute('CREATE INDEX application_family ON applications(family_id)')
            selection.execute('CREATE TABLE representatives(family_id TEXT PRIMARY KEY,appln_id TEXT,patent_year INTEGER,filing_date TEXT,status TEXT)')
            for fid, in selection.execute("SELECT DISTINCT family_id FROM applications WHERE family_id <> ''"):
                counts['docdb_families'] += 1
                issue = selection.execute("SELECT issue FROM applications WHERE family_id=? AND issue<>'' ORDER BY issue LIMIT 1", (fid,)).fetchone()
                if issue:
                    selection.execute('INSERT INTO representatives VALUES(?,?,?,?,?)', (fid, '', None, '', issue[0]))
                    continue
                aid, year, date_value = selection.execute('SELECT appln_id,patent_year,filing_date FROM applications WHERE family_id=? ORDER BY filing_date,CAST(appln_id AS INTEGER),appln_id LIMIT 1', (fid,)).fetchone()
                status = 'selected' if year >= start_year and (end_year is None or year <= end_year) else 'outside_years'
                counts['selected_families'] += status == 'selected'
                selection.execute('INSERT INTO representatives VALUES(?,?,?,?,?)', (fid, aid, year, date_value, status))
            fields = list(source_fields) + ['observation_id', 'patent_year', 'patent_year_status', 'year_source', 'year_evidence', 'family_id', 'representative_appln_id', 'representative_filing_date', 'selection_status']
            writers = []
            for path in paths[:4]:
                handle = stack.enter_context(path.open('x', newline='', encoding='utf-8'))
                created.append(path)
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writers.append(writer)
            addresses, links, undated, excluded = writers
            handle = stack.enter_context(paths[4].open('x', newline='', encoding='utf-8'))
            created.append(paths[4])
            families = csv.writer(handle)
            families.writerow(['family_id', 'representative_appln_id', 'patent_year', 'representative_filing_date', 'selection_status'])
            families.writerows(selection.execute('SELECT * FROM representatives ORDER BY family_id'))
            seen = {}
            for row in batched_rows(tables):
                counts['source_rows'] += 1
                fid, year, date_value, issue = selection.execute('SELECT family_id,patent_year,filing_date,issue FROM applications WHERE appln_id=?', (row['appln_id'],)).fetchone()
                representative = selection.execute('SELECT appln_id,patent_year,filing_date,status FROM representatives WHERE family_id=?', (fid,)).fetchone()
                aid, _, rep_date, status = representative if representative else ('', None, '', issue)
                evidence = application_metadata(row['appln_id'], row['office'], row.get('pub_nbr', '') if row['office'] == 'EPO' else row.get('pct_nbr', ''), row.get('app_nbr', '') if row['office'] == 'EPO' else row.get('internat_appln_nr', ''))
                normalized = clean_row(row)
                identity = stable_id(normalized)
                row.update(observation_id=identity, patent_year=year or '', patent_year_status=evidence['patent_year_status'], year_source=evidence.get('year_source') or '', year_evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True), family_id=fid, representative_appln_id=aid, representative_filing_date=rep_date, selection_status=status)
                if year is None:
                    undated.writerow(row)
                    counts['undated_rows'] += 1
                    continue
                if status == 'outside_years':
                    excluded.writerow(row)
                    counts['outside_years'] += 1
                    continue
                if status != 'selected':
                    excluded.writerow(row)
                    counts['excluded_rows'] += 1
                    continue
                if row['appln_id'] != aid:
                    row['selection_status'] = 'later_application'
                    excluded.writerow(row)
                    counts['later_application_rows'] += 1
                    continue
                key = tuple((normalized[field] for field in ADDRESS_KEY_FIELDS))
                if identity in seen and seen[identity] != key:
                    raise ValueError('Observation identifier collision')
                if identity not in seen:
                    addresses.writerow(row)
                    seen[identity] = key
                    counts['unique_addresses'] += 1
                links.writerow(row)
                counts['eligible_links'] += 1
        with paths[5].open('x', encoding='utf-8') as handle:
            created.append(paths[5])
            json.dump(dict(schema_version='regpat_intake_manifest_v1', source=str(source.resolve()),
                           start_year=start_year, end_year=end_year, counts=counts,
                           application_master=str(Path(application_master).resolve()) if application_master else None,
                           application_master_release=master_identity.get('release_id') if master_identity else None,
                           date_policy='exact canonical application dates; no fallback for missing/conflicting canonical rows' if application_master else 'PATSTAT/OPS date resolution',
                           source_databases_read_only=True, metadata_cache=str(cache_path.resolve()) if metadata_cache else None, metadata_cache_source=json.loads(encoded), metadata_cache_backup=str(backup_path.resolve()) if backup_path else None), handle, indent=2)
            handle.write('\n')
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return counts

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--patstat-db', type=Path)
    parser.add_argument('--ops-cache-db', type=Path)
    parser.add_argument('--family-backbone', type=Path)
    parser.add_argument('--application-master', type=Path, required=True, help='Verified canonical Parquet master; exact application filing dates take precedence, including missing/conflicting records')
    parser.add_argument('--metadata-cache', type=Path, help='Reusable local SQLite PATSTAT metadata index; source identity must match; retain for subsequent intakes')
    parser.add_argument('--metadata-cache-backup', type=Path, help='Durable SQLite backup restored when the local index is absent; checkpointed every million source rows and at each pass end')
    parser.add_argument('--start-year', type=int, default=2000)
    parser.add_argument('--end-year', type=int, required=True)
    args = parser.parse_args(argv)
    print(prepare_cohort(args.input, args.output, patstat_db=args.patstat_db, ops_db=args.ops_cache_db, family_backbone=args.family_backbone, start_year=args.start_year, end_year=args.end_year, application_master=args.application_master, metadata_cache=args.metadata_cache, metadata_cache_backup=args.metadata_cache_backup))
if __name__ == '__main__':
    main()
