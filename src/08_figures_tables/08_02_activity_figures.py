"""Plot annual patent activity and postcode areas"""
import csv,json,math,re,subprocess
from collections import Counter,defaultdict
from pathlib import Path
from validation.validate import read,write

# Prepare the configured annual activity panels for the R figure renderer
def activity_map(config,output):
 if config.get('compare_roles'):return applicant_inventor_grid(config,output)
 if config.get('compare_tl3'):return tl3_grid_map(config,output)
 grid=json.loads(Path(config['grid']).read_text());extent=config['extent'];start,end=config['filing_years']
 if len(extent)!=4 or start>end or not config.get('region_label'):raise ValueError('Define the example region extent, label and year window')
 if grid.get('resolution_1km')!=1000:raise ValueError('Expected the common 1 km grid')
 rows=[]
 for tier,path in config['panels'].items():
  if tier not in {'direct','direct_street','direct_street_pcdb','direct_street_pcdb_rg'}:raise ValueError('Use a named cumulative stage06 panel, keeping postcode approximations separate')
  counts=defaultdict(float)
  for r in read(path):
   if not start<=int(r['patent_year'])<=end:continue
   m=re.fullmatch(r'r(\d+)c(\d+)',r['grid_cell_id'])
   if not m:raise ValueError('Invalid grid cell')
   rr,cc=map(int,m.groups());x=grid['origin_x']+(cc+.5)*1000;y=grid['origin_y']-(rr+.5)*1000
   value=float(r['activity'])
   if not math.isfinite(value) or value<0:raise ValueError('Invalid fractional patent count')
   if extent[0]<=x<extent[2] and extent[1]<=y<extent[3]:counts[(x,y)]+=value
  for (x,y),value in sorted(counts.items()):rows.append(dict(tier=tier,x=x,y=y,activity=value))
 if not rows:raise ValueError('No activity in the explicitly selected example extent')
 write(output.with_suffix('.csv'),rows,['tier','x','y','activity']);output.with_suffix('.json').write_text(json.dumps(config,indent=2))
 subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(output.with_suffix('.csv')),str(output)],check=True)


# Retain anonymous area estimates and count allocated observations in 1 km² bins
def postcode_sizes(source, output, *, support_release):
 import ijson
 output.mkdir(parents=True,exist_ok=True)
 fields=('country_code','status','reason','construction_method','allocated_area_km2','extent_status','area_cap_km2')
 counts=defaultdict(int)
 with Path(source).open(newline='',encoding='utf-8-sig') as incoming, (output/'postcode_sizes.csv').open('w',newline='') as saved:
  reader=csv.DictReader(incoming)
  if not set(fields)<=set(reader.fieldnames or []):raise ValueError('Missing postcode size fields')
  writer=csv.DictWriter(saved,fieldnames=fields);writer.writeheader()
  for row in reader:
   if row['status']=='support_ready':
    area,cap=float(row['allocated_area_km2']),float(row['area_cap_km2'])
    if not math.isfinite(area+cap) or not 0<area<=cap:raise ValueError('Invalid allocated postcode area')
    counts[row['construction_method'],math.ceil(area)-1]+=1
   writer.writerow({k:row[k] for k in fields})
 if not counts:raise ValueError('No allocated postcode areas')
 rows=[dict(construction_method=method,area_lower_km2=lower,area_upper_km2=lower+1,allocations=n) for (method,lower),n in sorted(counts.items())]
 stem=output/'postcode-area-histogram'
 write(stem.with_suffix('.csv'),rows,['construction_method','area_lower_km2','area_upper_km2','allocations'])
 street_counts=defaultdict(int)
 with Path(support_release).open('rb') as incoming, (output/'street_lengths.csv').open('w',newline='') as saved:
  writer=csv.writer(saved);writer.writerow(['street_length_km'])
  for item in ijson.items(incoming,'supports.item',use_float=True):
   if item.get('support_method')!='actual_street_geometry_100m_support':continue
   length=float(item['street_length_m'])/1000
   if not math.isfinite(length) or length<=0:raise ValueError('Invalid saved street length')
   writer.writerow([length]);street_counts[math.ceil(length)-1]+=1
 if not street_counts:raise ValueError('No saved street lengths')
 street_bins=output/'street-length-histogram.csv'
 write(street_bins,[dict(length_lower_km=k,length_upper_km=k+1,allocations=n) for k,n in sorted(street_counts.items())],['length_lower_km','length_upper_km','allocations'])
 subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(stem.with_suffix('.csv')),str(stem),'postcode_sizes',str(street_bins)],check=True)
 return ['postcode_sizes.csv','street_lengths.csv','street-length-histogram.csv','postcode-area-histogram.csv','postcode-area-histogram.png']


