"""OECD polygon areas and official country identifiers"""
import tempfile,unittest,zipfile
from pathlib import Path
import shapefile
from pyproj import CRS
from figures_tables.paper_outputs import regional_areas

class RegionalAreasTests(unittest.TestCase):
 def test_country_uses_iso3_and_area_uses_polygon(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);stem=p/'TL3_TL2024'
   with shapefile.Writer(str(stem)) as writer:
    for name in ('tl3_id','iso3','name_en','name_or'):writer.field(name,'C')
    for code,country,size in [('ME01','MEX',1),('GB01','GBR',2),('JP01','JPN',3)]:
     writer.poly([[(0,0),(0,size),(size,size),(size,0),(0,0)]])
     writer.record(code,country,code,code)
   stem.with_suffix('.prj').write_text(CRS.from_epsg(4326).to_wkt())
   archive=p/'boundaries.zip'
   with zipfile.ZipFile(archive,'w') as z:
    for suffix in ('.shp','.shx','.dbf','.prj'):z.write(stem.with_suffix(suffix),stem.with_suffix(suffix).name)
   output=p/'output';output.mkdir()
   result=regional_areas(archive,output,countries_scope=('MX','GB','IS'))
   self.assertEqual(result['regions'],2)
   self.assertEqual(result['minimum']['country'],'MX')
   self.assertEqual(result['maximum']['country'],'GB')
   self.assertEqual(result['missing_countries'],['IS'])
   self.assertTrue(12000<result['minimum']['area_km2']<12500)
   self.assertIn('OECDTLThreeMinArea',(output/'oecd-tl3-area-macros.tex').read_text())
