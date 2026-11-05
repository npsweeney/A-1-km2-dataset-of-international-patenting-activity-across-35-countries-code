"""Reconcile cached OPS metadata and update the address registry"""
import argparse, csv, datetime, json, sqlite3
from pathlib import Path

# Reconcile cached filing dates by application ID, then refresh addresses and links
def main():
 import duckdb
 from ingest.patent_metadata.resolve_ops_families import saved_date_schema,iter_saved_application_dates
 p=argparse.ArgumentParser();p.add_argument('--master',required=True);p.add_argument('--ops-cache',required=True);p.add_argument('--source-db',required=True);p.add_argument('--output',required=True);p.add_argument('--previous-addresses');p.add_argument('--resume-export',action='store_true');p.add_argument('--release-id',required=True);a=p.parse_args()
 out=Path(a.output)
 def identity(value):
  path=Path(value).resolve(strict=True);st=path.stat();return dict(path=str(path),size=st.st_size,mtime_ns=st.st_mtime_ns)
 previous_release=Path(a.master).resolve().parent/'release.json'
 release=json.loads(previous_release.read_text())
 if release.get('validated') is not True or not release.get('release_id'):raise ValueError('Source master requires a validated release.json')
 request=dict(source_release=identity(previous_release),release_id=a.release_id,master=identity(a.master),ops_cache=identity(a.ops_cache),source_db=identity(a.source_db),previous_addresses=identity(a.previous_addresses) if a.previous_addresses else None)
 if a.resume_export:
  if json.loads((out/'request.json').read_text())!=request:raise ValueError('Refresh sources or release identity changed; use a new output')
  if (out/'release.json').is_file() and json.loads((out/'release.json').read_text()).get('validated') is True:
   print((out/'release.json').read_text());return
 out.mkdir(parents=True,exist_ok=a.resume_export);(out/'request.json').write_text(json.dumps(request,indent=2))
 def status(state,**kw):
  (out/'state.json').write_text(json.dumps(dict(state=state,checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),**kw),indent=2))
 if a.resume_export:
  d=duckdb.connect(str(out/'refresh.duckdb'));d.execute("SET threads=8; SET memory_limit='24GB'");invalid=json.loads((out/'invalid-date-evidence.json').read_text())
  assert d.execute('SELECT count(*) FROM master').fetchone()==d.execute('SELECT count(*) FROM old_master').fetchone()
 else:
  status('reading_saved_evidence')
  s=sqlite3.connect(Path(a.ops_cache).resolve().as_uri()+'?mode=ro',uri=True)
  schema=saved_date_schema(s)
  d=duckdb.connect(str(out/'refresh.duckdb'));d.execute("SET threads=8; SET memory_limit='24GB'")
  d.execute('CREATE TABLE evidence(appln_id VARCHAR,publication_key VARCHAR,filing_date DATE)');batch=[];invalid=[]
  evidence_file=out/'date-evidence.csv'; handle=evidence_file.open('w',newline='');writer=csv.writer(handle);writer.writerow(['appln_id','publication_key','filing_date'])
  for aid,pub,value in iter_saved_application_dates(s):
   try:
    if isinstance(value,str) and len(value)==8 and value.isascii() and value.isdigit():value=f'{value[:4]}-{value[4:6]}-{value[6:]}'
    date=datetime.date.fromisoformat(value)
    if date.year<1800 or date.year>datetime.date.today().year:raise ValueError()
    batch.append((aid,pub,date))
   except (ValueError,TypeError):invalid.append(dict(appln_id=aid,publication_key=pub,value=value))
   if len(batch)>=10000:writer.writerows(batch);batch=[]
  if batch:writer.writerows(batch)
  handle.close();d.execute("INSERT INTO evidence SELECT * FROM read_csv(?,header=true,columns={'appln_id':'VARCHAR','publication_key':'VARCHAR','filing_date':'DATE'})",[str(evidence_file)])
  s.close();(out/'invalid-date-evidence.json').write_text(json.dumps(invalid))
  d.execute('CREATE TABLE old_master AS SELECT * FROM read_parquet(?)',[a.master]);assert d.execute('SELECT count(*)=count(distinct appln_id) FROM old_master').fetchone()[0]
  d.execute('CREATE TABLE date_union AS SELECT appln_id,filing_date FROM evidence UNION SELECT appln_id,filing_date FROM old_master WHERE filing_date IS NOT NULL')
  d.execute('CREATE TABLE dates AS SELECT appln_id,count(DISTINCT filing_date) n,min(filing_date) filing_date FROM date_union GROUP BY appln_id')
  d.execute("""CREATE TABLE master AS SELECT m.* REPLACE(
   CASE WHEN coalesce(e.n,0)>1 THEN NULL WHEN e.n=1 THEN e.filing_date ELSE m.filing_date END AS filing_date,
   CASE WHEN coalesce(e.n,0)>1 THEN NULL WHEN e.n=1 THEN year(e.filing_date) ELSE m.filing_year END AS filing_year,
   m.date_conflict OR coalesce(e.n,0)>1 AS date_conflict,
   CASE WHEN coalesce(e.n,0)>1 THEN 'conflict' WHEN m.filing_date IS NULL AND e.n=1 THEN 'resolved_saved_ops' ELSE m.date_status END AS date_status,
   CASE WHEN m.filing_date IS NULL AND e.n=1 THEN list_append(m.provenance,'saved_ops_refresh') ELSE m.provenance END AS provenance)
   FROM old_master m LEFT JOIN dates e USING(appln_id)""")
  assert d.execute('SELECT count(*) FROM master').fetchone()==d.execute('SELECT count(*) FROM old_master').fetchone()
 def export(table,name):d.execute("COPY "+table+" TO '"+str(out/name).replace("'","''")+"' (FORMAT PARQUET)")
 export('master','regpat2026-application-master.parquet');export('(SELECT e.* FROM evidence e LEFT JOIN old_master m USING(appln_id) WHERE m.appln_id IS NULL)','unlinked-evidence.parquet');export('(SELECT * FROM date_union WHERE appln_id IN (SELECT appln_id FROM dates WHERE n>1))','conflicting-evidence.parquet')
 status('refreshing_source_links')
 if a.resume_export:
  for table in ('source_links','eligible','addresses','previous'):
   d.execute('DROP TABLE IF EXISTS '+table)
 d.execute("ATTACH '"+a.source_db.replace("'","''")+"' AS raw (READ_ONLY)")
 d.execute("""CREATE TABLE source_links AS SELECT s.*,m.filing_date,m.filing_year,m.date_status,m.date_conflict,
  to_json([upper(trim(s.country)),coalesce(s.address,''),coalesce(s.city,''),coalesce(s.postcode,''),coalesce(s.region,'')]) AS address_key
  FROM raw.source s LEFT JOIN master m ON m.appln_id=coalesce(s.appln_id,'SOURCE:'||s.office||':'||s.application_number||':'||s.publication_number)""")
 assert d.execute('SELECT count(*) FROM source_links').fetchone()==d.execute('SELECT count(*) FROM raw.source').fetchone()
 export('source_links','all-source-links.parquet')
 from run import COUNTRIES
 countries=list(COUNTRIES)
 d.execute('CREATE TABLE eligible AS SELECT * FROM source_links WHERE filing_year BETWEEN 2000 AND 2026 AND NOT coalesce(date_conflict,false) AND upper(trim(country)) IN (SELECT unnest(?))',[countries])
 d.execute("CREATE TABLE addresses AS SELECT DISTINCT address_key,upper(trim(country)) country,address,city,postcode,region FROM eligible WHERE trim(coalesce(address,''))!='' OR trim(coalesce(postcode,''))!='' OR trim(coalesce(city,''))!=''")
 export('addresses','unique-addresses.parquet');export('eligible','address-application-links.parquet')
 if a.previous_addresses:
  d.execute('CREATE TABLE previous AS SELECT address_key FROM read_parquet(?)',[a.previous_addresses]);export('(SELECT * FROM addresses ANTI JOIN previous USING(address_key))','new-addresses.parquet')
 else:
  d.execute("CREATE TABLE previous AS SELECT DISTINCT s.address_key FROM source_links s JOIN old_master m ON m.appln_id=coalesce(s.appln_id,'SOURCE:'||s.office||':'||s.application_number||':'||s.publication_number) WHERE m.filing_year BETWEEN 2000 AND 2026 AND NOT coalesce(m.date_conflict,false) AND upper(trim(s.country)) IN (SELECT unnest(?)) AND (trim(coalesce(s.address,''))!='' OR trim(coalesce(s.postcode,''))!='' OR trim(coalesce(s.city,''))!='')",[countries])
  export('(SELECT * FROM addresses ANTI JOIN previous USING(address_key))','new-addresses.parquet')
 export('(SELECT * FROM previous ANTI JOIN addresses USING(address_key))','removed-addresses.parquet')
 fields="appln_id,coalesce(person_id,'') person_id,actor,filing_year patent_year,address,city,postcode postal_code,region reg_code,upper(trim(country)) ctry_code,'address:'||to_json([trim(coalesce(address,'')),trim(coalesce(city,'')),trim(coalesce(postcode,'')),trim(upper(coalesce(country,''))),trim(coalesce(region,''))]) observation_id,source_row_id,office"
 for prefix,condition in [('', 'address_key IN (SELECT address_key FROM addresses)'),('new-', 'address_key IN (SELECT address_key FROM addresses ANTI JOIN previous USING(address_key))')]:
  for name,tail in [('geocoding-input-links.csv','ORDER BY source_row_id'),('geocoding-inputs.csv','QUALIFY row_number() OVER(PARTITION BY address_key ORDER BY source_row_id)=1 ORDER BY address_key')]:
   sql='SELECT '+fields+' FROM eligible WHERE '+condition+' '+tail
   if name=='geocoding-inputs.csv':
    # Select source rows before formatting IDs: the combined window/projection
    # export dropped rows and nullable region values with DuckDB 1.3.2
    d.execute('CREATE OR REPLACE TEMP TABLE selected_geocoding_inputs AS SELECT * FROM eligible WHERE '+condition+' '+tail)
    sql='SELECT '+fields+' FROM selected_geocoding_inputs ORDER BY address_key'
   name=prefix+name
   d.execute("COPY ("+sql+") TO '"+str(out/name).replace("'","''")+"' (FORMAT CSV, HEADER)")
 summary=dict(source_release='REGPAT August 2026',http_requests=0,cache_policy='Existing caches unchanged; reuse cached queries; geocode new-addresses only after previous address registry supplied',family_policy='Application links retained; DOCDB earliest-family cohort selection remains a separate pipeline stage',master_applications=d.execute('SELECT count(*) FROM master').fetchone()[0],newly_dated_applications=d.execute('SELECT count(*) FROM master m JOIN old_master o USING(appln_id) WHERE o.filing_date IS NULL AND m.filing_date IS NOT NULL').fetchone()[0],unresolved_applications=d.execute('SELECT count(*) FROM master WHERE filing_date IS NULL').fetchone()[0],conflicting_applications=d.execute('SELECT count(*) FROM dates WHERE n>1').fetchone()[0],source_rows=d.execute('SELECT count(*) FROM source_links').fetchone()[0],eligible_source_rows=d.execute('SELECT count(*) FROM eligible').fetchone()[0],unique_address_inputs=d.execute('SELECT count(*) FROM addresses').fetchone()[0],new_unique_address_inputs=d.execute('SELECT count(*) FROM addresses ANTI JOIN previous USING(address_key)').fetchone()[0],countries=countries,master=a.master,ops_cache=a.ops_cache,source_db=a.source_db,invalid_values=len(invalid))
 # Validate the release/CSV boundary before publishing its intake identity
 assert d.execute('SELECT count(*) FROM master').fetchone()[0]==d.execute('SELECT count(DISTINCT appln_id) FROM master').fetchone()[0]
 assert d.execute('SELECT count(*) FROM old_master ANTI JOIN master USING(appln_id)').fetchone()[0]==0
 expected=d.execute('SELECT count(*) FROM addresses').fetchone()[0]
 assert d.execute('SELECT count(*),count(DISTINCT observation_id) FROM read_csv(?,all_varchar=true)',[str(out/'geocoding-inputs.csv')]).fetchone()==(expected,expected)
 assert d.execute('SELECT count(*) FROM read_csv(?,all_varchar=true)',[str(out/'geocoding-input-links.csv')]).fetchone()[0]==summary['eligible_source_rows']
 (out/'summary.json').write_text(json.dumps(summary,indent=2));d.close()
 release=dict(release_id=a.release_id,validated=True,source_release=summary['source_release'],master='regpat2026-application-master.parquet',previous_master=a.master,ops_cache=a.ops_cache,source_db=a.source_db,counts=summary,checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
 (out/'release.json').write_text(json.dumps(release,indent=2));status('verified_complete',summary=summary)

if __name__=='__main__':main()
