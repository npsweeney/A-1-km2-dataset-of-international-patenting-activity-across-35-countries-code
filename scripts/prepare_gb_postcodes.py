#!/usr/bin/env python3
"""Stage the official GB and NI postcode points for Nominatim"""
import argparse, csv, gzip, io, json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Download and validate the official postcode supplement before writing the project files
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--without-northern-ireland', action='store_true')
    args = parser.parse_args()
    project = args.project_dir.resolve()
    if not project.is_dir():
        parser.error('The existing Nominatim project directory is required')
    target = project / 'gb_postcodes.csv.gz'
    manifest = project / 'gb_postcodes.source.json'
    if target.exists() or manifest.exists():
        parser.error('Preserve existing postcode files; use a new versioned project')
    filename = 'gb_postcodes.csv.gz' if args.without_northern_ireland else 'gb_postcodes_with_ni.csv.gz'
    url = 'https://nominatim.org/data/' + filename
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    rows = 0
    with gzip.open(io.BytesIO(payload), 'rt', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if not {'postcode', 'lat', 'lon'} <= set(reader.fieldnames or []):
            raise ValueError('Postcode CSV requires postcode, lat and lon columns')
        for row in reader:
            if not row['postcode'].strip() or not (-90 <= float(row['lat']) <= 90 and -180 <= float(row['lon']) <= 180):
                raise ValueError('Invalid postcode or coordinate')
            rows += 1
    if not rows:
        raise ValueError('Empty postcode file')
    with target.open('xb') as stream:
        stream.write(payload)
    source = dict(url=url, rows=rows,
                  retrieved_at_utc=datetime.now(timezone.utc).isoformat(),
                  gb_source='Ordnance Survey Code-Point Open',
                  ni_source=None if args.without_northern_ireland else 'Food Hygiene Ratings Scheme, aggregated postcode centres',
                  coordinates='WGS84 postcode representative points',
                  source_documentation='https://github.com/osm-search/gb-postcode-data',
                  database_refreshed=False)
    with manifest.open('x') as stream:
        json.dump(source, stream, indent=2)
        stream.write('\n')
    print(f'Prepared {rows:,} postcodes; source record: {manifest}')
    print(f'Refresh this project with: nominatim refresh --project-dir {project} --postcodes --threads 8')

if __name__ == '__main__':
    main()
