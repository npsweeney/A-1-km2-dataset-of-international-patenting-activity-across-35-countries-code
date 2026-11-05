"""Construct and weight geographic areas in resumable local batches"""
from pathlib import Path
from contextlib import ExitStack, closing
import csv,datetime,fcntl,importlib,json,shutil,signal,sqlite3,time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from . import batch_io as io


def _merge_csv(paths, target):
    fields = None; count = 0
    with target.open('w', newline='') as out:
        for path in paths:
            with Path(path).open(newline='', encoding='utf-8-sig') as source:
                reader = csv.DictReader(source)
                if fields is None:
                    fields = reader.fieldnames; writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
                if reader.fieldnames != fields:
                    raise ValueError('Batch CSV schemas differ: ' + str(path))
                for row in reader:
                    writer.writerow(row); count += 1
    return count


def _preserve(path, directory):
    if path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.rename(directory / (path.name + '-' + str(time.time_ns())))


# Reconcile batch counts and allocation mass
def merge_allocation_audits(audit_paths, run_id, counts):
    audits=[io.load(p) for p in audit_paths]
    if not all(x['all_100m_estimate_mass_one'] and x['all_1km_estimate_mass_one'] for x in audits):
        raise ValueError('A batch failed spatial mass conservation')
    audit=dict(audits[0],run_id=run_id,batch_audits=[str(p) for p in audit_paths])
    for key in ('temporal_links','scoped_temporal_links','skipped_out_of_scope_temporal_links','observations_with_links','estimates','support_rows_100m','final_rows_1km','withheld_rows'):
        audit[key]=sum(x[key] for x in audits)
    audit['country_group_reconciliation']={}
    groups={group for x in audits for group in x['country_group_reconciliation']}
    for group in groups:
        entry={k:sum(x['country_group_reconciliation'].get(group,{}).get(k,0) for x in audits) for k in ('input_links','estimated_links','withheld_links')}
        entry['reconciled']=entry['input_links']==entry['estimated_links']+entry['withheld_links']
        if not entry['reconciled']:raise ValueError('Country-group mass does not reconcile')
        audit['country_group_reconciliation'][group]=entry
    audit['postcode_construction_counts']={}
    for x in audits:
        for key,value in x.get('postcode_construction_counts',{}).items():
            audit['postcode_construction_counts'][key]=audit['postcode_construction_counts'].get(key,0)+value
    audit['scenario_ids']=sorted({v for x in audits for v in x.get('scenario_ids',[])})
    if audit['temporal_links']!=counts['temporal.csv'] or audit['final_rows_1km']!=counts['estimated_grid_1km.csv'] or audit['withheld_rows']!=counts['withheld.csv']:
        raise ValueError('Merged allocation audit differs from its tables')
    return audit


