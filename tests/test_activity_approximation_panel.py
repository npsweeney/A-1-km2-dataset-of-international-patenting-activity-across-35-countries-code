"""The optional approximation-inclusive output must retain its own figure label"""
import csv,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from figures_tables.activity_figures import activity_map, postcode_sizes
from figures_tables.paper_outputs import run as paper_outputs

class ActivityApproximationPanel(unittest.TestCase):
    def test_point_inferred_panel_is_retained_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);grid=p/'grid.json';grid.write_text(json.dumps(dict(resolution_1km=1000,origin_x=0,origin_y=2000)))
            panel=p/'panel.csv';panel.write_text('grid_cell_id,patent_year,activity\nr0c0,2020,0.25\nr0c1,2020,0.75\n')
            tier='direct_street_pcdb_rg';config=dict(grid=str(grid),extent=[0,0,2000,2000],filing_years=[2000,2024],region_label='Example',panels={tier:str(panel)})
            with patch('figures_tables.activity_figures.subprocess.run') as render:
                activity_map(config,p/'map.png');render.assert_called_once()
            with (p/'map.csv').open() as f:rows=list(csv.DictReader(f))
            self.assertEqual({r['tier'] for r in rows},{tier});self.assertEqual(sum(float(r['activity']) for r in rows),1)
            self.assertEqual(json.loads((p/'map.json').read_text()),config)

    def test_tl3_and_grid_figure_preserves_the_same_activity_for_each_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);grid=dict(crs='ESRI:54009',resolution_1km=1000,origin_x=0,origin_y=2000)
            panel=p/'panel.csv';panel.write_text('grid_cell_id,patent_year,activity\nr0c0,2010,0.25\nr0c1,2010,0.75\n')
            manifest=p/'manifest.json';manifest.write_text(json.dumps(dict(grid_identity=grid,artifacts={role+'_direct_street_pcdb_rg.csv':dict(path='panel.csv') for role in ('applicants','inventors')})))
            boundaries=p/'boundaries.json';boundaries.write_text(json.dumps(dict(type='FeatureCollection',features=[dict(type='Feature',properties=dict(tl3_id='TEST'),geometry=dict(type='Polygon',coordinates=[[[-.05,-.05],[.05,-.05],[.05,.05],[-.05,.05],[-.05,-.05]]]))])))
            config=dict(compare_tl3=True,activity_manifest=str(manifest),boundaries=str(boundaries),tl3_property='tl3_id',focus=[0,0],width_km=4,filing_years=[2000,2024],tier='direct_street_pcdb_rg')
            with patch('figures_tables.activity_figures.subprocess.run') as render:
                activity_map(config,p/'map');render.assert_called_once()
            with (p/'map.csv').open() as f:rows=list(csv.DictReader(f))
            for role in ('applicants','inventors'):
                for resolution in ('TL3','1 km'):
                    self.assertAlmostEqual(sum(float(r['activity']) for r in rows if r['role']==role and r['resolution']==resolution),1)
            self.assertIn('mass_checks',json.loads((p/'map.json').read_text()))
            config['locations']=[dict(region_label='First',focus=[0,0]),dict(region_label='Second',focus=[0,0],width_km=6)]
            with patch('figures_tables.activity_figures.subprocess.run') as render:
                activity_map(config,p/'cities');render.assert_called_once()
            with (p/'cities.csv').open() as handle:rows=list(csv.DictReader(handle))
            self.assertEqual({r['role'] for r in rows},{'inventors'})
            self.assertEqual({r['place'] for r in rows},{'First','Second'})
            self.assertEqual({float(r['view_width_km']) for r in rows},{4,6})
            for place in ('First','Second'):
                self.assertAlmostEqual(sum(float(r['activity']) for r in rows if r['place']==place and r['resolution']=='1 km'),1)


    def test_country_grid_keeps_single_role_cells_and_both_role_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);grid=dict(crs='ESRI:54009',resolution_1km=1000,origin_x=0,origin_y=2000)
            (p/'app.csv').write_text('grid_cell_id,patent_year,activity\nr0c0,2010,0.25\nr0c0,2011,0.75\n')
            (p/'inv.csv').write_text('grid_cell_id,patent_year,activity\nr0c1,2010,2\nr0c20,2010,1\n')
            (p/'manifest.json').write_text(json.dumps(dict(grid_identity=grid,artifacts={role+'_direct.csv':dict(path=file) for role,file in [('applicants','app.csv'),('inventors','inv.csv')]})))
            (p/'boundaries.json').write_text(json.dumps(dict(features=[dict(properties=dict(iso3='GBR',tl3_id='TEST'),geometry=dict(type='Polygon',coordinates=[[[-.05,-.05],[.05,-.05],[.05,.05],[-.05,.05],[-.05,-.05]]]))])))
            config=dict(compare_roles=True,activity_manifest=str(p/'manifest.json'),boundaries=str(p/'boundaries.json'),filing_years=[2000,2024],tier='direct')
            with patch('figures_tables.activity_figures.subprocess.run') as render:
                activity_map(config,p/'comparison');render.assert_called_once()
            with (p/'comparison.csv').open() as handle:rows=list(csv.DictReader(handle))
            self.assertEqual(len(rows),3);self.assertEqual({row['country'] for row in rows},{'GBR','Unassigned'})
            self.assertEqual([(float(r['applicants']),float(r['inventors'])) for r in rows],[(1,0),(0,2),(0,1)])
            proof=json.loads((p/'comparison.json').read_text())
            self.assertEqual((proof['plotted_cells'],proof['omitted_unassigned']),(2,1))
            self.assertEqual(proof['represented_tl3_regions'],['TEST'])

    def test_postcode_sizes_keep_reasons_and_count_allocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/'sizes.csv'
            source.write_text('observation_id,country_code,postcode,status,reason,construction_method,allocated_area_km2,extent_status,area_cap_km2\n'
                'one,GB,PRIVATE,support_ready,,postcode_point_single_cell,1,closed_matching_centres,50\n'
                'two,GB,PRIVATE,support_ready,,postcode_point_single_cell,1,closed_matching_centres,50\n'
                'three,DE,PRIVATE,support_ready,,qualified_osm_postcode_polygon,1.01,mapped_polygon,50\n'
                'four,DE,PRIVATE,withheld,area_cap,qualified_osm_postcode_polygon,51,unresolved,50\n')
            support=p/'support.json';support.write_text(json.dumps(dict(supports=[dict(support_method='actual_street_geometry_100m_support',street_length_m=1500)])))
            with patch('figures_tables.activity_figures.subprocess.run') as render:
                files=postcode_sizes(source,p/'release',support_release=support);render.assert_called_once()
            with (p/'release/postcode_sizes.csv').open() as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),4);self.assertEqual(rows[-1]['reason'],'area_cap')
            self.assertNotIn('observation_id',rows[0]);self.assertNotIn('postcode',rows[0])
            with (p/'release/postcode-area-histogram.csv').open() as f:bins=list(csv.DictReader(f))
            self.assertEqual(sum(int(r['allocations']) for r in bins),3)
            self.assertEqual({(r['area_lower_km2'],r['allocations']) for r in bins},{('0','2'),('1','1')})
            self.assertEqual(len(files),5)
            config=p/'figures.json'
            config.write_text(json.dumps(dict(postcode_sizes=dict(source=str(source),support_release=str(support)),release_outputs=[])))
            def render_files(command, **kwargs):
                for suffix in ('.png',):Path(command[3]).with_suffix(suffix).write_bytes(b'figure fixture')
            with patch('figures_tables.activity_figures.subprocess.run',side_effect=render_files):
                paper_outputs(config,p/'figures')
            manifest=json.loads((p/'figures/manifest.json').read_text())
            self.assertEqual(manifest['artifacts'][0]['files'],files)
            self.assertEqual(manifest['artifacts'][0]['label'],'fig:allocation-size-distributions')
            self.assertIn('postcode_sizes.csv',(p/'figures/release-outputs.csv').read_text())
            source.write_text(source.read_text().replace('polygon,1.01','polygon,51'))
            with self.assertRaisesRegex(ValueError,'Invalid allocated'):postcode_sizes(source,p/'bad',support_release=support)

if __name__=='__main__':unittest.main()
