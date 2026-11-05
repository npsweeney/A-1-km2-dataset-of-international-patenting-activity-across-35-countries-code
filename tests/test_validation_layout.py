"""Validation identity, masking isolation and paper-table denominator controls"""
import argparse,csv,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from validation import mask,download_references,build_reference_ids,evaluate_masking
from validation.validate import prepare_matches,de_rassenfosse_locations,run
from figures_tables.paper_outputs import coverage,accepted_coverage,dataset_macros,table_tex,run as paper_run

def table(path,rows):
 with path.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
class LayoutTests(unittest.TestCase):
 def test_masking_table_pools_evaluated_cases_and_writes_no_figure(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)
   metrics=[dict(mask='street',partition='a',cases=10,evaluated=8,support_coverage_1km_evaluated=.75,mean_reference_cell_weight_evaluated=.5),
            dict(mask='street',partition='b',cases=2,evaluated=2,support_coverage_1km_evaluated=.5,mean_reference_cell_weight_evaluated=0),
            dict(mask='postcode',partition='a',cases=10,evaluated=0,support_coverage_1km_evaluated=None,mean_reference_cell_weight_evaluated=None)]
   summary=p/'summary.json';summary.write_text(json.dumps(dict(stage='07/01_internal/evaluate_masks',metrics=metrics)))
   config=p/'config.json';config.write_text(json.dumps(dict(internal_validation=dict(summary=str(summary)))))
   paper_run(config,p/'outputs')
   text=(p/'outputs/masking-validation.tex').read_text()
   self.assertIn('Street & 10 & 70.00 & 40.00',text)
   self.assertIn('Postcode & 0 & -- & --',text)
   self.assertIn('Direct 1 km cell',text)
   self.assertFalse(list((p/'outputs').glob('*.png')))
   manifest=json.loads((p/'outputs/manifest.json').read_text())
   self.assertEqual(manifest['artifacts'],[dict(label='tab:masking-validation',files=['masking-validation.tex','masking-validation.json','masking-macros.tex'])])
   self.assertEqual(json.loads((p/'outputs/masking-validation.json').read_text()),json.loads(summary.read_text()))
   self.assertIn(r'\newcommand{\MaskingStreetInclusion}',(p/'outputs/masking-macros.tex').read_text())
   self.assertIn(r'\newcommand{\MaskingPostcodeCellWeight}',(p/'outputs/masking-macros.tex').read_text())
 def test_masking_table_rejects_duplicate_partitions(self):
  from figures_tables.internal_comparison import write_table
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);row=dict(mask='street',partition='a',cases=1,evaluated=1,support_coverage_1km_evaluated=1,mean_reference_cell_weight_evaluated=0)
   source=p/'summary.json';source.write_text(json.dumps(dict(stage='07/01_internal/evaluate_masks',metrics=[row,row])))
   with self.assertRaisesRegex(ValueError,'Duplicate'):write_table(source,p/'table')
 def test_masks_hide_building_details_truth_and_original_plain_address_ids(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);source=p/'source.csv';rows=[]
   for n in (1,2):rows.append(dict(source_observation_id=f'address:secret-building-{n}',house_number=str(n),street=f'Example road {n}',city='Example',postal_code=f'AB{n}',ctry_code='GB',reg_code='UK1',true_latitude='51',true_longitude='0.1',ground_truth_precision='address',appln_id=str(n),person_id=str(n),actor='inventor',patent_year='2010',sample_partition='holdout'))
   table(source,rows);mask.prepare(source,p/'construction',p/'private-truth')
   for f in (p/'construction').iterdir():
    content=f.read_text();self.assertNotIn('secret-building',content);self.assertNotIn('true_latitude',content);self.assertNotIn('house_number',content)
   with (p/'construction/postcode.csv').open() as f:self.assertTrue(all(r['address']=='' for r in csv.DictReader(f)))
   with self.assertRaisesRegex(ValueError,'outside'):mask.prepare(source,p/'bad',p/'bad/truth')
 def test_masked_group_cannot_span_partitions(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);base=dict(source_observation_id='one',house_number='1',street='Example road',city='Example',postal_code='AB1',ctry_code='GB',reg_code='UK1',true_latitude='51',true_longitude='0.1',ground_truth_precision='address',appln_id='1',person_id='1',actor='inventor',patent_year='2010',sample_partition='calibration')
   table(p/'source.csv',[base,{**base,'source_observation_id':'two','sample_partition':'holdout'}])
   with self.assertRaisesRegex(ValueError,'spans'):mask.prepare(p/'source.csv',p/'masks',p/'truth')
 def test_country_table_unions_ids_and_refuses_unsearched_denominator(self):
  roster=[dict(ctry_code=c,family_id='docdb:1',appln_id='1',observation_id=o) for c,o in [('GB','a'),('GB','a'),('FR','b')]]
  rows=coverage(roster,[dict(observation_id='a',allocation_precision='postcode'),dict(observation_id='b',precision='region')])
  total=next(r for r in rows if r['country']=='All countries');self.assertEqual(total['families'],1);self.assertEqual(total['addresses'],2);self.assertEqual(total['postcode_share_percent'],50)
  with self.assertRaisesRegex(ValueError,'unsearched'):coverage(roster,[dict(observation_id='a',precision='address')])
 def test_unassigned_counts_families_without_any_assigned_address(self):
  roster=[dict(ctry_code=c,family_id=f,appln_id=f,observation_id=o,actor=a) for c,f,o,a in [('GB','docdb:1','a','inventor'),('FR','docdb:1','b','applicant'),('GB','docdb:2','c','applicant')]]
  rows=coverage(roster,[dict(observation_id='a',precision='address'),dict(observation_id='b',precision='unresolved'),dict(observation_id='c',precision='unresolved')])
  indexed={r['country']:r for r in rows}
  self.assertEqual(indexed['All countries']['unassigned_families_count'],1)
  self.assertEqual(indexed['All countries']['unassigned_families_share_percent'],50)
  self.assertAlmostEqual(indexed['All countries']['unresolved_share_percent'],200/3)
  self.assertEqual(indexed['FR']['unassigned_families_share_percent'],100)
  final=coverage(roster,[dict(observation_id='a',precision='address'),dict(observation_id='b',precision='unresolved'),dict(observation_id='c',precision='unresolved')],allocated_family_countries=set())
  self.assertEqual(next(r for r in final if r['country']=='All countries')['unassigned_families_share_percent'],100)
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'table.tex'
   fields=['country','families','unassigned_families_share_percent','addresses','address_share_percent','street_share_percent','postcode_share_percent','coarser_share_percent','unresolved_share_percent']
   table_tex(path,rows,fields)
   self.assertIn(r'\multicolumn{2}{c|}{Patent families}',path.read_text())
   self.assertIn(r'\multicolumn{6}{c}{Addresses}',path.read_text())
   self.assertIn(r'\cline{2-3}\cline{4-9}',path.read_text())
   self.assertIn(r'\begin{tabular}{l|rr|r|rrrr|r}',path.read_text())
   self.assertIn(r'Country & Count & \shortstack{Unassigned\\(\%)} & Count',path.read_text())

 def test_accepted_coverage_splits_postcode_methods_and_accounts_for_every_address(self):
  methods=['observed_coordinate_passthrough','actual_street_geometry_100m_support',*['nominatim_derived_postcode_support']*3,'']
  roster=[dict(ctry_code='GB' if i<3 else 'DE',family_id='docdb:1',appln_id=str(i),observation_id=str(i)) for i in range(6)]
  outcomes=[dict(observation_id=str(i),status='support_ready' if i<5 else 'withheld',support_method=method) for i,method in enumerate(methods)]
  sizes=[dict(observation_id=str(i),status='support_ready',construction_method=method) for i,method in zip(range(2,5),['qualified_osm_postcode_polygon','postcode_1km_centre_flood_fill','postcode_point_single_cell'])]
  rows=accepted_coverage(iter(roster),iter(outcomes),iter(sizes));total=next(r for r in rows if r['country']=='All countries')
  self.assertEqual(total['addresses'],6);self.assertEqual(total['applications'],6);self.assertEqual(total['families'],1)
  self.assertEqual([total[k+'_count'] for k in ['address','street','postcode_boundary','postcode_reverse','unresolved']],[1,1,1,2,1])
  self.assertAlmostEqual(sum(v for k,v in total.items() if k.endswith('_share_percent')),100)
  with self.assertRaisesRegex(ValueError,'construction method'):accepted_coverage(iter(roster),iter(outcomes),iter([]))
  with self.assertRaisesRegex(ValueError,'Duplicate'):accepted_coverage(iter(roster),iter(outcomes),iter(sizes+sizes[:1]))
 def test_coverage_scope_and_macros_use_the_final_family_and_cell_counts(self):
  roster=[dict(ctry_code=c,family_id='docdb:1',appln_id='1',observation_id=o) for c,o in [('GB','one'),('GB','one'),('JP','outside')]]
  rows=coverage(roster,[dict(observation_id='one',precision='address'),dict(observation_id='outside',precision='unresolved')])
  total=next(row for row in rows if row['country']=='All countries')
  self.assertEqual((total['families'],total['applications'],total['addresses']),(1,1,1))
  grid=dict(cells=1234,represented_tl3_regions=['UK1','UK2'],config=dict(filing_years=[2000,2024]))
  with tempfile.TemporaryDirectory() as tmp:
   output=Path(tmp);counts=dataset_macros(rows,grid,output)
   self.assertEqual(counts,dict(DatasetPatentCount=1,DatasetTLThreeRegionCount=2,DatasetGridCellCount=1234))
   self.assertIn('\\newcommand{\\DatasetGridCellCount}{1,234}',(output/'dataset-macros.tex').read_text())
   with self.assertRaisesRegex(ValueError,'distinct'):dataset_macros(rows,{**grid,'represented_tl3_regions':['UK1','UK1']},output)
 def test_country_table_starts_with_total_and_orders_families_without_application_column(self):
  fields=['country','families','addresses']+[k+'_share_percent' for k in ['address','street','postcode_boundary','postcode_reverse','unresolved']]
  rows=[dict(country=c,families=n,addresses=n,**{k:0. for k in fields if k.endswith('_percent')}) for c,n in [('GB',2),('All countries',5),('DE',3)]]
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'table.tex';table_tex(path,rows,fields);text=path.read_text()
   self.assertLess(text.index('All countries &'),text.index('Germany &'));self.assertLess(text.index('Germany &'),text.index('United Kingdom &'))
   self.assertIn('All countries & 5 & 5',text);self.assertEqual(text.split('Germany &')[0].splitlines()[-1],r'\hline')
   self.assertNotIn('applications',text.lower())
   self.assertIn(r'\begin{tabular}{l|rr|rrrr|r}',text)
   self.assertIn(r'\cline{2-3}\cline{4-8}',text)
   self.assertIn('Unassigned',text)
 def test_paper_entry_point_exports_macros_alongside_tables(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)
   table(p/'source.csv',[dict(ctry_code='GB',family_id='docdb:1',appln_id='1',observation_id='one')])
   table(p/'decisions.csv',[dict(observation_id='one',precision='address')])
   (p/'geocoder.json').write_text(json.dumps(dict(partial=False,production_consumable=True)))
   config=dict(country_coverage=dict(source_roster='source.csv',before_geographic_filter=True,geocoder_manifest='geocoder.json',precision_records='decisions.csv'),release_outputs=[],applicant_inventor_grid=dict(activity_manifest='activity.json',boundaries='boundaries.json',compare_roles=True))
   (p/'config.json').write_text(json.dumps(config))
   def saved_grid(options,output):
    output.with_suffix('.json').write_text(json.dumps(dict(cells=12,represented_tl3_regions=['UK1'],config=dict(filing_years=[2000,2024]))))
   with patch('figures_tables.activity_figures.activity_map',side_effect=saved_grid):paper_run(p/'config.json',p/'output')
   proof=json.loads((p/'output/dataset-counts.json').read_text())
   self.assertEqual(proof['counts'],dict(DatasetPatentCount=1,DatasetTLThreeRegionCount=1,DatasetGridCellCount=12))
   self.assertIn('dataset-counts',{item['label'] for item in json.loads((p/'output/manifest.json').read_text())['artifacts']})
 def test_exact_historical_identity_bridge_excludes_ambiguous_number(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);base=dict(appln_id='1',appln_auth='EP',appln_nr='000123',appln_kind='A',appln_filing_date='20100102')
   table(p/'old.csv',[base,{**base,'appln_id':'2','appln_nr':'000999'},{**base,'appln_id':'3','appln_nr':'000999'}]);table(p/'new.csv',[{**base,'appln_id':'101'},{**base,'appln_id':'102','appln_nr':'000999'}])
   build_reference_ids.build(p/'old.csv',p/'new.csv',p/'bridge')
   with (p/'bridge/application_ids.csv').open() as f:rows=list(csv.DictReader(f))
   self.assertEqual(len(rows),1);self.assertEqual(rows[0]['reference_appln_id'],'1');self.assertEqual(rows[0]['regpat_appln_id'],'101');self.assertEqual(rows[0]['appln_filing_date'],'2010-01-02')
 def test_unproved_old_numeric_ids_are_not_default_production_matches(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);table(p/'ids.csv',[dict(regpat_appln_id='1',reference_appln_id='101')]);a=argparse.Namespace(de_rassenfosse_id_map=p/'ids.csv',allow_unverified_id_map=True)
   with self.assertRaisesRegex(ValueError,'Unverified numeric'):de_rassenfosse_locations(a,[],{'1'})

 def test_morrison_incomplete_reference_roster_is_excluded(self):
  import test_validation
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);a=test_validation.ValidationTests().fixture(p);a.morrison_descriptors.write_text(a.morrison_descriptors.read_text().replace('EP0000200|2010|1','EP0000200|2010|0'));run(a)
   with (a.output/'matches.csv').open() as f:self.assertNotIn('2',{r['representative_appln_id'] for r in csv.DictReader(f)})
 def test_reference_role_must_contain_the_actual_source_observation(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);roster=[dict(appln_id='1',actor='inventor',observation_id='source',office='EPO',pub_nbr='100',address='Example',city='',postal_code='',ctry_code='GB')];own=[dict(family_id='docdb:1',representative_appln_id='1',actor='inventor',observation_id='different',patent_year='2010')]
   (p/'descriptor.txt').write_text('pat|yr|wasComplete\nEP100|2010|1\n');(p/'inv.txt').write_text('id|EP100|Name|51,.1|87|high\n');(p/'app.txt').write_text('')
   a=argparse.Namespace(start_year=2000,end_year=2011,morrison_descriptors=p/'descriptor.txt',morrison_inventors=p/'inv.txt',morrison_applicants=p/'app.txt')
   matches,summary=prepare_matches(a,roster,own);self.assertEqual(matches,[]);self.assertEqual(summary['exclusions']['missing_source_observation_identity'],1)
 def test_mask_evaluation_uses_normalized_weights_and_keeps_failed_cases(self):
  import test_validation
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);test_validation.ValidationTests().fixture(p)
   # Fixture runner coordinate is safely inside the accepted street support
   with (p/'runner.csv').open() as f:point=next(csv.DictReader(f))
   truth=[dict(case_id='one',source_observation_id='gold-one',masked_observation_id='street',patent_year='2010',mask='street',sample_partition='holdout',true_latitude=point['observed_latitude'],true_longitude=point['observed_longitude']),dict(case_id='two',source_observation_id='gold-two',masked_observation_id='missing',patent_year='2010',mask='street',sample_partition='holdout',true_latitude=point['observed_latitude'],true_longitude=point['observed_longitude'])]
   table(p/'truth.csv',truth);table(p/'weights.csv',[dict(observation_id='street',patent_year='2010',estimate_id='trial',grid_cell_id='r2768c17878',normalized_weight='1',evidence_type='street')]);allocation=json.loads((p/'allocation.json').read_text());allocation['artifacts']={'estimated_grid_1km.csv':{'path':'weights.csv'}};(p/'allocation.json').write_text(json.dumps(allocation));query=dict(configuration={'endpoint':'http://owned-service'},telemetry={'initial_cache_entries':0,'attempted_requests':4},partial=False);(p/'query.json').write_text(json.dumps(query))
   evaluate_masking.evaluate(p/'allocation.json',p/'truth.csv',p/'result',p/'query.json');metric=json.loads((p/'result/summary.json').read_text())['metrics'][0];self.assertEqual(metric['cases'],2);self.assertEqual(metric['evaluated'],1);self.assertEqual(metric['support_coverage_all_cases'],.5);self.assertEqual(metric['support_coverage_evaluated'],1)
   query['telemetry']['initial_cache_entries']=1;(p/'query.json').write_text(json.dumps(query))
   with self.assertRaisesRegex(ValueError,'fresh cache'):evaluate_masking.evaluate(p/'allocation.json',p/'truth.csv',p/'bad',p/'query.json')
 def test_reference_metadata_catalogue_uses_both_comparison_sources(self):
  self.assertEqual(set(download_references.CATALOG),{'morrison','de-rassenfosse'})
 def test_point_inferred_postcode_mask_retains_route_and_grid_disagreement(self):
  import test_validation
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);test_validation.ValidationTests().fixture(p)
   point=__import__('validation.validate',fromlist=['read']).read(p/'runner.csv')[0]
   truth=dict(case_id='postcode-1',source_observation_id='source',masked_observation_id='postcode',patent_year='2010',mask='postcode',sample_partition='holdout',country_code='GB',true_latitude=point['observed_latitude'],true_longitude=point['observed_longitude'])
   table(p/'truth.csv',[truth,{**truth,'case_id':'postcode-2','masked_observation_id':'unmatched','country_code':'HU'}])
   rows=[dict(observation_id='postcode',patent_year='2010',estimate_id='estimate',grid_cell_id=c,normalized_weight=w,evidence_type='postcode_neighbourhood') for c,w in [('r2768c17878','.25'),('r2768c17879','.75')]]
   table(p/'weights.csv',rows)
   allocation=json.loads((p/'allocation.json').read_text());allocation['artifacts']={'estimated_grid_1km.csv':{'path':'weights.csv'}};(p/'allocation.json').write_text(json.dumps(allocation))
   support=json.loads((p/'support.json').read_text());support['supports'][1]['postcode_construction_method']='postcode_1km_centre_flood_fill';(p/'support.json').write_text(json.dumps(support))
   (p/'query.json').write_text(json.dumps(dict(configuration={'endpoint':'http://owned-service'},telemetry={'initial_cache_entries':0,'attempted_requests':4},partial=False)))
   table(p/'wrong-truth.csv',[{**truth,'masked_observation_id':'NEVER_IN_THIS_RUN'}])
   with self.assertRaisesRegex(ValueError,'absent from'):evaluate_masking.evaluate(p/'allocation.json',p/'wrong-truth.csv',p/'bad-truth',p/'query.json')
   (p/'wrong-query.json').write_text((p/'query.json').read_text())
   with self.assertRaisesRegex(ValueError,'does not belong'):evaluate_masking.evaluate(p/'allocation.json',p/'truth.csv',p/'bad-query',p/'wrong-query.json')
   evaluate_masking.evaluate(p/'allocation.json',p/'truth.csv',p/'result',p/'query.json')
   summary=json.loads((p/'result/summary.json').read_text());metric=summary['metrics'][0]
   self.assertEqual(metric['evaluated'],1);self.assertEqual(metric['cases'],2)
   self.assertEqual(metric['mean_grid_disagreement_1km_evaluated'],.75);self.assertEqual(metric['mean_grid_disagreement_10km_evaluated'],0)
   self.assertEqual(metric['mean_reference_cell_weight_all_cases'],.125)
   counts={r['country_code']:r for r in summary['country_method_counts']}
   self.assertEqual(counts['GB']['construction_method'],'postcode_1km_centre_flood_fill');self.assertEqual(counts['HU']['withheld_or_wrong_route'],1)
if __name__=='__main__':unittest.main()
