"""Run-owned local copies, incremental cache save-back and durable batch publication"""
from pathlib import Path
import os, json, sqlite3, shutil, filecmp, concurrent.futures, datetime
from contextlib import closing

def load(p):
    return json.loads(Path(p).read_text())

def save(p, v):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix(p.suffix + '.tmp')
    t.write_text(json.dumps(v, indent=2) + '\n')
    t.replace(p)

def identity(p):
    p = Path(p)
    s = p.stat()
    return dict(path=str(p.resolve()), size=s.st_size, mtime_ns=s.st_mtime_ns)

# Read retained baseline rows and their observation-level replacements without copying them

# Copy to a temporary file and compare every byte before publishing the destination
def verified_copy(source, target):
    source, target = (Path(source), Path(target))
    before = identity(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt = target.with_name(target.name + '.copy.json')
    if receipt.exists():
        r = load(receipt)
        if not (r['source'] == before and r['target'] == identity(target)):
            raise ValueError('Cache or published output verification failed')
        return target
    tmp = target.with_name(target.name + '.partial')
    shutil.copy2(source, tmp)
    if not (identity(source) == before and filecmp.cmp(source, tmp, shallow=False)):
        raise ValueError('Source/copy bytes differ')
    tmp.replace(target)
    os.chmod(target, 292)
    save(receipt, dict(source=before, target=identity(target), all_bytes_compared=True))
    return target

def copy_cache(source, target, *, copy_bodies=False):
    source, target = (Path(source), Path(target))
    target.mkdir(parents=True, exist_ok=True)
    db = target / 'nominatim-cache.sqlite3'
    receipt = target / 'seed.json'
    if receipt.exists():
        return
    if not not db.exists():
        raise ValueError('Preserve an incomplete seed before choosing a new cache directory')
    with closing(sqlite3.connect((source / db.name).as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(db)) as dst:
        src.backup(dst, pages=8192)
        dst.execute('pragma journal_mode=DELETE')
        if not dst.execute('pragma quick_check').fetchone() == ('ok',):
            raise ValueError('Cache or published output verification failed')
        if not src.execute('select count(*) from responses').fetchone() == dst.execute('select count(*) from responses').fetchone():
            raise ValueError('Cache or published output verification failed')
        dst.execute('attach database ? as original', [(source / db.name).as_uri() + '?mode=ro'])
        for table in ['responses', 'geographic_bodies']:
            if not dst.execute('select count(*) from (select * from ' + table + ' except select * from original.' + table + ')').fetchone() == (0,):
                raise ValueError('Cache or published output verification failed')
            if not dst.execute('select count(*) from (select * from original.' + table + ' except select * from ' + table + ')').fetchone() == (0,):
                raise ValueError('Cache or published output verification failed')
    files = [p for p in (source / 'raw').rglob('*') if p.is_file()] if copy_bodies and (source / 'raw').exists() else []

    def cp(p):
        q = target / p.relative_to(source)
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
        if not filecmp.cmp(p, q, shallow=False):
            raise ValueError('Cache or published output verification failed')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(cp, files))
    save(receipt, dict(source=str(source), target=str(target), sqlite_tables_equal=True, bodies_byte_compared=len(files), raw_body_policy='Raw bodies are derived from the complete preserved SQLite payloads; materialize on use and checkpoint referenced files'))

# Append missing compatible responses; preserve existing payloads, URLs and acquisition times

# Save cache progress under the single-writer lock
def mirror_cache(local, durable, *, response_ledger=None, connection=None):
    local, durable = (Path(local), Path(durable))
    durable.mkdir(exist_ok=True, parents=True)
    marker = durable / 'mirror-state.json'
    water = load(marker) if marker.exists() else dict(response_rowid=0, body_id=0)
    target = durable / 'nominatim-cache.sqlite3'
    if not target.exists():
        copy_cache(local, durable)
        with closing(sqlite3.connect((local / 'nominatim-cache.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            water = dict(response_rowid=db.execute('select coalesce(max(rowid),0) from responses').fetchone()[0], body_id=db.execute('select coalesce(max(file_id),0) from geographic_bodies').fetchone()[0])
    if connection is not None and Path(connection.execute('pragma database_list').fetchone()[2]).resolve() != target.resolve():
        raise ValueError('Checkpoint connection belongs to a different durable cache')
    with closing(sqlite3.connect((local / 'nominatim-cache.sqlite3').as_uri() + '?mode=ro', uri=True)) as src, (connection if connection is not None else closing(sqlite3.connect(target))) as dst:
        dst.execute('pragma synchronous=FULL')
        dst.execute('pragma cache_size=-1048576')
        if not dst.execute('select coalesce(max(rowid),0) from responses').fetchone()[0] >= water['response_rowid']:
            raise ValueError('Cache or published output verification failed')
        if not dst.execute('select coalesce(max(file_id),0) from geographic_bodies').fetchone()[0] >= water['body_id']:
            raise ValueError('Cache or published output verification failed')
        latest = src.execute('select coalesce(max(rowid),0) from responses').fetchone()[0]
        body = src.execute('select coalesce(max(file_id),0) from geographic_bodies').fetchone()[0]
        for key, url, payload, created in src.execute('select cache_key,url,payload,created_at_utc from responses where rowid>? order by rowid', (water['response_rowid'],)):
            old = dst.execute('select url,payload,created_at_utc from responses where cache_key=?', (key,)).fetchone()
            if not (old is None or old == (url, payload, created)):
                raise ValueError('Cache or published output verification failed')
            dst.execute('insert or ignore into responses values(?,?,?,?)', (key, url, payload, created))
        for number, key in src.execute('select file_id,cache_key from geographic_bodies where file_id>? order by file_id', (water['body_id'],)):
            old = dst.execute('select cache_key from geographic_bodies where file_id=?', (number,)).fetchone()
            if not (old is None or old == (key,)):
                raise ValueError('Cache or published output verification failed')
            dst.execute('insert or ignore into geographic_bodies values(?,?)', (number, key))
        dst.execute('create table if not exists mirrored_files(file_id integer primary key)')
        if response_ledger:
            import csv
            with Path(response_ledger).open() as f:
                paths = {Path(r['response_path']) for r in csv.DictReader(f)}
            pending = []
            for file in paths:
                if not file.is_relative_to(local / 'raw'):
                    continue
                number = int(file.stem.split('-')[-1])
                if dst.execute('select 1 from mirrored_files where file_id=?', (number,)).fetchone():
                    continue
                pending.append((file, durable / file.relative_to(local), number))
            for parent in {q.parent for _, q, _ in pending}:
                parent.mkdir(parents=True, exist_ok=True)

            def copy_body(item):
                file, q, number = item
                if not q.exists():
                    tmp = q.with_suffix('.partial')
                    shutil.copy2(file, tmp)
                    if not filecmp.cmp(file, tmp, shallow=False):
                        raise ValueError('Cache or published output verification failed')
                    tmp.replace(q)
                elif not filecmp.cmp(file, q, shallow=False):
                    raise ValueError('Existing unsealed body differs; preserve and inspect')
                return (number,)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                verified = list(pool.map(copy_body, pending))
            dst.executemany('insert or ignore into mirrored_files values(?)', verified)
        dst.commit()
        if not src.execute('select count(*) from responses where rowid>?', (water['response_rowid'],)).fetchone() == dst.execute('select count(*) from responses where rowid>?', (water['response_rowid'],)).fetchone():
            raise ValueError('Cache or published output verification failed')
        if not src.execute('select count(*) from geographic_bodies where file_id>?', (water['body_id'],)).fetchone() == dst.execute('select count(*) from geographic_bodies where file_id>?', (water['body_id'],)).fetchone():
            raise ValueError('Cache or published output verification failed')
        if not dst.execute('select coalesce(max(rowid),0) from responses').fetchone()[0] == latest:
            raise ValueError('Cache or published output verification failed')
        if not dst.execute('select coalesce(max(file_id),0) from geographic_bodies').fetchone()[0] == body:
            raise ValueError('Cache or published output verification failed')
    save(marker, dict(response_rowid=latest, body_id=body, checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), original_payloads_and_timestamps_preserved=True))
    return load(marker)

# These are reusable archives; stage06 reads estimated_grid_1km.csv directly
ARCHIVAL_GRID_FILES = ('support_weights_100m.csv', 'postcode_point_neighbourhood_1km.csv', 'polygon_and_other_support_1km.csv')

def decoded_equal(source, stored, *, compression=None):
    """Compare every original byte with a plain or gzip stored artifact"""
    import gzip
    opener = gzip.open if compression == 'gzip' or str(stored).endswith('.gz') else open
    with Path(source).open('rb') as left, opener(stored, 'rb') as right:
        while True:
            a, b = left.read(1024 * 1024), right.read(1024 * 1024)
            if a != b:
                return False
            if not a:
                return True

# Compress large intermediate CSVs losslessly; retire raw files only after decoded equality
def compress_archival_outputs(directory, *, threshold_bytes=1024 * 1024):
    """Compress reusable intermediate CSVs and preserve aggregation inputs"""
    import gzip
    import fcntl
    directory = Path(directory)
    manifest_path = directory / 'final_grid_manifest.json'
    if not manifest_path.exists():
        return {}
    with (directory / '.archive.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = load(manifest_path)
        receipt_path = directory / 'storage-receipt.json'
        receipt = load(receipt_path) if receipt_path.exists() else {'files': {}}
        for name in ARCHIVAL_GRID_FILES:
            if name not in manifest.get('artifacts', {}):
                continue
            entry = manifest['artifacts'][name]
            if entry.get('path_basis') != 'manifest_relative':
                raise ValueError('Archive requires a manifest-relative grid artifact')
            source, stored = directory / name, directory / (name + '.gz')
            if entry.get('compression') == 'gzip' and not source.exists():
                prior = receipt['files'].get(name)
                if not prior or prior.get('stored_identity') != identity(stored):
                    raise ValueError('Compressed archive lacks its verified storage receipt')
                continue
            if not source.is_file() or source.stat().st_size < threshold_bytes:
                continue
            before = identity(source)
            tmp = stored.with_name(stored.name + '.partial')
            fresh = not stored.exists()
            if fresh:
                with source.open('rb') as src, tmp.open('wb') as dst:
                    with gzip.GzipFile(filename='', fileobj=dst, mode='wb', compresslevel=1, mtime=0) as zipped:
                        shutil.copyfileobj(src, zipped, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                if identity(source) != before or not decoded_equal(source, tmp, compression='gzip'):
                    raise ValueError('Archive verification failed; original retained')
                tmp.replace(stored)
            if identity(source) != before or (not fresh and not decoded_equal(source, stored)):
                raise ValueError('Archive verification failed; original retained')
            receipt['files'][name] = dict(original_identity=before, stored_identity=identity(stored), uncompressed_bytes=before['size'], compressed_bytes=stored.stat().st_size, all_bytes_compared=True)
            # Publish the verified receipt and new location before retiring raw bytes
            save(receipt_path, receipt)
            if not (directory / 'manifest-before-storage.json').exists():
                shutil.copy2(manifest_path, directory / 'manifest-before-storage.json')
            entry.update(path=stored.name, compression='gzip', uncompressed_bytes=before['size'])
            manifest['storage_policy'] = 'lossless gzip level1 for reusable large archives; stage06 input stays plain CSV'
            manifest.setdefault('resource_estimate', {})['output_bytes'] = sum((directory / v['path']).stat().st_size for v in manifest['artifacts'].values())
            save(manifest_path, manifest)
            with manifest_path.open('rb') as handle:
                os.fsync(handle.fileno())
            source.unlink()
        return receipt

# Verify the durable batch copy before marking it complete or retiring local working files
def publish_batch(working, destination, mappings):
    working, destination = (Path(working), Path(destination))
    if not not destination.exists():
        raise ValueError('Never overwrite a prior durable batch')
    stage = working.with_name('.' + working.name + '.canonical-publication')
    if not not stage.exists():
        raise ValueError('Preserve and inspect an incomplete publication before retry')
    weight_tables = {*ARCHIVAL_GRID_FILES, 'estimated_grid_1km.csv', 'withheld.csv', 'support_weights_100m.updates.csv'}
    def stage_copy(source, target):
        source = Path(source)
        if source.parent == working / '05_grid' and source.name in weight_tables:
            try:
                os.link(source, target)
                return target
            except OSError:
                pass
        return shutil.copy2(source, target)
    shutil.copytree(working, stage, copy_function=stage_copy)
    replacements = [(str(working), str(destination)), *[(str(a), str(b)) for a, b in mappings]]

    def translate(s):
        for old, new in replacements:
            s = s.replace(old, new)
        return s
    receipts = []
    for p in stage.rglob('*'):
        if not p.is_file() or p.suffix not in {'.json', '.jsonl', '.csv'}:
            continue
        # Weight rows contain identifiers and values, so preserve their exact bytes
        if p.parent == stage / '05_grid' and p.name in weight_tables:
            continue
        if p.name == 'stage_complete.json':
            receipts.append(p.relative_to(stage))
            continue
        if p.suffix == '.json':

            def walk(v):
                if isinstance(v, str):
                    return translate(v)
                if isinstance(v, list):
                    return [walk(x) for x in v]
                if isinstance(v, dict):
                    return {translate(k): walk(x) for k, x in v.items()}
                return v
            v = walk(load(p))
            p.write_text(json.dumps(v, separators=(',', ':'), ensure_ascii=False) + '\n')
        else:
            temp = p.with_suffix(p.suffix + '.relocated')
            with p.open('rb') as src, temp.open('wb') as dst:
                for line in src:
                    for old, new in replacements:
                        line = line.replace(old.encode(), new.encode())
                    dst.write(line)
            temp.replace(p)
    originals = {rel: load(stage / rel) for rel in receipts}
    for rel in receipts:
        (stage / rel).unlink()
    compress_archival_outputs(stage / '05_grid')
    upload = destination.with_name('.' + destination.name + '.upload')
    if not not upload.exists():
        raise ValueError('Cache or published output verification failed')
    shutil.copytree(stage, upload, copy_function=shutil.copy2)
    upload.replace(destination)
    storage_receipt = destination / '05_grid/storage-receipt.json'
    if storage_receipt.exists():
        saved_storage = load(storage_receipt)
        for name, item in saved_storage['files'].items():
            item['stored_identity'] = identity(destination / '05_grid' / (name + '.gz'))
        save(storage_receipt, saved_storage)
    from geographic_uncertainty.retrieve_and_rasterise import _seal_completed

    def refresh(v):
        if isinstance(v, str):
            return translate(v)
        if isinstance(v, list):
            return [refresh(x) for x in v]
        if isinstance(v, dict):
            value = {translate(k): refresh(x) for k, x in v.items()}
            if {'path', 'size', 'mtime_ns'} <= set(value) and Path(value['path']).is_file():
                value.update(identity(value['path']))
            return value
        return v
    for rel, old in originals.items():
        _seal_completed((destination / rel).parent, refresh(old['invocation']))
    for file in (working / '05_grid').glob('*.csv'):
        target = destination / '05_grid' / file.name
        if not target.exists():
            target = target.with_name(target.name + '.gz')
        if not decoded_equal(file, target):
            raise ValueError(('Published weights differ', file.name))
    if (working / '05_sampled/ghsl_values.json').exists():
        if not load(working / '05_sampled/ghsl_values.json') == load(destination / '05_sampled/ghsl_values.json'):
            raise ValueError('Cache or published output verification failed')
    save(destination / 'working-receipts.json', {str(key): value for key, value in originals.items()})
    save(destination / 'publication.json', dict(all_scientific_outputs_preserved=True, path_relocations=replacements, original_working_receipts=str(destination / 'working-receipts.json'), checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()))