# Check complete, disjoint batch coverage before joining outputs for family aggregation
def reconcile(root, records, total):
    """Merge the complete geographic batches for family aggregation"""
    out = root / '05_grid'; cross_out = root / '04_inputs'; support_out = root / '05_sampled'
    if (root / 'geographic-batches-complete.json').exists():
        from .retrieve_and_rasterise import _verify_completed
        # The summary is written last, outside the sealed allocation directory
        for folder in (cross_out,support_out,out):_verify_completed(folder,io.load(folder/'stage_complete.json')['invocation'])
        return io.load(root / 'geographic-batches-complete.json')
    for path in (out, cross_out, support_out):
        _preserve(path, root / 'preserved-incomplete-merge'); path.mkdir()
    batches = [Path(r['path']) for r in records]
    crosses = [io.load(p / '04_inputs/handoff.json') for p in batches]
    first = io.load(batches[0] / '05_grid/final_grid_manifest.json')
    first_support = io.load(batches[0] / '05_sampled/support_release.json')
    count = 0
    for r in records:
        if r['start'] != count or r['observations'] <= 0:
            raise ValueError('Overlapping or missing batch range')
        count += r['observations']
    if count != total or len({r['path'] for r in records}) != len(records):
        raise ValueError('Batches do not cover the complete observation cohort')
    if any(c['scope_countries'] != crosses[0]['scope_countries'] or
           c['source_role_links'] != crosses[0]['source_role_links'] for c in crosses):
        raise ValueError('Batch country scope or full source roster changed')
    counts = {}
    files = {'estimated_grid_1km.csv': [p/'05_grid/estimated_grid_1km.csv' for p in batches],
             'withheld.csv': [p/'05_grid/withheld.csv' for p in batches],
             'temporal.csv': [Path(c['temporal_child']['path']) for c in crosses],
             'address_links.csv': [p/'source_links.csv' for p in batches],
             'runner.csv': [Path(c['runner']['path']) for c in crosses],
             'routing.csv': [Path(c['routing']['path']) for c in crosses],
             'countries.csv': [Path(c['country_groups']['path']) for c in crosses],
             'postcode_sizes.csv': [p/'04_support/postcode_sizes.csv' for p in batches]}
    for name, paths in files.items():
        target = out/name if name in {'estimated_grid_1km.csv','withheld.csv'} else cross_out/name
        counts[name] = _merge_csv(paths, target)
    if counts['runner.csv'] != total or counts['address_links.csv'] != sum(r['source_links'] for r in records):
        raise ValueError('Merged observation/source-link counts differ')
    cross = dict(crosses[0], run_id='batched-geographic-release', population_rows=total,
                 source_link_rows=counts['address_links.csv'],
                 inputs={**crosses[0]['inputs'], 'address_links.csv': {'path': str(cross_out/'address_links.csv')}},
                 temporal_child=dict(path=str(cross_out/'temporal.csv'), rows=counts['temporal.csv']),
                 batch_manifests=[str(p/'04_inputs/handoff.json') for p in batches])
    cross.pop('batch', None)
    for key,name in [('runner','runner.csv'),('routing','routing.csv'),('country_groups','countries.csv')]:
        cross[key] = dict(path=str(cross_out/name),rows=counts[name])
    io.save(cross_out/'handoff.json',cross)
    meta = {k:first_support[k] for k in ('schema_version','grid_identity','ghsl_source','ghsl_epoch_scope','grid_contract','ghsl_built_assets')}
    meta['batch_evidence']=[dict(support_release=str(p/'05_sampled/support_release.json'),support_inputs=str(p/'04_support/support_inputs.json'),artifact_ledger=str(p/'05_sampled/support_release.json'),failure_ledger=str(p/'05_sampled/ghsl_values.json')) for p in batches]
    meta.update(run_id=cross['run_id'],sealed=True,batch_support_releases=[str(p/'05_sampled/support_release.json') for p in batches])
    with (support_out/'support_release.json').open('w') as out_file:
        out_file.write(json.dumps(meta)[:-1]+',"supports":[');comma=False
        for p in batches:
            release=io.load(p/'05_sampled/support_release.json')
            if release['grid_identity']!=meta['grid_identity']:raise ValueError('Batch grids differ')
            for item in release['supports']:
                if comma:out_file.write(',')
                out_file.write(json.dumps(item));comma=True
        out_file.write(']}\n')
    audit=merge_allocation_audits([p/'05_grid/audit.json' for p in batches],cross['run_id'],counts)
    io.save(out/'audit.json',audit)
    manifest=dict(first,cross_stage_release=dict(path=str(cross_out/'handoff.json'),path_basis='absolute',schema_version=cross['schema_version'],run_id=cross['run_id']),
                  support_release=dict(path=str(support_out/'support_release.json'),path_basis='absolute',schema_version=meta['schema_version'],run_id=cross['run_id'],sealed=True),
                  artifacts={name:dict(path=name,path_basis='manifest_relative',rows=counts.get(name,1)) for name in ('estimated_grid_1km.csv','withheld.csv','audit.json')},
                  batch_allocation_manifests=[str(p/'05_grid/final_grid_manifest.json') for p in batches])
    io.save(out/'final_grid_manifest.json',manifest)
    result=dict(verified=True,observations=total,source_links=counts['address_links.csv'],batches=len(records),
                zero_overlap=True,zero_omissions=True,full_source_roster=cross['source_role_links'],
                postcode_sizes=str(cross_out/'postcode_sizes.csv'),original_batch_outputs_retained=True)
    from .retrieve_and_rasterise import _seal_completed
    for folder in (cross_out,support_out,out):_seal_completed(folder,dict(mode='batch_reconciliation',observations=total))
    io.save(root/'geographic-batches-complete.json',result)
    return result