# Compare the same allocated activity at TL3 and 1 km resolution
def tl3_grid_map(config, output, *, render=True):
 if 'locations' in config:
  rows=[];boundaries=[];checks={}
  for i,location in enumerate(config['locations']):
   local={k:v for k,v in config.items() if k!='locations'};local.update(location);local['roles']=['inventors']
   part=output.with_name(output.name+f'-{i+1}')
   tl3_grid_map(local,part,render=False)
   label=location['region_label']
   for row in read(part.with_suffix('.csv')):row.update(place=label,view_width_km=local['width_km']);rows.append(row)
   for row in read(part.with_suffix('.boundaries.private.csv')):row.update(place=label,view_width_km=local['width_km']);boundaries.append(row)
   checks[label]=json.loads(part.with_suffix('.json').read_text())
  write(output.with_suffix('.csv'),rows,list(rows[0]))
  private=output.with_suffix('.boundaries.private.csv');write(private,boundaries,list(boundaries[0]))
  output.with_suffix('.json').write_text(json.dumps(dict(config=config,locations=checks),indent=2)+'\n')
  subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(output.with_suffix('.csv')),str(output),'tl3_grid',str(private),'1'],check=True)
  return
 from pyproj import Transformer
 from shapely.geometry import shape,box,Point
 from shapely.ops import transform
 manifest_path=Path(config['activity_manifest']);manifest=json.loads(manifest_path.read_text());grid=manifest['grid_identity']
 if grid['resolution_1km']!=1000:raise ValueError('Expected the released 1 km grid')
 start,end=config['filing_years'];tier=config['tier']
 if start>end or tier not in {'direct','direct_street','direct_street_pcdb','direct_street_pcdb_rg'}:raise ValueError('Invalid figure cohort')
 project=Transformer.from_crs('EPSG:4326',grid['crs'],always_xy=True)
 cx,cy=project.transform(*config['focus']);half=float(config['width_km'])*500
 if not 0<half<=100000:raise ValueError('Specify a local map extent')
 window=box(cx-half,cy-half,cx+half,cy+half)
 features=json.loads(Path(config['boundaries']).read_text())['features'];regions={}
 inverse=Transformer.from_crs(grid['crs'],'EPSG:4326',always_xy=True)
 west,south=inverse.transform(cx-half,cy-half);east,north=inverse.transform(cx+half,cy+half)
 search=box(min(west,east),min(south,north),max(west,east),max(south,north))
 for feature in features:
  geographic=shape(feature['geometry'])
  if not geographic.intersects(search):continue
  geometry=transform(project.transform,geographic)
  if not geometry.intersects(window) or geometry.intersection(window).area==0:continue
  identity=str(feature['properties'][config['tl3_property']])
  if not identity or identity in regions:raise ValueError('Missing or duplicate TL3 identity')
  regions[identity]=geometry
 if not regions:raise ValueError('No official TL3 regions in map extent')
 bounds=[g.bounds for g in regions.values()];extent=(min(b[0] for b in bounds),min(b[1] for b in bounds),max(b[2] for b in bounds),max(b[3] for b in bounds))
 rows=[];mass={};boundary_rows=[]
 for identity,geometry in sorted(regions.items()):
  clipped=geometry.intersection(window);parts=list(clipped.geoms) if clipped.geom_type=='MultiPolygon' else [clipped]
  for part_number,part in enumerate(parts):
   for ring_number,ring in enumerate([part.exterior,*part.interiors]):
    for x,y in ring.coords:boundary_rows.append(dict(region_id=identity,group=f'{identity}-{part_number}',ring=f'{identity}-{part_number}-{ring_number}',x=(x-cx)/1000,y=(y-cy)/1000))
 roles=config.get('roles',['applicants','inventors'])
 if not roles or any(role not in {'applicants','inventors'} for role in roles):raise ValueError('Invalid figure roles')
 for role in roles:
  entry=Path(manifest['artifacts'][f'{role}_{tier}.csv']['path']);entry=entry if entry.is_absolute() else manifest_path.parent/entry
  cells=defaultdict(float)
  with entry.open(newline='') as handle:
   for row in csv.DictReader(handle):
    if not start<=int(row['patent_year'])<=end:continue
    match=re.fullmatch(r'r(\d+)c(\d+)',row['grid_cell_id'])
    if not match:raise ValueError('Invalid released grid cell')
    r,c=map(int,match.groups());x=grid['origin_x']+(c+.5)*1000;y=grid['origin_y']-(r+.5)*1000
    if not extent[0]<=x<=extent[2] or not extent[1]<=y<=extent[3]:continue
    value=float(row['activity'])
    if not math.isfinite(value) or value<0:raise ValueError('Invalid released activity')
    cells[r,c]+=value
  totals=defaultdict(float);assigned=0.0
  for (r,c),value in sorted(cells.items()):
   x=grid['origin_x']+(c+.5)*1000;y=grid['origin_y']-(r+.5)*1000
   identities=[key for key,geometry in sorted(regions.items()) if geometry.covers(Point(x,y))]
   if not identities:continue
   identity=identities[0];totals[identity]+=value;assigned+=value
   if window.covers(Point(x,y)):
    rows.append(dict(role=role,resolution='1 km',region_id=identity,grid_cell_id=f'r{r}c{c}',x=(x-cx)/1000,y=(y-cy)/1000,activity=value,density=value))
  for identity,geometry in sorted(regions.items()):
   rows.append(dict(role=role,resolution='TL3',region_id=identity,grid_cell_id='',x='',y='',activity=totals[identity],density=totals[identity]/(geometry.area/1000000)))
  assert math.isclose(sum(totals.values()),assigned,rel_tol=1e-10,abs_tol=1e-8)
  mass[role]=dict(assigned_activity=assigned,tl3_activity=sum(totals.values()))
 output.parent.mkdir(parents=True,exist_ok=True)
 write(output.with_suffix('.csv'),rows,['role','resolution','region_id','grid_cell_id','x','y','activity','density'])
 private=output.with_suffix('.boundaries.private.csv');write(private,boundary_rows,['region_id','group','ring','x','y'])
 output.with_suffix('.json').write_text(json.dumps(dict(config=config,region_ids=sorted(regions),mass_checks=mass,tl3_aggregation='Released 1 km cell centres; full-region activity divided by official region area',private_boundary_vertices=str(private)),indent=2)+'\n')
 if render:
  subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(output.with_suffix('.csv')),str(output),'tl3_grid',str(private),str(half/1000)],check=True)

