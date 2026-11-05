"""Prepared fractions must reproduce the existing stage06 outputs exactly"""
import csv,json,tempfile,unittest
from pathlib import Path
from aggregation.aggregate_grid_activity import build,prepare_family_fractions,_PreparedFractions,_address_shares
from test_grid_aggregation import fixture

class PreparedFamilyFractions(unittest.TestCase):
    def _prepare(self, root, source):
        with source.open() as f:count=sum(1 for _ in csv.DictReader(f))
        cohort=root/'cohort.json';cohort.write_text(json.dumps(dict(start_year=2000,end_year=2024,application_master_release='fixture-canonical',scope_countries=['FR','GB','US'],counts=dict(eligible_links=count))))
        return prepare_family_fractions(source_links=source,cohort_manifest=cohort,output_dir=root/'fractions')

    def test_full_outputs_identical(self):
        for options in ({},{'family_locations':'complete','family_copies':30},{'family_locations':True,'shared_direct':True},{'unresolved':True}):
            with self.subTest(options=options),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);allocation,backbone=fixture(root,**options);source=root/'source-links.csv';original=source.read_bytes()
                baseline=build(allocation_manifest=allocation,family_backbone=backbone,output_dir=root/'baseline')
                manifest=self._prepare(root,source)
                cached=build(allocation_manifest=allocation,family_backbone=backbone,output_dir=root/'cached',family_fractions=root/'fractions')
                for name in baseline['artifacts']:
                    self.assertEqual((root/'baseline'/name).read_bytes(),(root/'cached'/name).read_bytes(),name)
                for key,value in baseline.items():
                    if key!='artifacts':self.assertEqual(value,cached[key],key)
                self.assertTrue(manifest['exact_roundtrip_verified_for_every_unit']);self.assertEqual(source.read_bytes(),original)

    def test_changed_roster_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);allocation,backbone=fixture(root);source=root/'source-links.csv';self._prepare(root,source)
            with source.open() as f:reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
            rows[0]['inv_name' if rows[0]['actor']=='inventor' else 'app_name']='changed-name'
            with source.open('w',newline='') as f:writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
            with self.assertRaisesRegex(ValueError,'full source roster'):
                build(allocation_manifest=allocation,family_backbone=backbone,output_dir=root/'cached',family_fractions=root/'fractions')

    def test_exact_roundtrip_and_missing_names_keep_withheld_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'links.csv'
            rows=[dict(appln_id='1',actor='inventor',observation_id='one',inv_name='Alice',address='A',city='Paris',postal_code='1',ctry_code='FR',patent_year='2024',family_id='docdb:1'),dict(appln_id='1',actor='inventor',observation_id='two',inv_name='Bob',address='B',city='Elsewhere',postal_code='2',ctry_code='US',patent_year='2024',family_id='docdb:1')]
            with source.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            self._prepare(root,source);cache=_PreparedFractions(root/'fractions');addresses={'one':[],'two':[]}
            try:
                self.assertEqual(cache.get(addresses,rows,application_id='1',actor='inventor',family_id='docdb:1',patent_year='2024'),_address_shares(addresses,{},application_id='1',actor='inventor',roster=rows))
                with self.assertRaisesRegex(ValueError,'representative family/year'):cache.get(addresses,rows,application_id='1',actor='inventor',family_id='docdb:1',patent_year='2023')
                with self.assertRaisesRegex(ValueError,'observation cohort'):cache.get({'one':[]},rows,application_id='1',actor='inventor',family_id='docdb:1',patent_year='2024')
            finally:cache.close()

if __name__=='__main__':unittest.main()
