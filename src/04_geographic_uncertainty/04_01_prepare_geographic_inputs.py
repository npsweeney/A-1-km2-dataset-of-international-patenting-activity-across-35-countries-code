"""Prepare geographic routing and patent-year links from geocoded addresses"""
from __future__ import annotations
from uuid import uuid4
import argparse
import csv
from itertools import islice
import json
import math
import sqlite3
import io
import fcntl
from contextlib import nullcontext
from contextlib import closing
from pathlib import Path
from geographic_uncertainty.retrieve_and_rasterise import SUPPORT_SEED_FIELDS, _file_identity, _seal_completed, _verify_completed
from geographic_uncertainty.contracts import COUNTRY_FIELDS, TEMPORAL_FIELDS, temporal_link_key
from geographic_uncertainty.routing import RUNNER_FIELDS, ROUTING_FIELDS, route_rows
SCHEMA = 'patent_geographic_handoff_v1'
SEED_SCHEMA = 'patent_support_seed_v1'
from run import COUNTRIES

# Read the saved geocoder evidence through a reusable local index instead of rescanning ledgers
class _RowIndex:
    """Index source evidence by byte offset for resumable geographic batches"""
    def __init__(self, path, *, source_copies=None):
        self.source_copies = {str(Path(source).resolve()): Path(copy).resolve() for source, copy in (source_copies or {}).items()}
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.path)
            self.db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, identity TEXT, fields TEXT, offset INTEGER, rows INTEGER, complete INTEGER)')
            self.db.execute('CREATE TABLE IF NOT EXISTS records(path TEXT, seq INTEGER, observation_id TEXT, offset INTEGER, length INTEGER, PRIMARY KEY(path,seq)) WITHOUT ROWID')
            self.db.execute('CREATE INDEX IF NOT EXISTS records_observation ON records(path,observation_id,seq)')
            self.db.commit()
        except Exception:
            self.lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()
        self.lock.close()

    def read(self, path, *, fields=None, observation_ids=None, start=0, limit=None, jsonl=False):
        path = Path(path).resolve()
        key, identity = str(path), json.dumps(_file_identity(path), sort_keys=True)
        saved = self.db.execute('SELECT identity,fields,offset,rows,complete FROM files WHERE path=?', (key,)).fetchone()
        if saved and saved[0] != identity:
            raise ValueError('handoff row-index source changed; preserve it and choose a new index')
        physical = self.source_copies.get(str(path), path)
        if physical != path and _file_identity(physical, include_path=False) != _file_identity(path, include_path=False):
            raise ValueError('local evidence copy identity differs from its immutable source')
        with physical.open('rb') as handle:
            if saved:
                names, offset, count, complete = json.loads(saved[1]), *saved[2:]
                handle.seek(offset)
            else:
                names = None if jsonl else next(csv.reader([handle.readline().decode('utf-8-sig')]))
                offset, count, complete = handle.tell(), 0, 0
                self.db.execute('INSERT INTO files VALUES(?,?,?,?,?,0)', (key, identity, json.dumps(names), offset, count))
                self.db.commit()
            if fields is not None and tuple(names or ()) != tuple(fields):
                raise ValueError(f'incorrect fields: {path.name}')
            if not complete:
                # readline(), rather than file iteration, keeps exact record byte offsets,
                # including quoted CSV fields spanning several lines
                reader = None if jsonl else csv.DictReader(iter(lambda: handle.readline().decode('utf-8'), ''), fieldnames=names)
                while True:
                    begin = handle.tell()
                    if jsonl:
                        line = handle.readline()
                        if not line:
                            break
                        if not line.strip():
                            continue
                        row = json.loads(line)
                    else:
                        row = next(reader, None)
                        if row is None:
                            break
                    count += 1
                    self.db.execute('INSERT INTO records VALUES(?,?,?,?,?)', (key, count, row['observation_id'], begin, handle.tell()-begin))
                    if count % 10000 == 0:
                        self.db.execute('UPDATE files SET offset=?,rows=? WHERE path=?', (handle.tell(),count,key))
                        self.db.commit()
                if json.dumps(_file_identity(path), sort_keys=True) != identity:
                    self.db.rollback()
                    raise ValueError('handoff source changed while indexing')
                self.db.execute('UPDATE files SET offset=?,rows=?,complete=1 WHERE path=?', (handle.tell(),count,key))
                self.db.commit()
            if observation_ids is None:
                cursor = self.db.execute('SELECT offset,length FROM records WHERE path=? AND seq>? ORDER BY seq LIMIT ?', (key,start,-1 if limit is None else limit))
                positions = cursor.fetchall()
            else:
                positions = []
                ids = sorted(observation_ids)
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    # Some SQLite versions otherwise choose a full path scan per ID
                    placeholders = ','.join('?' for _ in chunk)
                    positions.extend(self.db.execute(
                        'SELECT offset,length FROM records INDEXED BY records_observation '
                        'WHERE path=? AND observation_id IN (' + placeholders + ') ORDER BY seq',
                        (key, *chunk)))
                positions.sort()
            result = []
            for offset, length in positions:
                handle.seek(offset)
                text = handle.read(length).decode('utf-8')
                row = json.loads(text) if jsonl else next(csv.DictReader(io.StringIO(text, newline=''), fieldnames=names))
                result.append(row)
            return result


