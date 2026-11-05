"""Generate manuscript tables and figures from pipeline outputs"""
import argparse, csv, json, re, shutil
from collections import Counter,defaultdict
from pathlib import Path
from validation.validate import read,write,iter_rows,resolve
BUCKETS=('address','street','postcode','coarser','unresolved')
from run import COUNTRIES
COUNTRY_DATA={
 'AT':('Austria', 'AUT'),'AU':('Australia', 'AUS'),'BE':('Belgium', 'BEL'),
 'CA':('Canada', 'CAN'),'CH':('Switzerland', 'CHE'),'CL':('Chile', 'CHL'),
 'CO':('Colombia', 'COL'),'CR':('Costa Rica', 'CRI'),'CZ':('Czechia', 'CZE'),
 'DE':('Germany', 'DEU'),'DK':('Denmark', 'DNK'),'EE':('Estonia', 'EST'),
 'ES':('Spain', 'ESP'),'FI':('Finland', 'FIN'),'FR':('France', 'FRA'),
 'GB':('United Kingdom', 'GBR'),'GR':('Greece', 'GRC'),'HU':('Hungary', 'HUN'),
 'IE':('Ireland', 'IRL'),'IS':('Iceland', 'ISL'),'IT':('Italy', 'ITA'),
 'LT':('Lithuania', 'LTU'),'LU':('Luxembourg', 'LUX'),'LV':('Latvia', 'LVA'),
 'MX':('Mexico', 'MEX'),'NL':('Netherlands', 'NLD'),'NO':('Norway', 'NOR'),
 'NZ':('New Zealand', 'NZL'),'PL':('Poland', 'POL'),'PT':('Portugal', 'PRT'),
 'SE':('Sweden', 'SWE'),'SI':('Slovenia', 'SVN'),'SK':('Slovakia', 'SVK'),
 'TR':('Türkiye', 'TUR'),'US':('United States', 'USA'),
}
COUNTRY_NAMES={code:item[0] for code,item in COUNTRY_DATA.items()}
# Count applications, families and unique addresses separately before geographic filtering
def coverage(roster,decisions,*,countries_scope=COUNTRIES,buckets=BUCKETS,allocated_family_countries=None):
 selected={}
 for r in decisions:
  oid=r['observation_id']
  if oid in selected:raise ValueError('Duplicate precision record')
  if r.get('status') in {'pending','not_processed','unsearched'}:raise ValueError('An unsearched address cannot be labelled unresolved')
  precision=r.get('allocation_precision') or r.get('precision') or r.get('granularity') or ''
  if precision in buckets:bucket=precision
  elif precision in {'locality','region','country'} and 'coarser' in buckets:bucket='coarser'
  else:bucket='unresolved'
  selected[oid]=bucket
 countries=defaultdict(lambda:dict(applications=set(),families=set(),addresses=set(),assigned_families=set()))
 for r in roster:
  country=(r.get('ctry_code') or r.get('country_code') or r.get('country') or '').strip().upper()
  if country and country not in countries_scope:continue
  if not country or not r.get('family_id','').startswith('docdb:') or not r.get('appln_id') or not r.get('observation_id'):raise ValueError('Coverage requires the complete dated DOCDB source roster with address identities')
  if r['observation_id'] not in selected:raise ValueError('Coverage is incomplete: an unsearched address cannot be labelled unresolved')
  for group in (country,'All countries'):
   v=countries[group];v['applications'].add(r['appln_id']);v['families'].add(r['family_id']);v['addresses'].add(r['observation_id'])
   assigned=(r['family_id'],country) in allocated_family_countries if allocated_family_countries is not None else selected[r['observation_id']] not in {'unresolved','coarser'}
   if assigned:v['assigned_families'].add(r['family_id'])
 rows=[]
 for country in sorted(countries,key=lambda c:(c=='All countries',c)):
  v=countries[country];counts=Counter(selected[o] for o in v['addresses']);n=len(v['addresses'])
  row=dict(country=country,applications=len(v['applications']),families=len(v['families']),addresses=n)
  for k in buckets:row[k+'_count']=counts[k];row[k+'_share_percent']=100*counts[k]/n
  row['unassigned_families_count']=len(v['families']-v['assigned_families'])
  row['unassigned_families_share_percent']=100*row['unassigned_families_count']/len(v['families'])
  rows.append(row)
 if not rows:raise ValueError('No in-scope coverage cohort')
 return rows

