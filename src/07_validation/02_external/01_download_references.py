"""Resolve and download the publisher reference datasets"""

import argparse, datetime, json, urllib.parse, urllib.request
from pathlib import Path

CATALOG = {
    'morrison': {
        'provider': 'figshare',
        'articles': [3502706, 3502727, 3502730],
        'citation': 'https://doi.org/10.1038/sdata.2017.64',
    },
    'de-rassenfosse': {
        'provider': 'dataverse',
        'doi': 'doi:10.7910/DVN/OTTBDX',
        'citation': 'https://doi.org/10.1038/s41597-019-0264-6',
    },
}


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


# Resolve the reference files from the publisher catalogue before optional download
def files(reference):
    spec = CATALOG[reference]
    result = []
    if spec['provider'] == 'figshare':
        for article in spec['articles']:
            url = f'https://api.figshare.com/v2/articles/{article}'
            meta = get_json(url)
            for f in meta['files']:
                result.append(
                    dict(
                        name=f['name'],
                        url=f['download_url'],
                        bytes=f['size'],
                        metadata_url=url,
                        version=meta.get('version'),
                    )
                )
    else:
        url = 'https://dataverse.harvard.edu/api/datasets/:persistentId/?' + urllib.parse.urlencode(
            {'persistentId': spec['doi']}
        )
        meta = get_json(url)['data']['latestVersion']
        for entry in meta['files']:
            f = entry['dataFile']
            name = f['filename']
            if reference == 'de-rassenfosse' and name not in {'geoc_inv.txt', 'geoc_app.txt'}:
                continue
            result.append(
                dict(
                    name=name,
                    url=f'https://dataverse.harvard.edu/api/access/datafile/{f["id"]}',
                    bytes=f['filesize'],
                    metadata_url=url,
                    version=f'{meta["versionNumber"]}.{meta["versionMinorNumber"]}',
                )
            )
    if not result:
        raise ValueError(
            'Publisher metadata contains no eligible files; inspect the release rather than substituting a dataset'
        )
    return result


# Download the original comparator files from their recorded publisher sources
def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', choices=CATALOG, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--download', action='store_true')
    p.add_argument('--max-bytes', type=int, default=50_000_000_000)
    a = p.parse_args()
    entries = files(a.reference)
    if sum(f['bytes'] for f in entries) > a.max_bytes:
        raise ValueError('Download plan exceeds --max-bytes')
    a.output_dir.mkdir(parents=True, exist_ok=False)
    manifest = dict(
        reference=a.reference,
        citation=CATALOG[a.reference]['citation'],
        checked_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        files=entries,
        downloaded=False,
    )
    (a.output_dir / 'download-plan.json').write_text(json.dumps(manifest, indent=2))
    if a.download:
        for f in entries:
            if Path(f['name']).name != f['name']:
                raise ValueError('Unsafe publisher filename')
            dest = a.output_dir / f['name']
            partial = dest.with_name(dest.name + '.partial')
            with urllib.request.urlopen(f['url'], timeout=120) as incoming, partial.open(
                'xb'
            ) as outgoing:
                total = 0
                while chunk := incoming.read(1024 * 1024):
                    total += len(chunk)
                    if total > f['bytes'] or total > a.max_bytes:
                        raise ValueError('Unexpected download size')
                    outgoing.write(chunk)
            if total != f['bytes']:
                raise ValueError('Incomplete download; partial file preserved')
            partial.rename(dest)
        manifest['downloaded'] = True
        (a.output_dir / 'download-plan.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