def _csv(path, fields=None, *, observation_ids=None, start=0, limit=None, index=None):
    if index is not None:
        return index.read(path, fields=fields, observation_ids=observation_ids, start=start, limit=limit)
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if fields is not None and tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f'incorrect fields: {path.name}')
        return [row for row in islice(reader, start, None if limit is None else start + limit)
                if observation_ids is None or row['observation_id'] in observation_ids]

def _jsonl(path, observation_ids=None, index=None):
    if index is not None:
        return index.read(path, observation_ids=observation_ids, jsonl=True)
    with path.open(encoding='utf-8') as handle:
        rows = (json.loads(line) for line in handle if line.strip())
        return [row for row in rows if observation_ids is None or row['observation_id'] in observation_ids]

def _index(rows, key):
    result = {}
    for row in rows:
        identity = key(row)
        if not identity or identity in result:
            raise ValueError('missing or duplicate source identity')
        result[identity] = row
    return result

def build(*, geocoder_dir: Path, output_dir: Path, temporal_path: Path | None=None, family_backbone: Path | None=None, batch_start: int=0, batch_size: int | None=None, resume: bool=False, input_index: Path | None=None):
    with _RowIndex(input_index) if input_index is not None else nullcontext(None) as index:
        return _build(geocoder_dir=geocoder_dir, output_dir=output_dir, temporal_path=temporal_path,
                      family_backbone=family_backbone, batch_start=batch_start, batch_size=batch_size,
                      resume=resume, index=index)