# Read assigned families from final allocations across both roles
def assigned_family_countries(manifest):
 data=json.loads(manifest.read_text())
 source=resolve(data['artifacts']['family_highest_weight_cell_grid.csv'],manifest.parent)
 return {(r['family_id'],r['country_code'].upper()) for r in iter_rows(source)}


# Split accepted postcode evidence using its saved construction method
def accepted_coverage(roster,outcomes,sizes,*,countries_scope=COUNTRIES,allocated_family_countries=None):
 methods={}
 for row in sizes:
  oid=row['observation_id']
  if oid in methods:raise ValueError('Duplicate postcode size observation')
  methods[oid]=row['construction_method'] if row['status']=='support_ready' else None
 def decisions():
  for row in outcomes:
   if row['status'] not in {'support_ready','withheld'}:raise ValueError('Incomplete geographic outcomes')
   kind='unresolved'
   if row['status']=='support_ready':
    method=row['support_method']
    if method=='observed_coordinate_passthrough':kind='address'
    elif method=='actual_street_geometry_100m_support':kind='street'
    elif method=='nominatim_derived_postcode_support':
     construction=methods.get(row['observation_id'])
     if construction=='qualified_osm_postcode_polygon':kind='postcode_boundary'
     elif construction in {'postcode_1km_centre_flood_fill','postcode_point_single_cell'}:kind='postcode_reverse'
     else:raise ValueError('Accepted postcode lacks a recorded construction method')
    else:raise ValueError('Unknown accepted geographic method')
   yield dict(observation_id=row['observation_id'],allocation_precision=kind)
 return coverage(roster,decisions(),countries_scope=countries_scope,buckets=('address','street','postcode_boundary','postcode_reverse','unresolved'),allocated_family_countries=allocated_family_countries)


def geographic_outcomes(handoff):
 release=json.loads(handoff.read_text())
 for path in release.get('batch_manifests',[str(handoff)]):
  path=Path(path);path=path if path.is_absolute() else handoff.parent/path
  with (path.parent.parent/'04_support/support_outcomes.csv').open(newline='') as handle:
   yield from csv.DictReader(handle)

# Calculate TL2024 polygon areas on the WGS84 ellipsoid
def regional_areas(source,output,*,countries_scope=COUNTRIES):
 import io,zipfile,math,shapefile
 from pyproj import CRS,Geod
 from shapely.geometry import shape
 lookup={item[1]:code for code,item in COUNTRY_DATA.items() if code in countries_scope}
 geod=Geod(ellps='WGS84');rows=[]
 with zipfile.ZipFile(source) as archive:
  member=next(name for name in archive.namelist() if name.endswith('TL3_TL2024.shp'));stem=member[:-4]
  if CRS.from_wkt(archive.read(stem+'.prj').decode()).to_epsg()!=4326:raise ValueError('TL2024 areas require the original WGS84 polygons')
  reader=shapefile.Reader(shp=io.BytesIO(archive.read(member)),shx=io.BytesIO(archive.read(stem+'.shx')),dbf=io.BytesIO(archive.read(stem+'.dbf')),encoding='latin1')
  for item in reader.iterShapeRecords():
   record=item.record.as_dict();country=lookup.get(record['iso3'])
   if country is None:continue
   geometry=shape(item.shape.__geo_interface__)
   if geometry.is_empty or not geometry.is_valid:raise ValueError('Invalid TL2024 polygon')
   area=abs(geod.geometry_area_perimeter(geometry)[0])/1000000
   if not math.isfinite(area) or area<=0:raise ValueError('Invalid TL2024 region area')
   rows.append(dict(tl3_id=record['tl3_id'],country=country,name=record['name_en'] or record['name_or'],area_km2=area))
 if not rows or len({row['tl3_id'] for row in rows})!=len(rows):raise ValueError('Require distinct TL2024 regions')
 rows.sort(key=lambda row:(row['area_km2'],row['tl3_id']))
 summary=dict(source=str(source),classification='TL2024',method='Geodesic polygon area on the WGS84 ellipsoid, including multipart geometry and holes',regions=len(rows),countries=sorted({row['country'] for row in rows}),missing_countries=sorted(set(countries_scope)-{row['country'] for row in rows}),minimum=rows[0],maximum=rows[-1],iceland_2020_supplement_used=False)
 write(output/'oecd-tl3-areas.csv',rows,list(rows[0]))
 (output/'oecd-tl3-areas.json').write_text(json.dumps(summary,indent=2)+'\n')
 (output/'oecd-tl3-area-macros.tex').write_text('\\newcommand{\\OECDTLThreeMinArea}{'+format(rows[0]['area_km2'],',.1f')+'}\n\\newcommand{\\OECDTLThreeMaxArea}{'+format(rows[-1]['area_km2'],',.0f')+'}\n')
 return summary

