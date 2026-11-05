import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class ValidationRunnerTests(unittest.TestCase):
    def test_crosswalk_is_optional_and_forwarded_when_supplied(self):
        root = Path(__file__).resolve().parents[1]
        for mapped in (False, True):
            with self.subTest(mapped=mapped), tempfile.TemporaryDirectory() as directory:
                p = Path(directory)
                executable = p/'patent-code'
                executable.write_text('''#!/usr/bin/env python
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
out=Path(args[args.index('--output-dir')+1]);out.mkdir(parents=True)
(out/'summary.json').write_text('{}')
Path(os.environ['ARGUMENT_LOG']).write_text(json.dumps(args))
''')
                executable.chmod(0o755)
                env = dict(os.environ, PATH=str(p)+os.pathsep+os.environ['PATH'],
                           RUN_OUTPUT=str(p/'run'), ARGUMENT_LOG=str(p/'args.json'),
                           DE_RASSENFOSSE_INVENTORS='/data/geoc_inv.txt',
                           DE_RASSENFOSSE_APPLICANTS='/data/geoc_app.txt')
                for name in ('MASKED_ALLOCATION_MANIFEST', 'MORRISON_DESCRIPTORS',
                             'MORRISON_INVENTORS', 'MORRISON_APPLICANTS',
                             'DE_RASSENFOSSE_ID_MAP'):
                    env.pop(name, None)
                if mapped:
                    env['DE_RASSENFOSSE_ID_MAP'] = '/data/verified-map.csv'
                result = subprocess.run(['bash', str(root/'scripts/run_pipeline.sh'),
                                         '--from-stage', '7'], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads((p/'args.json').read_text())
                self.assertEqual('--de-rassenfosse-id-map' in args, mapped)
                self.assertEqual(args[args.index('--end-year')+1], '2024')


if __name__ == '__main__':
    unittest.main()
