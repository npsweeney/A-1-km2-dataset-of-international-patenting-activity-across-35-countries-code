"""Execute a pipeline step or verify its recorded invocation before reuse"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

IGNORED_PATH_OPTIONS = {'--output', '--output-dir', '--cache', '--metadata-cache', '--metadata-cache-backup', '--working-dir', '--durable-cache', '--stop-file'}


def inventory(paths):
    found = {}
    def visit(path):
        path = path.resolve()
        if str(path) in found or not path.exists():
            return
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix in {'.csv', '.json', '.jsonl'}:
                    visit(child)
            return
        st = path.stat()
        found[str(path)] = [st.st_size, st.st_mtime_ns]
        if path.suffix == '.json':
            def walk(value):
                if isinstance(value, dict):
                    for key,v in value.items():
                        if key not in {'cache_path','response_cache_dir','metadata_cache','metadata_cache_backup'}: walk(v)
                elif isinstance(value, list):
                    for v in value: walk(v)
                elif isinstance(value, str) and (value.startswith('/') or Path(value).suffix in {'.csv', '.json', '.jsonl', '.sqlite', '.sqlite3', '.parquet', '.tif', '.zip', '.shp', '.dbf', '.shx', '.prj', '.R'}):
                    try:
                        p = Path(value)
                        visit(p if p.is_absolute() else path.parent / p)
                    except (OSError, ValueError):
                        pass
            walk(json.loads(path.read_text()))
    for path in paths: visit(Path(path))
    return found


# Verify the invocation and saved inputs and outputs before reusing a completed step
def execute(command, marker, receipt, *, reuse):
    paths = []
    for i, arg in enumerate(command):
        if i and command[i - 1] in IGNORED_PATH_OPTIONS: continue
        if not arg.startswith('-'):
            p = Path(arg)
            if p.exists() and (p.is_absolute() or '/' in arg): paths.append(p)
    # Record the installed code and input paths for this step
    import run
    implementation = [Path(run.__file__), Path(__file__), Path(__file__).with_name('run_pipeline.sh')]
    implementation.extend(Path(run.__file__).parent.rglob('*.py'))
    invocation = dict(command=[arg for arg in command if arg != '--resume'], package_version=importlib.metadata.version('patent-code'), inputs=inventory(paths), implementation=inventory(implementation))
    pending = receipt.with_suffix('.pending.json')
    partial = marker.exists() and marker.name == 'run_manifest.json' and json.loads(marker.read_text()).get('partial')
    if partial and reuse and pending.is_file():
        if json.loads(pending.read_text()) != invocation:
            raise ValueError('Inputs, configuration or code changed since the geocoding checkpoint')
    elif marker.exists():
        if not reuse or not receipt.is_file():
            raise ValueError(f'Existing output has no matching runner receipt; preserve it and use a new RUN_OUTPUT: {marker}')
        saved = json.loads(receipt.read_text())
        if saved['invocation'] != invocation or saved['outputs'] != inventory([marker]):
            raise ValueError(f'Inputs, configuration, code or outputs changed; use a new RUN_OUTPUT: {marker}')
        print('Verified completed step:', marker)
        return
    # Retain the invocation before execution so an interrupted geocoder can resume safely
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if reuse and pending.is_file() and json.loads(pending.read_text()) != invocation:
        raise ValueError('Inputs, configuration or code changed since the pending step')
    temporary = pending.with_suffix('.tmp')
    temporary.write_text(json.dumps(invocation, indent=2))
    temporary.replace(pending)
    subprocess.run(command, check=True)
    if not marker.is_file(): raise ValueError(f'Step did not produce its completion file: {marker}')
    if marker.name == 'run_manifest.json':
        manifest = json.loads(marker.read_text())
        if manifest.get('partial') or manifest.get('production_consumable') is False:
            raise ValueError('Incomplete geocoding is not a completed production stage')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(invocation=invocation, outputs=inventory([marker])), indent=2))
    temporary.replace(receipt)
    pending.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--marker', type=Path, required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--reuse', action='store_true')
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    try: execute(command, a.marker, a.receipt, reuse=a.reuse)
    except (ValueError, OSError) as e: p.exit(2, str(e) + '\n')

if __name__ == '__main__': main()
