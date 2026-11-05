"""Search Nominatim and qualify REGPAT address matches"""
__version__ = '0.2.2'

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


_load("compare_fields", "03_03_compare_fields.py")
_load("select_location", "03_04_select_location.py")
_load("search_and_select", "03_02_search_and_select.py")
_load("geocode", "03_01_geocode.py")
