"""Check the example figure against saved allocations and publication links"""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from figures_tables.activity_figures import allocation_examples

class AllocationExampleTests(unittest.TestCase):
 def fixture(self,p,method='postcode_1km_centre_flood_fill'):
  cells=[dict(cell_id_100m=f'r{r}c{c}',parent_cell_id_1km=f'r0c{c//10}') for r in range(10) for c in range(20)]
  support=dict(observation_id='private-address',support_id='private-support',postcode_construction_method=method,accepted_cells=cells,support_area_m2=2000000,seed_cell_id_1km='r0c0')
  (p/'support.json').write_text(json.dumps(dict(supports=[support],grid_identity=dict(crs='ESRI:54009',origin_x=0,origin_y=1000))))
  (p/'values.json').write_text(json.dumps(dict(values_by_support={'private-support':{c['cell_id_100m']:{'2005':'1'} for c in cells}})))
  (p/'roster.csv').write_text('observation_id,appln_id,patent_year,patent_year_status,office,pub_nbr\nprivate-address,42,2008,valid,EPO,1234567\n')
  (p/'allocation.csv').write_text('observation_id,appln_id,patent_year,grid_cell_id,normalized_weight\nprivate-address,42,2008,r0c0,0.5\nprivate-address,42,2008,r0c1,0.5\n')
  return dict(cases=[dict(observation_id='private-address',appln_id='42',publication='EP1234567',place='Example',postcode='12345',patent_year=2008,support_release=str(p/'support.json'),ghsl_values=str(p/'values.json'),source_roster=str(p/'roster.csv'),allocation=str(p/'allocation.csv'))])
 def test_saved_weights_and_public_summary(self):
  with tempfile.TemporaryDirectory() as tmp,patch('figures_tables.activity_figures.subprocess.run') as render,patch('figures_tables.activity_figures.example_basemap',return_value={'source':'test basemap'}):
   p=Path(tmp);allocation_examples(self.fixture(p),p/'figure');text=(p/'figure.json').read_text();row=json.loads(text)['cases'][0]
   self.assertEqual(row['weight_sum'],1);self.assertEqual(row['cells_1km'],2);self.assertEqual(row['ghsl_epoch'],2005)
   self.assertTrue(row['verified_against_final_allocation']);self.assertNotIn('private-address',text);self.assertNotIn('private-support',text)
   self.assertTrue(row['espacenet_url'].endswith('pn=EP1234567'));render.assert_called_once()
 def test_changed_weights_are_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);config=self.fixture(p);a=p/'allocation.csv';a.write_text(a.read_text().replace('0.5','0.6'))
   with self.assertRaisesRegex(ValueError,'differs from the final allocation'):allocation_examples(config,p/'figure')
 def test_unlinked_publication_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);config=self.fixture(p);config['cases'][0]['publication']='EP7654321'
   with self.assertRaisesRegex(ValueError,'publication differs'):allocation_examples(config,p/'figure')
 def test_missing_mapped_boundary_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);config=self.fixture(p,'qualified_osm_postcode_polygon');s=p/'support.json';j=json.loads(s.read_text());j['supports'][0].update(source_artifacts=dict(query_ledger='ledger'),boundary_provenance=dict(osm_id=1));s.write_text(json.dumps(j))
   (p/'ledger.json').write_text('[]');(p/'inputs.json').write_text(json.dumps(dict(artifacts=[dict(role='ledger',path=str(p/'ledger.json'))])))
   config['cases'][0]['support_inputs']=str(p/'inputs.json')
   with self.assertRaisesRegex(ValueError,'mapped boundary is missing'):allocation_examples(config,p/'figure')

 def test_saved_street_geometry_uses_the_same_weights(self):
  with tempfile.TemporaryDirectory() as tmp,patch('figures_tables.activity_figures.subprocess.run'),patch('figures_tables.activity_figures.example_basemap',return_value={'source':'test basemap'}):
   p=Path(tmp);config=self.fixture(p,'actual_street_geometry_100m_support');support=p/'support.json';j=json.loads(support.read_text());j['supports'][0].update(source_artifacts=dict(street_geometry='road'),selected_osm_objects=[['way','7']],street_length_m=1200);support.write_text(json.dumps(j))
   (p/'road.json').write_text(json.dumps([dict(osm_type='way',osm_id=7,geojson=dict(type='LineString',coordinates=[[0,0],[.01,.01]]))]))
   (p/'inputs.json').write_text(json.dumps(dict(artifacts=[dict(role='road',path=str(p/'road.json'))])))
   config['cases'][0]['support_inputs']=str(p/'inputs.json');allocation_examples(config,p/'figure')
   row=json.loads((p/'figure.json').read_text())['cases'][0];self.assertEqual(row['street_length_km'],1.2);self.assertEqual(row['weight_sum'],1)