# Compare applicant and inventor totals in each occupied 1 km cell
def applicant_inventor_grid(config,output):
 from pyproj import Transformer
 from shapely.geometry import shape,Point
 from shapely.strtree import STRtree
 manifest_path=Path(config['activity_manifest']);manifest=json.loads(manifest_path.read_text());grid=manifest['grid_identity']
 if grid['resolution_1km']!=1000:raise ValueError('Expected the released 1 km grid')
 start,end=config['filing_years'];tier=config['tier']
 if start>end or tier not in {'direct','direct_street','direct_street_pcdb','direct_street_pcdb_rg'}:raise ValueError('Invalid figure cohort')
 cells=defaultdict(lambda:[0.0,0.0]);totals={}
 for index,role in enumerate(('applicants','inventors')):
  entry=Path(manifest['artifacts'][f'{role}_{tier}.csv']['path']);entry=entry if entry.is_absolute() else manifest_path.parent/entry
  total=0.0
  with entry.open(newline='') as handle:
   for row in csv.DictReader(handle):
    if not start<=int(row['patent_year'])<=end:continue
    value=float(row['activity'])
    if not math.isfinite(value) or value<0:raise ValueError('Invalid released activity')
    cells[row['grid_cell_id']][index]+=value;total+=value
  totals[role]=total
 features=json.loads(Path(config['boundaries']).read_text())['features']
 geometries=[shape(feature['geometry']) for feature in features]
 countries=[str(feature['properties']['iso3']) for feature in features]
 region_ids=[str(feature['properties']['tl3_id']) for feature in features];represented=set()
 tree=STRtree(geometries);inverse=Transformer.from_crs(grid['crs'],'EPSG:4326',always_xy=True)
 rows=[]
 for cell,values in sorted(cells.items()):
  match=re.fullmatch(r'r(\d+)c(\d+)',cell)
  if not match:raise ValueError('Invalid released grid cell')
  r,c=map(int,match.groups());point=Point(*inverse.transform(grid['origin_x']+(c+.5)*1000,grid['origin_y']-(r+.5)*1000))
  hits=tree.query(point,predicate='intersects');represented.update(region_ids[int(i)] for i in hits)
  found=sorted({countries[int(i)] for i in hits})
  country=found[0] if len(found)==1 else 'Border' if found else 'Unassigned'
  rows.append(dict(grid_cell_id=cell,country=country,applicants=values[0],inventors=values[1]))
 for role in totals:assert math.isclose(sum(row[role] for row in rows),totals[role],rel_tol=1e-9,abs_tol=1e-7)
 output.parent.mkdir(parents=True,exist_ok=True)
 write(output.with_suffix('.csv'),rows,['grid_cell_id','country','applicants','inventors'])
 output.with_suffix('.json').write_text(json.dumps(dict(config=config,cells=len(rows),represented_tl3_regions=sorted(represented),plotted_cells=sum(row['country']!='Unassigned' for row in rows),omitted_unassigned=sum(row['country']=='Unassigned' for row in rows),country_cells=dict(Counter(row['country'] for row in rows)),totals=totals,country_assignment='Official TL3 country at the released cell centre; unmatched and border cells retained'),indent=2)+'\n')
 subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(output.with_suffix('.csv')),str(output),'country_grid'],check=True)


