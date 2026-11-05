"""Numbered source files exposed through stable Python import names"""
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import sys

def _load(name, filename):
    qualified = __name__ + "." + name
    spec = spec_from_file_location(qualified, Path(__file__).with_name(filename))
    module = module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    globals()[name] = module


_load('map_patstat_families', '01_map_patstat_families.py')
_load('prepare_ops_queue', '02_prepare_ops_queue.py')
_load('resolve_ops_families', '03_resolve_ops_families.py')
_load('build_family_backbone', '04_build_family_backbone.py')

_load('refresh_saved_ops', '05_refresh_saved_ops.py')