def latex_escape(value):
 value=str(value)
 for a,b in [('\\',r'\textbackslash{}'),('&',r'\&'),('%',r'\%'),('_',r'\_'),('#',r'\#')]:value=value.replace(a,b)
 return value

def table_tex(path,rows,fields):
 country='country' in fields;family_unassigned='unassigned_families_share_percent' in fields;accepted='postcode_boundary_share_percent' in fields
 headers=({'country':'Country','families':'Patent families','addresses':'Addresses',**{k+'_share_percent':k.title() for k in BUCKETS}} if country else {'file':'Output files','unit':'Unit','contents':'Contents'})
 if accepted:headers.update(address_share_percent='Direct',postcode_boundary_share_percent='Postcode boundary',postcode_reverse_share_percent='Reverse-geocoded postcode',unresolved_share_percent='Unassigned')
 headers['unassigned_families_share_percent']='Unassigned'
 columns=('l|rr|r|rrrr|r' if family_unassigned else 'l|rr|'+'r'*(len(fields)-4)+'|r') if country else r'p{.38\linewidth}p{.18\linewidth}p{.38\linewidth}'
 lines=[r'\begin{tabular}{'+columns+'}' if country else r'\begin{tabularx}{\textwidth}{@{}p{0.38\textwidth}p{0.18\textwidth}X@{}}',r'\hline']
 if country:
  if family_unassigned:lines +=[r' & \multicolumn{2}{c|}{Patent families} & \multicolumn{6}{c}{Addresses} \\',r'\cline{2-3}\cline{4-9}']
  else:lines +=[r' & \multicolumn{2}{c|}{Distinct counts} & \multicolumn{5}{c}{Share of address inputs (\%)} \\',r'\cline{2-3}\cline{4-8}']
 labels=[latex_escape(headers.get(f,f)) for f in fields]
 if accepted:
  for field,label in [('postcode_boundary_share_percent',r'\shortstack{Postcode\\boundary}'),('postcode_reverse_share_percent',r'\shortstack{Reverse-geocoded\\postcode}')]:labels[fields.index(field)]=label
 if family_unassigned:
  for field in ('families','addresses'):labels[fields.index(field)]='Count'
  for field in fields:
   if field.endswith('_share_percent'):
    label=headers.get(field,field)
    if field=='postcode_boundary_share_percent':label=r'Postcode\\boundary'
    elif field=='postcode_reverse_share_percent':label=r'Reverse-geocoded\\postcode'
    labels[fields.index(field)]=r'\shortstack{'+label+r'\\(\%)}'
 lines +=[' & '.join(labels)+r' \\',r'\hline']
 if country:rows=sorted(rows,key=lambda row:(row['country']!='All countries',-row['families'],row['country']))
 for row in rows:
  row={**row,'country':COUNTRY_NAMES.get(row['country'],row['country'])} if country else row
  values=[]
  for f in fields:
   value=f'{row[f]:.2f}' if isinstance(row[f],float) else f'{row[f]:,}' if isinstance(row[f],int) else str(row[f])
   if not country and f=='file':
    values.append(''.join(r'\path{'+part+'}' if re.fullmatch(r'[A-Za-z0-9_./-]+\.(?:csv|json|txt|png|tex)',part) else latex_escape(part) for part in re.split(r'([A-Za-z0-9_./-]+\.(?:csv|json|txt|png|tex))',value)))
   else:values.append(latex_escape(value))
  lines.append(' & '.join(values)+r' \\')
  if country and row['country']=='All countries':lines.append(r'\hline')
 lines +=[r'\hline',r'\end{tabular}' if country else r'\end{tabularx}'];path.write_text('\n'.join(lines)+'\n')

