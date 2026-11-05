"""Evaluate masking performance and external cell agreement"""

from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import sys


def _load(name, filename):
    qualified = __name__ + '.' + name
    spec = spec_from_file_location(qualified, Path(__file__).parent / filename)
    module = module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    globals()[name] = module


_load('validate', '02_external/03_compare_locations.py')
_load('mask', '01_internal/01_prepare_masks.py')
_load('evaluate_masking', '01_internal/02_evaluate_masks.py')
_load('download_references', '02_external/01_download_references.py')
_load('build_reference_ids', '02_external/02_build_identifier_crosswalk.py')
