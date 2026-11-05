"""Built-surface weighting and grid allocation"""
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


_load('register_ghsl', '05_01_register_ghsl.py')
_load('allocate_to_grid', '05_03_allocate_to_grid.py')
_load('sample_built_surface', '05_02_sample_built_surface.py')