# Cache a contextual basemap in the same projection and extent as the saved cells
def example_basemap(cells, grid, cache):
 import numpy as np
 import requests
 from rasterio.io import MemoryFile
 from rasterio.transform import from_bounds
 from rasterio.warp import reproject, transform_bounds, Resampling
 indices=[tuple(map(int,re.fullmatch(r'r(\d+)c(\d+)',c['parent']).groups())) for c in cells]
 xmin=grid['origin_x']+min(c for r,c in indices)*1000;xmax=grid['origin_x']+(max(c for r,c in indices)+1)*1000
 ymin=grid['origin_y']-(max(r for r,c in indices)+1)*1000;ymax=grid['origin_y']-min(r for r,c in indices)*1000
 cx=(xmin+xmax)/2;cy=(ymin+ymax)/2;span=max(xmax-xmin,ymax-ymin)*1.35
 bounds=(cx-span/2,cy-span/2,cx+span/2,cy+span/2)
 west,south,east,north=transform_bounds(grid['crs'],'EPSG:4326',*bounds)
 zoom=max(2,min(17,round(math.log2(40075016*math.cos(math.radians((south+north)/2))*500/(256*span)))))
 n=2**zoom;world=20037508.342789244;tile_width=2*world/n
 left,bottom,right,top=transform_bounds(grid['crs'],'EPSG:3857',*bounds)
 columns=range(math.floor((left+world)/tile_width),math.floor((right+world)/tile_width)+1)
 rows=range(math.floor((world-top)/tile_width),math.floor((world-bottom)/tile_width)+1)
 if len(columns)*len(rows)>64:raise ValueError('Example basemap exceeds the tile budget')
 size=500;rgb=np.full((3,size,size),255,dtype='uint8');target=from_bounds(*bounds,size,size);cache.mkdir(parents=True,exist_ok=True)
 for row in rows:
  for column in columns:
   path=cache/f'osm-{zoom}-{column}-{row}.png'
   if not path.exists():
    response=requests.get(f'https://tile.openstreetmap.org/{zoom}/{column}/{row}.png',timeout=(10,30),headers={'User-Agent':'patent-code/0.2.2 (+https://github.com/npsweeney/A-1-km2-dataset-of-international-patenting-activity-across-35-countries-code)'})
    response.raise_for_status();path.write_bytes(response.content)
   with MemoryFile(path.read_bytes()) as image, image.open() as tile:
    data=tile.read()
    if data.shape[0]==1:
     palette=np.full((256,4),255,dtype='uint8')
     for key,colour in tile.colormap(1).items():palette[key]=colour
     rgba=palette[data[0]];alpha=rgba[:,:,3:4]/255
     data=(rgba[:,:,:3]*alpha+255*(1-alpha)).astype('uint8').transpose(2,0,1)
    if data.shape[0]<3:raise ValueError('Expected RGB basemap tiles')
    source=from_bounds(-world+column*tile_width,world-(row+1)*tile_width,-world+(column+1)*tile_width,world-row*tile_width,tile.width,tile.height)
    for band in range(3):reproject(data[band],rgb[band],src_transform=source,src_crs='EPSG:3857',dst_transform=target,dst_crs=grid['crs'],init_dest_nodata=False,resampling=Resampling.bilinear)
 grey=(rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722)*.85+255*.15
 rgb=np.repeat(grey[None,:,:],3,axis=0).astype('uint8')
 colours=[['#%02x%02x%02x'%tuple(rgb[:,r,c]) for c in range(size)] for r in range(size)]
 return dict(bounds=bounds,colours=colours,source='© OpenStreetMap contributors',zoom=zoom)


