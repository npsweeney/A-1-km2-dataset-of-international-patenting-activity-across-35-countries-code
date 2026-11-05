"""Generate the Data Descriptor tables and figures"""
from importlib.util import spec_from_file_location,module_from_spec
from pathlib import Path
import sys

def _load(name,filename):
    qualified=__name__+'.'+name;spec=spec_from_file_location(qualified,Path(__file__).parent/filename)
    module=module_from_spec(spec);sys.modules[qualified]=module;spec.loader.exec_module(module);globals()[name]=module

_load('paper_outputs','08_01_paper_outputs.py')
_load('activity_figures','08_02_activity_figures.py')
_load('internal_comparison','08_03_internal_comparison.py')
_load('plot_validation','08_04_external_comparison.py')
