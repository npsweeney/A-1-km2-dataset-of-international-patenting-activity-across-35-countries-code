"""Convert an OECD TL3 shapefile and REGPAT county lookup to pipeline inputs"""
import argparse
import csv
import json
import re
from pathlib import Path


# Prepare the recorded regional boundaries and lookup fields for spatial checks
def prepare(source, output, *, vintage, property='tl3_id', us_lookup=None, encoding='latin1'):
    import shapefile
    from rasterio.crs import CRS
    for suffix in ('.shp', '.shx', '.dbf', '.prj'):
        if not source.with_suffix(suffix).is_file():
            raise ValueError(f'Missing shapefile component: {suffix}')
    if CRS.from_wkt(source.with_suffix('.prj').read_text()).to_epsg() != 4326:
        raise ValueError('Convert the OECD shapefile to EPSG:4326 before preparing boundaries')
    counties = {}
    if us_lookup:
        with us_lookup.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle, delimiter='|')
            if not {'reg_code', 'reg_tl3'}.issubset(reader.fieldnames or ()):
                raise ValueError('US region lookup requires reg_code and reg_tl3')
            for row in reader:
                code, target = row['reg_code'].strip(), row['reg_tl3'].strip()
                if re.fullmatch(r'US\d{5}', code):
                    targets = [target] if re.fullmatch(r'US\d{3}', target) else []
                    record = dict(status='mapped' if targets else 'unmapped', expected_tl3=targets)
                    if code[2:] in counties and counties[code[2:]] != record:
                        raise ValueError(f'Conflicting county mapping: {code}')
                    counties[code[2:]] = record
    with shapefile.Reader(str(source), encoding=encoding) as reader:
        if property not in {field[0] for field in reader.fields[1:]}:
            raise ValueError(f'Shapefile has no region-code field {property}')
        output.mkdir(parents=True)
        boundary = output / f'oecd_tl3_{vintage}.geojson'
        codes = set()
        with boundary.open('x', encoding='utf-8') as handle:
            handle.write('{"type":"FeatureCollection","features":[')
            for count, row in enumerate(reader.iterShapeRecords()):
                code = str(row.record[property]).strip()
                if count:
                    handle.write(',')
                json.dump(dict(type='Feature', properties={property: code}, geometry=row.shape.__geo_interface__), handle)
                codes.add(code)
            handle.write(']}\n')
        (output / 'us_counties.json').write_text(json.dumps(dict(counties=counties)) + '\n')
        manifest = dict(schema_version=f'gpd_region_preflight_{vintage}_v1', boundary_vintage=vintage,
                        boundary_codes=sorted(codes), feature_count=len(reader), county_crosswalk_file='us_counties.json')
        (output / 'region_preflight.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return boundary, output / 'region_preflight.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--vintage', type=int, choices=(2020, 2024), required=True)
    parser.add_argument('--tl3-property', default='tl3_id')
    parser.add_argument('--us-lookup', type=Path)
    parser.add_argument('--encoding', default='latin1')
    args = parser.parse_args(argv)
    print(prepare(args.input, args.output_dir, vintage=args.vintage, property=args.tl3_property,
                  us_lookup=args.us_lookup, encoding=args.encoding))


if __name__ == '__main__':
    main()