# Plot saved areas and verify their weights against the completed allocation
def allocation_examples(config, output):
 from pyproj import Transformer
 cases=[]; summaries=[];sources={}
 for roster in {case['source_roster'] for case in config['cases']}:
  wanted={(case['observation_id'],case['appln_id'],str(case['patent_year'])) for case in config['cases'] if case['source_roster']==roster}
  with Path(roster).open(newline='') as handle:
   for row in csv.DictReader(handle):
    key=(row['observation_id'],row['appln_id'],row['patent_year'])
    if key in wanted:sources[(roster,*key)]=row;wanted.remove(key)
    if not wanted:break
 for case in config['cases']:
  release=json.loads(Path(case['support_release']).read_text())
  support=next(s for s in release['supports'] if s['observation_id']==case['observation_id'])
  method=support.get('postcode_construction_method',support.get('support_method'))
  if method not in {'actual_street_geometry_100m_support','qualified_osm_postcode_polygon','postcode_1km_centre_flood_fill'}:raise ValueError('Use a street, mapped boundary or multi-cell inferred area')
  year=int(case['patent_year']);epoch=max(e for e in (2000,2005,2010,2015,2020) if e<=year)
  source=sources.get((case['source_roster'],case['observation_id'],case['appln_id'],str(year)))
  if source is None or source['patent_year_status']!='valid':raise ValueError('Example is not linked to a dated source patent')
  publication=('EP'+source['pub_nbr']) if source['office']=='EPO' else source['pct_nbr']
  if publication!=case['publication']:raise ValueError('Example publication differs from its REGPAT source')
  values=json.loads(Path(case['ghsl_values']).read_text())['values_by_support'][support['support_id']]
  cells=[];totals=defaultdict(float)
  for cell in support['accepted_cells']:
   cid=cell['cell_id_100m'];built=float(values[cid][str(epoch)])
   if not math.isfinite(built) or built<0:raise ValueError('Invalid saved built surface')
   cells.append(dict(cell=cid,parent=cell['parent_cell_id_1km'],built=built));totals[cell['parent_cell_id_1km']]+=built
  mass=sum(totals.values())
  if mass<=0:raise ValueError('Example has no built surface')
  expected={cell:built/mass for cell,built in totals.items() if built>0};actual={}
  with Path(case['allocation']).open(newline='') as handle:
   for row in csv.DictReader(handle):
    if row['observation_id']==case['observation_id'] and row['appln_id']==case['appln_id'] and int(row['patent_year'])==year:
     cell=row['grid_cell_id'];weight=float(row['normalized_weight'])
     if cell in actual and not math.isclose(actual[cell],weight,abs_tol=1e-12):raise ValueError('Conflicting saved example weights')
     actual[cell]=weight
  if set(actual)!=set(expected) or any(not math.isclose(actual[cell],expected[cell],abs_tol=1e-10) for cell in actual):raise ValueError('Example differs from the final allocation')
  geometry=None
  if method in {'qualified_osm_postcode_polygon','actual_street_geometry_100m_support'}:
   inputs=json.loads(Path(case['support_inputs']).read_text());artifacts={a['role']:a['path'] for a in inputs['artifacts']}
   ledger=([dict(request_kind='search',response_path=artifacts[support['source_artifacts']['street_geometry']])]
           if method=='actual_street_geometry_100m_support' else json.loads(Path(artifacts[support['source_artifacts']['query_ledger']]).read_text()))
   geometries=[]
   for request in ledger:
    if request['request_kind']!='search' or not request.get('response_path'):continue
    response=json.loads(Path(request['response_path']).read_text())
    payload=response.get('payload',response.get('body',response)) if isinstance(response,dict) else response
    if isinstance(payload,str):payload=json.loads(payload)
    for obj in payload if isinstance(payload,list) else []:
     if method=='actual_street_geometry_100m_support':
      if [obj.get('osm_type'),str(obj.get('osm_id'))] in [[kind,str(ident)] for kind,ident in support['selected_osm_objects']]:geometries.append(obj['geojson'])
     elif str(obj.get('osm_id'))==str(support['boundary_provenance']['osm_id']):geometries.append(obj['geojson'])
   if not geometries:raise ValueError('Saved street geometry or mapped boundary is missing')
   rings=[]
   for geometry in geometries:
    kind=geometry['type'];coordinates=geometry['coordinates']
    if kind=='LineString':rings.append(coordinates)
    elif kind in {'Polygon','MultiLineString'}:rings.extend(coordinates)
    elif kind=='MultiPolygon':rings.extend(ring for polygon in coordinates for ring in polygon)
    else:raise ValueError('Expected saved street or postcode geometry')
   project=Transformer.from_crs('EPSG:4326',release['grid_identity']['crs'],always_xy=True)
   geometry=[dict(x=x,y=y,ring=i) for i,ring in enumerate(rings)
             for x,y in (project.transform(*point[:2]) for point in ring)]
  elif len(cells)!=100*len(totals):raise ValueError('Inferred cells must contain all constituent 100 m cells')
  summary=dict(place=case['place'],postcode=case['postcode'],publication=publication,patent_year=year,ghsl_epoch=epoch,
   method=method,allocated_area_km2=support['support_area_m2']/1e6,cells_100m=len(cells),cells_1km=len(totals),
   positive_weight_cells=len(actual),weight_sum=sum(actual.values()),extent_status=support.get('postcode_extent_status','mapped_polygon' if method=='qualified_osm_postcode_polygon' else 'mapped_street'),
   verified_against_final_allocation=True,espacenet_url='https://worldwide.espacenet.com/patent/search?q=pn='+publication)
  if method=='actual_street_geometry_100m_support':summary['street_length_km']=support['street_length_m']/1000
  basemap=example_basemap(cells,release['grid_identity'],Path(config.get('basemap_cache',output.parent/'basemap-cache')))
  summary['basemap_source']=basemap['source']
  summaries.append(summary);cases.append(dict(summary=summary,grid=release['grid_identity'],cells=cells,weights=actual,geometry=geometry,seed=support.get('seed_cell_id_1km'),basemap=basemap))
 output.parent.mkdir(parents=True,exist_ok=True)
 private=output.with_suffix('.private.json');private.write_text(json.dumps(cases)+'\n')
 output.with_suffix('.json').write_text(json.dumps(dict(cases=summaries,queries_executed=False),indent=2)+'\n')
 subprocess.run(['Rscript',str(Path(__file__).parent/'renderers/activity.R'),str(private),str(output),'allocation_examples'],check=True)
 return [output.name+'.png',output.name+'.json']+[output.name+f'-{i+1}.png' for i in range(len(cases))]