# Link selected evidence to the complete source roster and filing-year records
def _build(*, geocoder_dir: Path, output_dir: Path, temporal_path: Path | None=None, family_backbone: Path | None=None, batch_start: int=0, batch_size: int | None=None, resume: bool=False, index=None):
    """Retain every observation and actual family/application/year child link"""
    if type(batch_start) is not int or batch_start < 0 or (batch_size is not None and (type(batch_size) is not int or batch_size <= 0)):
        raise ValueError('batch start must be nonnegative and size positive')
    paths = {name: (geocoder_dir / name).resolve(strict=True) for name in ('observations.csv', 'address_links.csv', 'selections.jsonl', 'candidates.jsonl', 'queries.jsonl')}
    identity_paths = dict(paths)
    identity_paths['temporal_source'] = temporal_path if temporal_path is not None else family_backbone
    if identity_paths['temporal_source'] is None:
        raise ValueError('supply either temporal links or the existing family backbone')
    if (geocoder_dir / 'run_manifest.json').is_file():
        identity_paths['run_manifest'] = geocoder_dir / 'run_manifest.json'
        original = json.loads(identity_paths['run_manifest'].read_text()).get('inputs', {}).get('full_source_links')
        if original:
            identity_paths['full_source_links'] = Path(original)
    invocation = dict(mode='handoff', batch_start=batch_start, batch_size=batch_size,
                      inputs={name: _file_identity(path) for name, path in identity_paths.items()})
    if output_dir.exists():
        if resume:
            _verify_completed(output_dir, invocation)
            return json.loads((output_dir / 'handoff.json').read_text())
        raise FileExistsError(output_dir)
    run_id = 'handoff-' + uuid4().hex
    observations = _index(_csv(paths['observations.csv'], start=batch_start, limit=batch_size, index=index), lambda r: r['observation_id'])
    selected_ids = set(observations)
    selections = _index(_jsonl(paths['selections.jsonl'], selected_ids, index=index), lambda r: r['observation_id'])
    candidates = _index(_jsonl(paths['candidates.jsonl'], selected_ids, index=index), lambda r: r['candidate_id'])
    queries = _index(_jsonl(paths['queries.jsonl'], selected_ids, index=index), lambda r: (r['observation_id'], str(r['query_index'])))
    if set(observations) != set(selections):
        raise ValueError('selection/observation coverage mismatch')
    links = _csv(paths['address_links.csv'], observation_ids=selected_ids, index=index)
    link_fields = ('observation_id', 'appln_id', 'person_id', 'actor', 'patent_year', 'patent_year_status')
    if any((any((field not in row for field in link_fields)) for row in links)):
        raise ValueError('source links lack role or filing-year evidence; use current cohort links')
    unlinked = [row for row in links if not row['appln_id']]
    linked = [row for row in links if row['appln_id']]
    if (temporal_path is None) == (family_backbone is None):
        raise ValueError('supply either temporal links or the existing family backbone')
    if temporal_path is not None:
        temporal_path = temporal_path.resolve(strict=True)
        temporal = _csv(temporal_path, TEMPORAL_FIELDS, observation_ids=selected_ids, index=index)
    else:
        family_backbone = family_backbone.resolve(strict=True)
        with closing(sqlite3.connect(family_backbone.as_uri() + '?mode=ro', uri=True)) as db:
            temporal_by_key = {}
            families = {}
            for link in linked:
                app = link['appln_id']
                if app not in families:
                    result = db.execute('SELECT canonical_family_id FROM regpat_family_backbone WHERE appln_id = ?', (app,)).fetchone()
                    families[app] = str(result[0]) if result and result[0] else 'unmatched:' + app
                family = families[app]
                location = 'fle:' + json.dumps([family, link['observation_id'], link['person_id'], link['actor']], ensure_ascii=False, separators=(',', ':'))
                row = dict(family_id=family, location_evidence_id=location, **{field: link[field] for field in link_fields})
                key = temporal_link_key(row)
                if key in temporal_by_key and temporal_by_key[key] != row:
                    raise ValueError('conflicting filing years for the same application/location link')
                temporal_by_key[key] = row
            temporal = list(temporal_by_key.values())
    _index(temporal, temporal_link_key)
    link_keys = {tuple((row[field] for field in link_fields)) for row in linked}
    if link_keys != {tuple((row[field] for field in link_fields)) for row in temporal}:
        raise ValueError('temporal child changed or omitted source application/role/year links')
    linked_observations = {row['observation_id'] for row in linked}
    if {r['observation_id'] for r in temporal} != linked_observations or {row['observation_id'] for row in links} != set(observations):
        raise ValueError('source/temporal links must cover the selected observations')
    for row in temporal:
        year, status = (row['patent_year'], row['patent_year_status'])
        if not (status == 'valid' and year.isdigit() and (1000 <= int(year) <= 9998) or (status == 'missing' and (not year))):
            raise ValueError('invalid filing-year evidence must be quarantined')
    inputs = {name: dict(path=str(path)) for name, path in paths.items()}
    identity_path = temporal_path if temporal_path is not None else family_backbone
    inputs['temporal_links' if temporal_path is not None else 'family_backbone'] = dict(path=str(identity_path))


    full_links = paths['address_links.csv']
    run_manifest = geocoder_dir / 'run_manifest.json'
    full_source_recorded = False
    if run_manifest.is_file():
        original = json.loads(run_manifest.read_text()).get('inputs', {}).get('full_source_links')
        if original:
            full_links = Path(original).resolve(strict=True)
            full_source_recorded = True
    full_rows = _csv(full_links, observation_ids=selected_ids, index=index)
    def name_key(row):
        field = 'inv_name' if row['actor'] == 'inventor' else 'app_name'
        return row['appln_id'], row['actor'], row['observation_id'], row.get(field, '')
    if not {name_key(row) for row in linked} <= {name_key(row) for row in full_rows}:
        raise ValueError('full source links omit a selected name/address')
    runner, seeds, countries = ([], [], [])
    street_objects = {}
    for oid, source in observations.items():
        selection = selections[oid]
        precision = selection.get('allocation_precision')
        ids = selection.get('selected_candidate_ids', [])
        selected = []
        for cid in ids:
            candidate = candidates.get(cid)
            if candidate is None or candidate['observation_id'] != oid:
                raise ValueError('selected candidate does not belong to observation')
            selected.append(candidate)
        objects = {(str(c.get('raw_candidate', {}).get('osm_type')), str(c.get('raw_candidate', {}).get('osm_id'))) for c in selected}
        accepted = precision in {'address', 'street', 'postcode'} and bool(selected)
        reason = selection.get('resolution_status', 'unresolved')
        if accepted and precision == 'postcode':
            # Coordinates of address objects need not coincide. They are only
            # query provenance; the next stage retrieves the postcode itself
            normalize = lambda value: ''.join(x for x in str(value or '').upper() if x.isalnum())
            expected_postcode = normalize(source.get('query_postal_code') or source['postal_code'])
            if (not expected_postcode or any(
                c.get('postcode_match') is not True or
                normalize(c.get('postcode_candidate')) != expected_postcode or
                c.get('tl3_match') is not True or
                str(c.get('raw_candidate', {}).get('address', {}).get('country_code', '')).upper() != source['ctry_code']
                for c in selected)):
                accepted, reason = (False, 'postcode_seed_ambiguous_or_unconfirmed')
        elif accepted and precision == 'street':
            # Retain every confirmed same-road segment, deduplicated by OSM ID
            if any(c.get('road_match') is not True or c.get('tl3_match') is not True
                   or c.get('raw_candidate', {}).get('osm_type') not in {'way', 'relation'}
                   or not c.get('raw_candidate', {}).get('osm_id')
                   or str(c.get('raw_candidate', {}).get('address', {}).get('country_code', '')).upper() != source['ctry_code']
                   for c in selected):
                accepted, reason = False, 'street_objects_unconfirmed'
            else:
                street_objects[oid] = [dict(osm_type=kind, osm_id=ident) for kind, ident in sorted(objects)]
        elif accepted and precision == 'address' and selection.get('coordinate_selection_method') == 'first_qualifying_same_address_result':
            coordinate_id = selection.get('coordinate_candidate_id')
            if not coordinate_id or coordinate_id not in ids:
                raise ValueError('first-result address lacks coordinate candidate provenance')
        elif accepted and (len(objects) != 1 or ('None', 'None') in objects):
            accepted, reason = (False, 'multiple_or_missing_objects_require_retrieval_review')
        coordinate_id = selection.get('coordinate_candidate_id') if precision == 'address' else None
        c = next(c for c in selected if c['candidate_id'] == coordinate_id) if coordinate_id and accepted else selected[0] if selected else {}
        source_postcode = source.get('query_postal_code') or source['postal_code']
        if accepted and precision == 'postcode' and (not source_postcode):
            accepted, reason = (False, 'source_postcode_missing')
        if oid not in linked_observations:
            accepted, reason = (False, 'source_application_id_missing')
        if accepted and (source.get('region_resolution_status') != 'ready' or c.get('tl3_match') is not True):
            raise ValueError('allocatable selection lacks source-region/TL3 agreement')
        q = queries.get((oid, str(c.get('query_index')))) if c else None
        if accepted and q is None:
            raise ValueError('selected query provenance is missing')
        lat, lon = (c.get('lat', ''), c.get('lon', ''))
        if accepted and (not math.isfinite(float(lat)) or not math.isfinite(float(lon)) or (not -90 <= float(lat) <= 90) or (not -180 <= float(lon) <= 180)):
            raise ValueError('invalid selected coordinate')
        if accepted and precision == 'address':
            coordinate = selection.get('selected_coordinate')
            if coordinate is None or tuple(map(float, coordinate)) != (float(lat), float(lon)):
                raise ValueError('address coordinate differs from selected object')
        query_id = json.dumps([oid, c.get('query_index')], ensure_ascii=False, separators=(',', ':')) if q else ''
        row = dict.fromkeys(RUNNER_FIELDS, '')
        row.update(validation_id=oid, observation_id=oid, candidate_id=c.get('candidate_id', ''), route=precision or '', country_code=source['ctry_code'], observed_address=source['address'], observed_road=c.get('road_expected', ''), observed_postcode=source_postcode, observed_locality=source['locality'], observed_region=source['reg_code'], observed_latitude=lat if accepted and precision == 'address' else '', observed_longitude=lon if accepted and precision == 'address' else '', release_id=run_id, contract_version=SCHEMA, decision_status='matched' if accepted else 'withheld', decision_reason=reason, granularity=selection.get('precision', ''), expected_tl3=source.get('resolved_expected_tl3', ''), selected_query_id=query_id, candidate_admin_area_match=bool(accepted), validation_method='oecd_tl3', query_index=c.get('query_index', ''), query_variant=c.get('query_variant', ''), query_rank=c.get('query_rank', ''))
        runner.append(row)
        code = source['ctry_code']
        countries.append(dict(observation_id=oid, ctry_code=code, country_group=code))
        if accepted and precision in {'street', 'postcode'}:
            raw = c['raw_candidate']
            seed = dict.fromkeys(SUPPORT_SEED_FIELDS, '')
            seed.update(observation_id=oid, validation_id=oid, candidate_id=c['candidate_id'], selected_query_id=query_id, query_index=c['query_index'], query_variant=c['query_variant'], query_rank=c['query_rank'], support_route=precision, seed_latitude=lat, seed_longitude=lon, seed_coordinate_role='coarse_support_seed_not_accepted_address_coordinate', seed_status='available', nominatim_place_id=raw.get('place_id', ''), osm_type=raw.get('osm_type', ''), osm_id=raw.get('osm_id', ''), returned_country_code=raw.get('address', {}).get('country_code', '').upper(), returned_postcode=c.get('postcode_candidate', ''), returned_road=c.get('road_candidate', ''), display_name=c.get('display_name', ''), raw_source_address=source['address'], source_road=c.get('road_expected', ''), source_postcode=source_postcode, source_locality=source['locality'])
            seeds.append(seed)
    routing = route_rows(runner, run_id, countries=COUNTRIES)
    output_dir.mkdir(parents=True)

    def write(name, fields, rows):
        path = (output_dir / name).resolve()
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields))
            writer.writeheader()
            writer.writerows(rows)
        return dict(path=str(path), rows=len(rows))
    runner_artifact = write('runner.csv', RUNNER_FIELDS, runner)
    routing_artifact = write('routing.csv', ROUTING_FIELDS, routing)
    country_artifact = write('countries.csv', COUNTRY_FIELDS, countries)
    temporal_artifact = write('temporal.csv', TEMPORAL_FIELDS, temporal)
    seed_artifact = write('seeds.csv', SUPPORT_SEED_FIELDS, seeds)
    unlinked_artifact = write('unlinked_source_rows.csv', tuple(links[0]) if links else link_fields, unlinked)
    manifest = dict(schema_version=SCHEMA, run_id=run_id, inputs=inputs, runner=runner_artifact, routing=routing_artifact, country_groups=country_artifact, temporal_child=temporal_artifact, scope_countries=list(COUNTRIES), population_rows=len(runner))
    manifest.update(unlinked_source_rows=unlinked_artifact, source_link_rows=len(links), unmatched_family_identity='unmatched:<source application ID>')
    manifest['batch'] = dict(start_row=batch_start, row_count=len(observations), limit=batch_size, ordering='source observations.csv row order', observation_ids=list(observations))
    if run_manifest.is_file():manifest['geocoder_manifest'] = dict(path=str(run_manifest.resolve()))
    manifest['source_role_links'] = dict(path=str(full_links), before_geographic_filter=full_source_recorded)
    path = (output_dir / 'handoff.json').resolve()
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    seed_manifest = dict(schema_version=SEED_SCHEMA, artifact_schema_version=SEED_SCHEMA, artifact_fields=list(SUPPORT_SEED_FIELDS), access_classification='restricted', public_export_allowed=False, route_scope=['street', 'postcode'], artifact_path=seed_artifact['path'], row_count=len(seeds))
    seed_manifest['street_objects'] = street_objects
    (output_dir / 'seeds.json').write_text(json.dumps(seed_manifest, indent=2) + '\n')
    _seal_completed(output_dir, invocation)
    return manifest

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geocoder-dir', type=Path, required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument('--temporal-links', type=Path)
    identity.add_argument('--family-backbone', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--batch-start', type=int, default=0, help='Zero-based source observation row')
    parser.add_argument('--batch-size', type=int, help='Maximum observations; use contiguous nonoverlapping batches')
    parser.add_argument('--input-index', type=Path, help='Reusable task-owned local SSD row index; preserve or back it up with the run')
    parser.add_argument('--resume', action='store_true', help='Reuse verified completed outputs')
    parser.add_argument('--run-batches', action='store_true', help='Run existing stages 04–05 in local resumable batches')
    parser.add_argument('--working-dir', type=Path)
    parser.add_argument('--service-identity', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--grid-contract', type=Path)
    parser.add_argument('--ghsl-registry', type=Path)
    parser.add_argument('--durable-cache', type=Path)
    parser.add_argument('--stop-file', type=Path)
    args = parser.parse_args(argv)
    if args.run_batches:
        from geographic_uncertainty.batches import run_batches
        if not all((args.family_backbone,args.working_dir,args.service_identity,args.policy,args.grid_contract,args.ghsl_registry,args.batch_size)):
            parser.error('batch mode requires family backbone, working directory, service identity, policy, grid contract, GHSL registry and batch size')
        result=run_batches(geocoder_dir=args.geocoder_dir,output_dir=args.output_dir,family_backbone=args.family_backbone,batch_size=args.batch_size,working_dir=args.working_dir,service_identity=args.service_identity,policy=args.policy,grid_contract=args.grid_contract,ghsl_registry=args.ghsl_registry,durable_cache=args.durable_cache,stop_file=args.stop_file,resume=args.resume)
        return 0 if result else 75
    build(geocoder_dir=args.geocoder_dir, temporal_path=args.temporal_links, family_backbone=args.family_backbone, output_dir=args.output_dir, batch_start=args.batch_start, batch_size=args.batch_size, resume=args.resume, input_index=args.input_index)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