# Write the abstract's family, region and grid counts
def dataset_macros(rows,grid,output):
 total=next(row for row in rows if row['country']=='All countries')
 regions=grid['represented_tl3_regions']
 if not regions or len(set(regions))!=len(regions):raise ValueError('Require distinct represented TL3 regions')
 counts=dict(DatasetPatentCount=total['families'],DatasetTLThreeRegionCount=len(regions),DatasetGridCellCount=grid['cells'])
 if any(not isinstance(n,int) or n<=0 for n in counts.values()):raise ValueError('Require completed positive dataset counts')
 (output/'dataset-macros.tex').write_text(''.join('\\newcommand{\\'+key+'}{'+format(value,',')+'}\n' for key,value in counts.items()))
 (output/'dataset-counts.json').write_text(json.dumps(dict(counts=counts,patent_unit='Distinct DOCDB families in the source-country cohort',grid_unit='Occupied 1 km cells across both full-tier role panels',tl3_unit='Official TL3 polygons containing those cell centres',filing_years=grid['config']['filing_years']),indent=2)+'\n')
 return counts

# Generate the configured manuscript figures and tables
def run(config,output):
 cfg=json.loads(config.read_text());output.mkdir(parents=True,exist_ok=False);artifacts=[];coverage_rows=None
 def path(v):
  p=Path(v);return p if p.is_absolute() else config.parent/p
 if 'regional_areas' in cfg:
  regional_areas(path(cfg['regional_areas']['source']),output)
  artifacts.append(dict(label='oecd-tl3-areas',files=['oecd-tl3-areas.csv','oecd-tl3-areas.json','oecd-tl3-area-macros.tex']))
 if cfg.get('method_flow'):
  shutil.copyfile(Path(__file__).parent/'assets/address-processing-flow.tex',output/'address-processing-flow.tex');artifacts.append(dict(label='fig:address-processing-flow',files=['address-processing-flow.tex']))
 if 'allocation_examples' in cfg:
  from .activity_figures import allocation_examples
  c=dict(cfg['allocation_examples']);c['cases']=[dict(case) for case in c['cases']]
  for case in c['cases']:
   for field in ('support_release','support_inputs','ghsl_values','allocation','source_roster'):case[field]=str(path(case[field]))
  files=allocation_examples(c,output/'allocation-examples')
  artifacts.append(dict(label='fig:street-post-uncert',files=files))
 if 'country_coverage' in cfg:
  c=cfg['country_coverage']
  completed=json.loads(path(c['geocoder_manifest']).read_text())
  if completed.get('partial') or completed.get('production_consumable') is not True:raise ValueError('Country coverage requires a completed geocoder run')
  if c.get('before_geographic_filter') is not True:raise ValueError('Country denominator must precede geographic filtering')
  if 'geographic_handoff' in c:
   assigned=assigned_family_countries(path(c['activity_manifest']))
   with path(c['source_roster']).open(newline='') as roster, path(c['postcode_sizes']).open(newline='') as sizes:
    rows=accepted_coverage(csv.DictReader(roster),geographic_outcomes(path(c['geographic_handoff'])),csv.DictReader(sizes),countries_scope=set(c.get('countries',COUNTRIES)),allocated_family_countries=assigned)
   buckets=('address','street','postcode_boundary','postcode_reverse','unresolved')
  else:
   source=read(path(c['source_roster']));decision_path=path(c['precision_records'])
   if decision_path.suffix=='.jsonl':decisions=[json.loads(line) for line in decision_path.read_text().splitlines() if line.strip()]
   else:decisions=read(decision_path)
   rows=coverage(source,decisions,countries_scope=set(c.get('countries',COUNTRIES)));buckets=BUCKETS
  rows=sorted(rows,key=lambda row:(row['country']!='All countries',-row['families'],row['country']))
  coverage_rows=rows
  write(output/'country-coverage.csv',rows,list(rows[0]));fields=['country','families','unassigned_families_share_percent','addresses']+[k+'_share_percent' for k in buckets];table_tex(output/'country-coverage.tex',rows,fields);artifacts.append(dict(label='tab:country-coverage',files=['country-coverage.csv','country-coverage.tex']))
 if 'postcode_sizes' in cfg:
  from .activity_figures import postcode_sizes
  files=postcode_sizes(path(cfg['postcode_sizes']['source']),output,support_release=path(cfg['postcode_sizes']['support_release']))
  artifacts.append(dict(label='fig:allocation-size-distributions',files=files))
 if 'release_outputs' in cfg:
  rows=list(cfg['release_outputs'])
  if 'postcode_sizes' in cfg and not any('postcode_sizes.csv' in row['file'] for row in rows):
   rows.append(dict(file='street_lengths.csv; postcode_sizes.csv; postcode-area-histogram.csv/.png',unit='Address allocation',contents='Street lengths, postcode areas and histograms',paths=[str(output/name) for name in files]))
  for row in rows:
   for file in row.get('paths',[row.get('path','')]):
    if not file or not path(file).is_file():raise ValueError('A listed release artifact is missing: '+file)
  public=[{k:row[k] for k in ('file','unit','contents')} for row in rows]
  write(output/'release-outputs.csv',public,['file','unit','contents']);table_tex(output/'release-outputs.tex',public,['file','unit','contents']);artifacts.append(dict(label='tab:release-outputs',files=['release-outputs.csv','release-outputs.tex']))
 if 'within_clustering' in cfg:
  from .activity_figures import activity_map
  c=dict(cfg['within_clustering'])
  for field in ('grid','activity_manifest','boundaries'):
   if field in c:c[field]=str(path(c[field]))
  if 'panels' in c:c['panels']={k:str(path(v)) for k,v in c['panels'].items()}
  activity_map(c,output/'within-clustering');artifacts.append(dict(label='fig:within-clustering',files=['within-clustering.csv','within-clustering.json','within-clustering.png']))
 if 'applicant_inventor_grid' in cfg:
  from .activity_figures import activity_map
  c=dict(cfg['applicant_inventor_grid'])
  for field in ('activity_manifest','boundaries'):c[field]=str(path(c[field]))
  activity_map(c,output/'applicant-inventor-grid')
  artifacts.append(dict(label='fig:applicant-inventor-grid',files=['applicant-inventor-grid'+suffix for suffix in ('.csv','.json','.png')]))
  if coverage_rows is not None:
   dataset_macros(coverage_rows,json.loads((output/'applicant-inventor-grid.json').read_text()),output)
   artifacts.append(dict(label='dataset-counts',files=['dataset-macros.tex','dataset-counts.json']))
 if 'regional_inequality' in cfg:raise ValueError('Regional inequality measure and zero-cell/population denominator must be specified before it can be generated')
 if 'internal_validation' in cfg:
  from .internal_comparison import write_table
  write_table(path(cfg['internal_validation']['summary']),output/'masking-validation');artifacts.append(dict(label='tab:masking-validation',files=['masking-validation.tex','masking-validation.json','masking-macros.tex']))
 if 'external_comparison' in cfg:
  from .plot_validation import summarise,plot
  c=cfg['external_comparison'];names=tuple(c['references']);data=summarise(path(c['activity_manifest']),[path(c['references'][n]) for n in names],names=names)
  plot(data,output/'august-regpat-validation');artifacts.append(dict(label='fig:deras-morr',files=['august-regpat-validation.json','august-regpat-validation.png']))
 if not artifacts:raise ValueError('Specify actual table or figure inputs; placeholders are not outputs')
 (output/'manifest.json').write_text(json.dumps(dict(stage='08_figures_tables',inputs=str(config.resolve()),artifacts=artifacts,queries_executed=False),indent=2))
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();run(a.config,a.output_dir)
if __name__=='__main__':main()
