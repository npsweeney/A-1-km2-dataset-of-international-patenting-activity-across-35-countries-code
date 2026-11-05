"""Run the international patenting pipeline"""
import importlib
import sys

COUNTRIES = tuple('AT AU BE CA CH CL CO CR CZ DE DK EE ES FI FR GB GR HU IE IS IT LT LU LV MX NL NO NZ PL PT SE SI SK TR US'.split())

STAGES = {
    "refresh-metadata": "ingest.patent_metadata.refresh_saved_ops",
    "plot-validation": "figures_tables.plot_validation",
    "figures-tables": "figures_tables.paper_outputs",
    "mask": "validation.mask",
    "validate-masking": "validation.evaluate_masking",
    "download-references": "validation.download_references",
    "build-reference-ids": "validation.build_reference_ids",
    "validate": "validation.validate",
    "import-patstat": "ingest.patent_metadata.import_patstat",
    "prepare-boundaries": "geocoding.validation.prepare_boundaries",
    "ingest": "ingest.import_regpat",
    "clean": "cleaning.clean_addresses",
    "geocode": "geocoding.geocode",
    "handoff": "geographic_uncertainty.prepare_geographic_inputs",
    "geographies": "geographic_uncertainty.retrieve_and_rasterise",
    "register-ghsl": "weighting.register_ghsl",
    "sample-ghsl": "weighting.sample_built_surface",
    "allocate": "weighting.allocate_to_grid",
    "map-families": "ingest.patent_metadata.map_patstat_families",
    "ops-queue": "ingest.patent_metadata.prepare_ops_queue",
    "ops-resolve": "ingest.patent_metadata.resolve_ops_families",
    "build-backbone": "ingest.patent_metadata.build_family_backbone",
    "aggregate-grid": "aggregation.aggregate_grid_activity",
}

def code_revision():
    from pathlib import Path
    import subprocess
    root=Path(__file__).resolve().parent.parent
    if not (root/'pyproject.toml').is_file():return None
    try:
        revision=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,text=True).strip()
        dirty=subprocess.run(['git','-C',str(root),'diff','--quiet','HEAD','--','src','scripts','pyproject.toml'],check=False).returncode
        return revision+('-dirty' if dirty else '')
    except (OSError,subprocess.CalledProcessError):return None

# Dispatch each public command to its numbered stage implementation
def main():
    if len(sys.argv) < 2 or sys.argv[1] in {"--help", "-h"}:
        print("Usage: patent-code {"+"|".join(STAGES)+"} [stage options]")
        print("Metadata: import-patstat → ops-queue → ops-resolve → build-backbone. Reuse existing caches; resume OPS with the same state database.")
        print("Run patent-code <stage> --help for required inputs.")
        return 0
    command = sys.argv[1]
    if command not in STAGES:
        raise SystemExit("Unknown stage: " + command)
    module = importlib.import_module(STAGES[command])
    sys.argv = ["patent-code " + command, *sys.argv[2:]]
    return module.main() or 0

if __name__ == "__main__":
    raise SystemExit(main())