# Publish each completed batch through its own copy process
def _publish_batch(batch, destination, mappings, record):
    started = time.monotonic()
    io.publish_batch(batch, destination, mappings)
    record = dict(record, source_links_identity=io.identity(destination / 'source_links.csv'))
    io.save(destination / 'complete.json', record)
    shutil.rmtree(batch)
    shutil.rmtree(batch.with_name('.' + batch.name + '.canonical-publication'))
    print('IO_TIMING','publish_batch',round(time.monotonic()-started,3),flush=True)
    return record


# Calculate bounded local batches and publish verified checkpoints
def run_batches(*, geocoder_dir, output_dir, family_backbone, batch_size, working_dir,
                service_identity, policy, grid_contract, ghsl_registry, durable_cache=None,
                stop_file=None, resume=False):
    """Construct and weight local geographic batches with durable checkpoints"""
    from . import prepare_geographic_inputs as handoff
    from .retrieve_and_rasterise import _service, _seal_completed, _verify_completed
    from run import STAGES
    if type(batch_size) is not int or batch_size <= 0:raise ValueError('Batch size must be positive')
    root=Path(output_dir).resolve();work=Path(working_dir).resolve();root.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
    geocoder_dir=Path(geocoder_dir).resolve();cache=work/'geographic-cache';service=io.load(service_identity)
    endpoint,_=_service(service)
    from .retrieve_and_rasterise import check_capacity
    check_capacity(service)
    approval_path=service['capacity_manifest']
    durable=Path(durable_cache or service.get('response_cache_dir') or root/'geographic-cache').resolve()
    if durable==cache or durable.is_relative_to(work):raise ValueError('Durable cache must be separate from the working directory')
    durable.mkdir(parents=True,exist_ok=True)
    paths=[geocoder_dir/name for name in ('observations.csv','address_links.csv','selections.jsonl','candidates.jsonl','queries.jsonl')]
    evidence_paths=list(paths)
    paths.extend(map(Path,[family_backbone,service_identity,policy,grid_contract,ghsl_registry,approval_path]))
    registry=io.load(ghsl_registry)
    for asset in registry.get('assets',[]):
        paths.extend(Path(asset[field]) for field in ('path','archive_path') if asset.get(field))
    boundary=service.get('point_tl3_boundaries',{}).get('path')
    if boundary:paths.append(Path(boundary))
    original_manifest=geocoder_dir/'run_manifest.json'
    if original_manifest.exists():
        paths.append(original_manifest);forward=io.load(original_manifest)
        if forward.get('partial') or forward.get('production_consumable') is False:raise ValueError('Forward geocoding must finish before global batching')
        roster=forward.get('inputs',{}).get('full_source_links')
        if roster:paths.append(Path(roster));evidence_paths.append(Path(roster))
    implementation={Path(__file__),Path(io.__file__)}
    for stage in ('handoff','geographies','sample-ghsl','allocate'):
        module=importlib.import_module(STAGES[stage]);implementation.update(Path(module.__file__).parent.rglob('*.py'))
    request=dict(schema_version='local-geographic-batches-v1',batch_size=batch_size,working_root=str(work),durable_cache=str(durable),
                 inputs=[io.identity(p) for p in paths],implementation=[io.identity(p) for p in sorted(implementation)])
    request_path=root/'geographic-batches-request.json';state_path=root/'geographic-batches-state.json'
    def state(phase,**kw):io.save(state_path,dict(phase=phase,checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),working_root=str(work),durable_cache=str(durable),**kw))
    checkpoint_connection = None
    def checkpoint(*,response_ledger=None):
        nonlocal checkpoint_connection
        # Retain the SQLite page cache while the existing durable-writer lock is held
        target=durable/'nominatim-cache.sqlite3'
        if checkpoint_connection is None and target.exists():
            checkpoint_connection=stack.enter_context(closing(sqlite3.connect(target)))
        started=time.monotonic();value=io.mirror_cache(cache,durable,response_ledger=response_ledger,connection=checkpoint_connection)
        print('IO_TIMING','mirror_cache',round(time.monotonic()-started,3),flush=True);return value
    def command(stage,*args):
        module=importlib.import_module(STAGES[stage]);started=time.monotonic();value=module.main(list(map(str,args)))
        if isinstance(value,int) and value:raise RuntimeError('Stage failed: '+stage)
        print('STAGE_TIMING',stage,list(map(str,args[:1])),round(time.monotonic()-started,3),flush=True)
    stop=[False];previous={}
    def request_stop(*unused):stop[0]=True
    with ExitStack() as stack:
        for path in (root/'.geographic-batches.lock',work/'.batches.lock',durable/'.geographic-writer.lock'):
            handle=stack.enter_context(path.open('a'));fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if request_path.exists():
            if not resume or io.load(request_path)!=request:raise ValueError('Batch inputs, settings or code changed; preserve this run and choose a new output')
        else:
            if any((root/name).exists() for name in ('batches','04_inputs','05_grid','05_sampled')):raise ValueError('Existing outputs have no batch ownership record; preserve them')
            io.save(request_path,request)
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig]=signal.signal(sig,request_stop)
            stack.callback(signal.signal,sig,previous[sig])
        state('preparing_working_inputs')
        if not cache.exists():
            if (durable/'nominatim-cache.sqlite3').exists():io.copy_cache(durable,cache)
            else:cache.mkdir()
        mappings=[(cache,durable)]
        def local_asset(source):
            source=Path(source).resolve()
            if source.stat().st_dev==work.stat().st_dev:return source
            target=io.verified_copy(source,work/'assets'/source.name);mappings.append((target,source));return target
        working_backbone=local_asset(family_backbone)
        working_registry=json.loads(json.dumps(registry))
        for asset in working_registry.get('assets',[]):
            for field in ('path','archive_path'):
                if asset.get(field):asset[field]=str(local_asset(asset[field]))
        registry_copy=work/'ghsl-registry.json'
        if not registry_copy.exists():io.save(registry_copy,working_registry)
        elif io.load(registry_copy)!=working_registry:raise ValueError('Working raster registry changed')
        mappings.append((registry_copy,ghsl_registry))
        service=dict(service,response_cache_dir=str(cache),cache_writer_id='local-geographic-batches',delay_seconds=0)
        if boundary:service['point_tl3_boundaries']={**service['point_tl3_boundaries'],'path':str(local_asset(boundary))}
        if (work/'service.json').exists():
            if io.load(work/'service.json')!=service:raise ValueError('Working service identity changed')
        else:io.save(work/'service.json',service)
        copies={}
        for path in dict.fromkeys(p.resolve() for p in evidence_paths):
            if path.stat().st_dev!=work.stat().st_dev:
                target=work/'evidence'/str(len(copies))/path.name;io.verified_copy(path,target);copies[str(path.resolve())]=str(target)
        state('indexing_saved_evidence')
        with handoff._RowIndex(work/'geographic-inputs.sqlite',source_copies=copies) as index:
            index.read(paths[0],limit=0)
            total=index.db.execute('SELECT rows FROM files WHERE path=?',(str(paths[0].resolve()),)).fetchone()[0]
            unique=index.db.execute('SELECT count(DISTINCT observation_id) FROM records WHERE path=?',(str(paths[0].resolve()),)).fetchone()[0]
            if total!=unique or not total:raise ValueError('Observation identities must be nonempty and unique across all batches')
            records=[];calls=0;budget=int(io.load(policy)['request_budget'])
            publisher=stack.enter_context(ProcessPoolExecutor(max_workers=1,mp_context=get_context('spawn')))
            publication = None
            committed_addresses = 0
            def drain_publication():
                nonlocal publication,committed_addresses
                if publication is not None:
                    record = publication.result()
                    records.append(record)
                    committed_addresses += record['observations']
                    publication = None
            reservations_path=root/'unobserved-request-reservations.json';reservations=io.load(reservations_path) if reservations_path.exists() else []
            inflight=root/'geographic-request-inflight.json'
            if inflight.exists():
                pending=io.load(inflight);directory=Path(pending['response_directory'])
                if (directory/'stage_complete.json').exists():_verify_completed(directory,io.load(directory/'stage_complete.json')['invocation'])
                else:
                    reservations.append(pending);io.save(reservations_path,reservations)
                inflight.unlink()
            calls=sum(r['maximum_attempts'] for r in reservations)
            try:
                for start in range(0,total,batch_size):
                    name='batch-'+str(start).zfill(8);batch=work/'batches'/name;destination=root/'batches'/name;done=destination/'complete.json'
                    if publication is not None and publication.done():
                        drain_publication()
                    if done.exists():
                        record=io.load(done)
                        from .retrieve_and_rasterise import _verify_completed
                        for folder in ('04_inputs','04_requests','04_responses','04_support','05_sampled','05_grid'):
                            directory=destination/folder;_verify_completed(directory,io.load(directory/'stage_complete.json')['invocation'])
                        if record.get('source_links_identity')!=io.identity(destination/'source_links.csv'):raise ValueError('Published source links changed')
                        if record['start']!=start or record['observations']!=min(batch_size,total-start):raise ValueError('Batch range receipt changed')
                        records.append(record);calls+=record['network_requests'];committed_addresses+=record['observations'];continue
                    if stop[0] or stop_file and Path(stop_file).exists():
                        drain_publication()
                        if (cache/'nominatim-cache.sqlite3').exists():checkpoint()
                        state('paused_at_batch_boundary',completed_batches=len(records),next_batch=name);return None
                    if calls>=budget:raise ValueError('Run-wide geographic request budget exhausted; completed batches preserved')
                    if min(shutil.disk_usage(work).free,shutil.disk_usage(root).free)<2*1024**3:raise ValueError('Insufficient disk headroom; completed batches and caches preserved')
                    state('running',batch=name,completed_batches=len(records),completed_addresses=committed_addresses,network_requests=calls)
                    batch.mkdir(parents=True,exist_ok=True)
                    for folder,marker in [('04_inputs','handoff.json'),('04_requests','stage_a_request_manifest.json'),('04_responses','response_ledger_manifest.json'),('04_support','support_inputs.json'),('05_sampled','support_release.json'),('05_grid','final_grid_manifest.json')]:
                        target=batch/folder
                        if target.exists() and (not (target/marker).exists() or folder.startswith('04_') and not (target/'stage_complete.json').exists()):_preserve(target,work/'preserved-incomplete')
                    state('preparing_batch_inputs',batch=name,completed_batches=len(records),completed_addresses=committed_addresses)
                    started=time.monotonic()
                    handoff._build(geocoder_dir=geocoder_dir,output_dir=batch/'04_inputs',family_backbone=working_backbone,batch_start=start,batch_size=batch_size,resume=True,index=index)
                    print('STAGE_TIMING','handoff',round(time.monotonic()-started,3),flush=True)
                    state('acquiring_and_weighting',batch=name,completed_batches=len(records),completed_addresses=committed_addresses)
                    hand=io.load(batch/'04_inputs/handoff.json');ids=set(hand['batch']['observation_ids'])
                    source_rows=index.read(geocoder_dir/'address_links.csv',observation_ids=ids)
                    with (batch/'source_links.csv').open('w',newline='') as f:
                        names=json.loads(index.db.execute('SELECT fields FROM files WHERE path=?',(str((geocoder_dir/'address_links.csv').resolve()),)).fetchone()[0]);writer=csv.DictWriter(f,fieldnames=names);writer.writeheader();writer.writerows(source_rows)
                    p=dict(io.load(policy),request_budget=budget-calls)
                    if not (batch/'policy.json').exists():io.save(batch/'policy.json',p)
                    common=['--runner',batch/'04_inputs/runner.csv','--routing',batch/'04_inputs/routing.csv','--final-evidence',batch/'04_inputs/handoff.json','--grid-contract',grid_contract]
                    command('geographies','prepare',*common,'--support-seed-manifest',batch/'04_inputs/seeds.json','--policy',batch/'policy.json','--output-dir',batch/'04_requests',*(['--resume'] if (batch/'04_requests').exists() else []))
                    maximum=io.load(batch/'04_requests/stage_a_request_manifest.json')['maximum_network_attempts']
                    if not (batch/'04_responses/stage_complete.json').exists():
                        if calls+maximum>budget:raise ValueError('Insufficient remaining run budget for this batch; previous interrupted attempts conservatively reserved')
                        io.save(inflight,dict(batch=name,response_directory=str(batch/'04_responses'),maximum_attempts=maximum))
                    command('geographies','acquire','--requests',batch/'04_requests/support_requests.csv','--request-manifest',batch/'04_requests/stage_a_request_manifest.json','--service-identity',work/'service.json','--output-dir',batch/'04_responses',*(['--resume'] if (batch/'04_responses').exists() else []))
                    if inflight.exists():inflight.unlink()
                    checkpoint(response_ledger=batch/'04_responses/response_ledger.csv')
                    command('geographies','finalize',*common,'--requests',batch/'04_requests/support_requests.csv','--request-manifest',batch/'04_requests/stage_a_request_manifest.json','--responses',batch/'04_responses/response_ledger.csv','--response-manifest',batch/'04_responses/response_ledger_manifest.json','--service-identity',work/'service.json','--cross-stage-release',batch/'04_inputs/handoff.json','--output-dir',batch/'04_support',*(['--resume'] if (batch/'04_support').exists() else []))
                    checkpoint()
                    if not (batch/'05_sampled').exists():command('sample-ghsl','--cross-stage-release',batch/'04_inputs/handoff.json','--ghsl-registry',registry_copy,'--grid-contract',grid_contract,'--support-inputs',batch/'04_support/support_inputs.json','--output-dir',batch/'05_sampled')
                    if not (batch/'05_grid').exists():command('allocate','--cross-stage-release',batch/'04_inputs/handoff.json','--support-release',batch/'05_sampled/support_release.json','--output-dir',batch/'05_grid')
                    audit=io.load(batch/'05_grid/audit.json')
                    if not audit['all_100m_estimate_mass_one'] or not audit['all_1km_estimate_mass_one']:raise ValueError('Batch weights do not conserve one unit')
                    network=io.load(batch/'04_responses/response_ledger_manifest.json')['network_requests'];calls+=network
                    if calls>budget:raise ValueError('Run-wide request budget exceeded')
                    checkpoint()
                    for folder in ('05_sampled','05_grid'):
                        directory=batch/folder
                        if not (directory/'stage_complete.json').exists():_seal_completed(directory,dict(mode='batched_'+folder))
                        else:_verify_completed(directory,io.load(directory/'stage_complete.json')['invocation'])
                    _preserve(destination,root/'preserved-incomplete');_preserve(batch.with_name('.'+batch.name+'.canonical-publication'),work/'preserved-incomplete');_preserve(destination.with_name('.'+destination.name+'.upload'),root/'preserved-incomplete')
                    drain_publication()
                    record=dict(start=start,observations=len(ids),source_links=len(source_rows),network_requests=network,path=str(destination))
                    publication=publisher.submit(_publish_batch,batch,destination,mappings,record)
                drain_publication()
                state('reconciling',completed_batches=len(records))
                result=reconcile(root,records,total);state('complete_verified',request_budget_charged=calls,unobserved_attempts_reserved=sum(r['maximum_attempts'] for r in reservations),**result);return result
            except BaseException:
                try:drain_publication()
                finally:
                    if (cache/'nominatim-cache.sqlite3').exists():checkpoint()
                    state('interrupted',completed_batches=len(records),network_requests=calls)
                raise
