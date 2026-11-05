"""Exercise original reference files against a completed family allocation"""
import argparse
import csv
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from pyproj import Transformer
except ImportError:
    raise unittest.SkipTest("Install the validation extra for Morrison checks")
from validation.validate import publication, read, run, write
from figures_tables.plot_validation import summarise


class ValidationTests(unittest.TestCase):
    def fixture(self, p):
        grid = dict(crs='ESRI:54009', origin_x=-18041000, origin_y=9000000,
                    extent=[-18041000, -9000000, 18041000, 9000000],
                    resolution_100m=100, resolution_1km=1000, cell_indexing='zero_based_row_column')
        inverse = Transformer.from_crs(grid['crs'], 'EPSG:4326', always_xy=True)
        lon, lat = inverse.transform(grid['origin_x'] + 17878*1000 + 550, grid['origin_y'] - 2768*1000 - 550)
        def save(name, value):
            (p/name).write_text(json.dumps(value))
        def table(name, rows):
            write(p/name, rows, list(rows[0]))
        save('activity.json', dict(allocation_manifest=dict(path='allocation.json'), grid_identity=grid,
             artifacts={name:dict(path=name) for name in ('family_grid.csv', 'withheld_families.csv')}))
        save('allocation.json', dict(grid_identity=grid, cross_stage_release=dict(path='handoff.json'), support_release=dict(path='support.json')))
        save('handoff.json', dict(run_id='test', source_role_links=dict(path='source.csv', before_geographic_filter=True), runner=dict(path='runner.csv')))
        save('support.json', dict(grid_identity=grid, run_id='test', supports=[
             dict(observation_id='street', accepted_cells=[dict(cell_id_100m='r27685c178785')]),
             dict(observation_id='postcode', accepted_cells=[dict(cell_id_100m='r27685c178795')])]))
        source=[]
        for app, oid in [('1','direct'),('2','street'),('3','postcode'),('4','partial'),('4','outside')]:
            source.append(dict(appln_id=app, actor='inventor', observation_id=oid, office='EPO',
                               pub_nbr=str(int(app)*100), address=oid, city='Example', postal_code='', ctry_code='GB'))
        table('source.csv', source)
        table('runner.csv', [dict(observation_id='direct', observed_latitude=lat, observed_longitude=lon)])
        rows=[]
        for app, oid, route, cells in [('1','direct','observed',[('r2768c17878',1)]),
                                     ('2','street','street',[('r2768c17878',.25),('r2768c17879',.75)]),
                                     ('3','postcode','postcode',[('r2768c17877',1)]),
                                     ('4','partial','observed',[('r2768c17878',1/3)])]:
            for cell, weight in cells:
                rows.append(dict(family_id='docdb:'+app, representative_appln_id=app, actor='inventor',
                                 observation_id=oid, patent_year='2010', evidence_type=route,
                                 address_share=1/3 if app=='4' else 1, country_code='GB', grid_cell_id=cell, weight=weight))
        temporal=[dict(observation_id=oid,patent_year='2010') for oid in ('direct','street','postcode','partial','outside','missing','unmatched')]
        table('temporal.csv',temporal)
        cross=json.loads((p/'handoff.json').read_text());cross.update(temporal_child=dict(path='temporal.csv'),geocoder_manifest=dict(path=str(p/'query.json')));save('handoff.json',cross)
        table('family_grid.csv', rows)
        table('withheld_families.csv', [dict(actor='inventor', patent_year='2010', withheld_weight=2/3)])
        (p/'descriptors.txt').write_text('pat|yr|wasComplete\n'+''.join(f'EP{a*100:07d}|2010|1\n' for a in range(1,5)))
        (p/'inventors.txt').write_text(''.join(f'id|EP{a*100:07d}|Name|{lat},{lon}|87|high\n' for a in range(1,5)))
        (p/'applicants.txt').write_text('')
        return argparse.Namespace(activity_manifest=p/'activity.json', morrison_descriptors=p/'descriptors.txt',
              morrison_inventors=p/'inventors.txt', morrison_applicants=p/'applicants.txt', output=p/'results', start_year=2000, end_year=2011)

    def test_comparisons_preserve_cohort_mass_and_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); args=self.fixture(p); run(args)
            s=json.loads((args.output/'summary.json').read_text())
            self.assertLess(s['points']['inventor']['median_m'], 1e-6)
            self.assertEqual(s['points']['inventor']['unique_addresses'],1)
            by_route={r['route']:r for r in s['uncertainty']}
            self.assertEqual(by_route['street']['support_coverage'],1)
            self.assertEqual(by_route['street']['mean_reference_cell_spatial_weight'],.25)
            self.assertEqual(by_route['postcode']['support_coverage'],0)
            self.assertEqual(by_route['postcode']['mean_reference_cell_spatial_weight'],0)
            fine=next(r for r in s['aggregates'] if r['resolution_m']==1000)
            self.assertAlmostEqual(fine['redistributed_mass_fraction'],1.75/3)
            self.assertEqual(next(r for r in s['aggregates'] if r['resolution_m']==10000)['redistributed_mass_fraction'],0)
            coverage=next(r for r in s['coverage'] if r['actor']=='inventor')
            self.assertEqual(coverage['compared_mass'],3)
            self.assertAlmostEqual(coverage['unmatched_allocated_mass'],1/3)
            self.assertAlmostEqual(coverage['withheld_mass'],2/3)
            self.assertEqual(s['matching']['exclusions']['incomplete_our_allocation'],1)
            # The stage-7 shell path needs only completed outputs and reference inputs
            env=dict(os.environ, RUN_OUTPUT=str(p), MORRISON_DESCRIPTORS=str(args.morrison_descriptors),
                     MORRISON_INVENTORS=str(args.morrison_inventors), MORRISON_APPLICANTS=str(args.morrison_applicants))
            (p/'06_activity').mkdir(); (p/'06_activity/manifest.json').write_text(json.dumps(dict(
                allocation_manifest=dict(path=str(p/'allocation.json')), grid_identity=s['grid_identity'],
                artifacts={name:dict(path=str(p/name)) for name in ('family_grid.csv','withheld_families.csv')})))
            script=Path(__file__).resolve().parents[1]/'scripts/run_pipeline.sh'
            subprocess.run(['bash',str(script),'--from-stage','7'],env=env,check=True,capture_output=True)
            self.assertTrue((p/'07_validation/02_external/morrison/summary.json').is_file())

    def test_crs_and_mass_mismatches_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); args=self.fixture(p)
            support=json.loads((p/'support.json').read_text()); support['grid_identity']['crs']='EPSG:3857'
            (p/'support.json').write_text(json.dumps(support))
            with self.assertRaisesRegex(ValueError,'differ'): run(args)
            args=self.fixture(p); rows=read(p/'family_grid.csv');rows[0]['weight']='.9'
            write(p/'family_grid.csv',rows,list(rows[0]))
            with self.assertRaisesRegex(ValueError,'conserve'): run(args)

    def test_invalid_missing_and_unlocated_references(self):
        for suffix in ('id|EP0000200|Name|0,0|87|high\n', 'id|EP0000200|Name|1,1|87|unloc\n'):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp); args=self.fixture(p)
                with args.morrison_inventors.open('a') as f: f.write(suffix)
                run(args)
                self.assertNotIn('2',{r['representative_appln_id'] for r in read(args.output/'matches.csv')})
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);args=self.fixture(p);args.morrison_inventors.write_text('');run(args)
            s=json.loads((args.output/'summary.json').read_text())
            self.assertEqual(s['matching']['matched_address_pairs'],0)
            self.assertEqual(s['aggregates'],[])
            self.assertEqual(read(args.output/'03_grid_comparison.csv'),[])

    def test_identifier_and_year_disagreements_are_excluded(self):
        for conflict in ('year', 'publication'):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);args=self.fixture(p)
                if conflict=='year':
                    args.morrison_descriptors.write_text(args.morrison_descriptors.read_text().replace('EP0000200|2010','EP0000200|2009'))
                else:
                    rows=read(p/'source.csv');rows.append({**rows[1], 'appln_id':'99'})
                    write(p/'source.csv',rows,list(rows[0]))
                run(args)
                self.assertNotIn('2',{r['representative_appln_id'] for r in read(args.output/'matches.csv')})

    def de_rassenfosse_fixture(self, p):
        args=self.fixture(p);args.reference='de-rassenfosse'
        rows=read(p/'source.csv')
        for r in rows:r['representative_filing_date']='2010-02-03'
        write(p/'source.csv',rows,list(rows[0]))
        from validation.build_reference_ids import build as build_ids
        old=[dict(appln_id=str(i+100),appln_auth='EP',appln_nr=str(i*100),appln_kind='A',appln_filing_date='2010-02-03') for i in range(1,5)]
        new=[dict(row,appln_id=str(i)) for i,row in enumerate(old,1)]
        write(p/'reference-identifiers.csv',old,list(old[0]));write(p/'current-identifiers.csv',new,list(new[0]))
        build_ids(p/'reference-identifiers.csv',p/'current-identifiers.csv',p/'crosswalk')
        args.de_rassenfosse_id_map=p/'crosswalk/application_ids.csv'
        source=args.morrison_inventors.read_text().splitlines()
        rows=[]
        for i,line in enumerate(source,1):
            lat,lon=line.split('|')[3].split(',')
            rows.append(dict(appln_id=str(i+100),patent_office='EP',filing_date='2010-02-03',lat=lat,lng=lon,source='1',coord_source='geolocalization'))
        args.de_rassenfosse_inventors=p/'geoc_inv.txt';args.de_rassenfosse_applicants=p/'geoc_app.txt'
        write(args.de_rassenfosse_inventors,rows,list(rows[0]));write(args.de_rassenfosse_applicants,[],list(rows[0]))
        return args

    def test_de_rassenfosse_reuses_the_same_three_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);m=self.fixture(p);run(m)
            args=self.de_rassenfosse_fixture(p);args.output=p/'de-rassenfosse';run(args)
            baseline=json.loads((m.output/'summary.json').read_text());actual=json.loads((args.output/'summary.json').read_text())
            for field in ('points','uncertainty','aggregates','coverage'):
                self.assertEqual(actual[field],baseline[field])
            self.assertEqual({r['reference_source'] for r in read(args.output/'matches.csv')},{'1:geolocalization'})
            (p/'06_activity').mkdir()
            manifest=json.loads(args.activity_manifest.read_text())
            manifest['allocation_manifest']['path']=str(p/'allocation.json')
            for name,entry in manifest['artifacts'].items():entry['path']=str(p/name)
            (p/'06_activity/manifest.json').write_text(json.dumps(manifest))
            env=dict(os.environ,RUN_OUTPUT=str(p),MORRISON_DESCRIPTORS=str(args.morrison_descriptors),
                     MORRISON_INVENTORS=str(args.morrison_inventors),MORRISON_APPLICANTS=str(args.morrison_applicants),
                     DE_RASSENFOSSE_INVENTORS=str(args.de_rassenfosse_inventors),DE_RASSENFOSSE_APPLICANTS=str(args.de_rassenfosse_applicants),
                     DE_RASSENFOSSE_ID_MAP=str(args.de_rassenfosse_id_map))
            subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'scripts/run_pipeline.sh'),'--from-stage','7'],env=env,check=True,capture_output=True)
            for reference in ('morrison','de_rassenfosse'):
                self.assertEqual(json.loads((p/'07_validation/02_external'/reference/'summary.json').read_text())['matching']['matched_address_pairs'],3)

    def test_de_rassenfosse_excludes_identity_and_provenance_conflicts(self):
        for field,value in [('filing_date','2010-02-04'),('patent_office','WO'),('source','2'),('coord_source','geonames'),('lat','nan')]:
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);args=self.de_rassenfosse_fixture(p)
                rows=read(args.de_rassenfosse_inventors);rows[1][field]=value
                write(args.de_rassenfosse_inventors,rows,list(rows[0]));run(args)
                self.assertNotIn('2',{r['representative_appln_id'] for r in read(args.output/'matches.csv')})
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);args=self.de_rassenfosse_fixture(p)
            rows=read(args.de_rassenfosse_id_map);rows.append({**rows[1],'regpat_appln_id':'99'})
            write(args.de_rassenfosse_id_map,rows,list(rows[0]))
            with self.assertRaisesRegex(ValueError,'source index'):run(args)

    def test_stable_patstat_id_requires_office_full_date_and_quality(self):
        for conflict in (None, 'patent_office', 'filing_date', 'source', 'coord_source', 'appln_id'):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp); args=self.de_rassenfosse_fixture(p)
                args.de_rassenfosse_id_map=None
                for path in (args.de_rassenfosse_inventors,args.de_rassenfosse_applicants):
                    rows=read(path)
                    if not rows:
                        continue
                    for r in rows:
                        r['appln_id']=str(int(r['appln_id'])-100)
                        if conflict:
                            r[conflict]={'patent_office':'WO','filing_date':'2010-02-04',
                                'source':'2','coord_source':'geonames','appln_id':'999'}[conflict]
                    write(path,rows,list(rows[0]))
                run(args)
                summary=json.loads((args.output/'summary.json').read_text())
                self.assertEqual(summary['id_crosswalk_status'],'stable_patstat_id_office_full_filing_date')
                self.assertEqual(summary['matching']['matched_address_pairs'],3 if conflict is None else 0)

    def test_crosswalk_rejects_changed_application_number_and_kind(self):
        for field in ('appln_nr','appln_kind'):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);args=self.de_rassenfosse_fixture(p)
                rows=read(args.de_rassenfosse_id_map);rows[0][field]='WRONG'
                write(args.de_rassenfosse_id_map,rows,list(rows[0]))
                with self.assertRaisesRegex(ValueError,'source index'):run(args)

    def test_figure_compares_matched_cells_on_shared_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);m=self.fixture(p);run(m)
            d=self.de_rassenfosse_fixture(p);d.output=p/'de-rassenfosse'
            rows=read(d.de_rassenfosse_inventors);rows[1]['source']='2'
            write(d.de_rassenfosse_inventors,rows,list(rows[0]));run(d)
            data=summarise(d.activity_manifest,(m.output,d.output))
            self.assertEqual(data['shared_pairs'],2)
            self.assertEqual(data['eligible_pairs'],dict(morrison=3,de_rassenfosse=2))
            self.assertEqual(data['references']['morrison'],data['references']['de_rassenfosse'])
            cells=data['references']['morrison']['cells']
            self.assertEqual(sum(r['n'] for r in cells), 2)
            self.assertEqual(cells[0]['mean_agreement_percent'], 50)
            point = read(m.output/'01_point_distances.csv')[0]
            self.assertEqual(float(point['reference_cell_spatial_weight']), 1)
            altered=json.loads((d.output/'summary.json').read_text());altered['grid_identity']['crs']='EPSG:3857'
            (d.output/'summary.json').write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError,'stage 06'):summarise(d.activity_manifest,(m.output,d.output))

    def test_matched_cell_scores_keep_fractional_allocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);m=self.de_rassenfosse_fixture(p);m.reference='morrison';run(m)
            d=argparse.Namespace(**{**vars(m),'reference':'de-rassenfosse','output':p/'de-rassenfosse'});run(d)
            data=summarise(m.activity_manifest,(m.output,d.output))
            scores=read(m.output/'04_patent_cell_agreement.csv')
            self.assertEqual({r['representative_appln_id']:float(r['agreement_percent']) for r in scores},
                             {'1':100,'2':25,'3':0})
            rows=read(p/'family_grid.csv')
            for r in rows:
                if r['observation_id']=='street':
                    r['weight']=float(r['weight'])/4
                    r['address_share']=.25
            write(p/'family_grid.csv',rows,list(rows[0]))
            again=summarise(m.activity_manifest,(m.output,d.output))
            self.assertEqual(again['references'],data['references'])

    def test_multiple_source_and_reference_locations_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);a=self.de_rassenfosse_fixture(p);a.reference='morrison'
            source=read(p/'source.csv');source.append({**source[0],'observation_id':'second','address':'Second address'})
            write(p/'source.csv',source,list(source[0]))
            grid=read(p/'family_grid.csv');grid[0].update(weight=.5,address_share=.5)
            grid.append({**grid[0],'observation_id':'second','grid_cell_id':'r2768c17879'})
            write(p/'family_grid.csv',grid,list(grid[0]))
            contract=json.loads((p/'activity.json').read_text())['grid_identity']
            inverse=Transformer.from_crs(contract['crs'],'EPSG:4326',always_xy=True)
            lon,lat=inverse.transform(contract['origin_x']+17879*1000+550,contract['origin_y']-2768*1000-550)
            with a.morrison_inventors.open('a') as f:f.write(f'other|EP0000100|Other|{lat},{lon}|87|high\n')
            run(a)
            row=next(r for r in read(a.output/'04_patent_cell_agreement.csv') if r['representative_appln_id']=='1')
            self.assertEqual((int(row['source_addresses']),int(row['reference_locations'])),(2,2))
            self.assertAlmostEqual(float(row['agreement_percent']),100)
            matched=[r for r in read(a.output/'matches.csv') if r['representative_appln_id']=='1']
            self.assertEqual({r['observation_id'] for r in matched},{'direct','second'})
            self.assertEqual({r['reference_latitude'] for r in matched},{''})
            d=argparse.Namespace(**{**vars(a),'reference':'de-rassenfosse','output':p/'de-rassenfosse'});run(d)
            figure=summarise(a.activity_manifest,(a.output,d.output))
            group=figure['references']['morrison']['cells'][0]
            self.assertEqual(group['n'],3)
            self.assertAlmostEqual(group['mean_agreement_percent'],125/3)

    def test_duplicate_physical_address_alias_does_not_make_patent_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);a=self.fixture(p)
            source=read(p/'source.csv');source.append({**source[0],'observation_id':'duplicate_alias'})
            write(p/'source.csv',source,list(source[0]));run(a)
            row=next(r for r in read(a.output/'04_patent_cell_agreement.csv') if r['representative_appln_id']=='1')
            self.assertEqual(int(row['source_addresses']),1)
            self.assertEqual(float(row['agreement_percent']),100)

    def test_opposite_patent_errors_cannot_cancel(self):
        from validation.validate import patent_cell_agreement
        first=patent_cell_agreement({'A':1},{'B':1})
        second=patent_cell_agreement({'B':1},{'A':1})
        self.assertEqual((first+second)/2,0)
        self.assertEqual(patent_cell_agreement({'A':1,'B':1},{'A':1,'B':1}),100)
        self.assertEqual(patent_cell_agreement({'A':.5,'B':.5},{'A':1}),50)
        self.assertIsNone(patent_cell_agreement({}, {'A':1}))

    def test_publication_numbers(self):
        self.assertEqual(publication('0473341','EPO'),publication('EP0473341'))
        self.assertEqual(publication('1998000123','PCT'),publication('WO9800123'))
        self.assertEqual(publication('2002000123','PCT'),publication('WO02000123'))
        self.assertIsNone(publication('EPbad'))
