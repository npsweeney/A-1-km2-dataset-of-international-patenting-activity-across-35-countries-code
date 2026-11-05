"""Construct street and postcode areas"""
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


from . import routing, contracts
_load('retrieve_and_rasterise', '04_02_retrieve_and_rasterise.py')
_load('prepare_geographic_inputs', '04_01_prepare_geographic_inputs.py')

_load('batches', '04_03_run_batches.py')
