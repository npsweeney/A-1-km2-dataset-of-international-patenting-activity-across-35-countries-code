"""Aggregate DOCDB family activity by role, cell and filing year"""
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



_load('aggregate_grid_activity', '06_01_aggregate_grid_activity.py')
