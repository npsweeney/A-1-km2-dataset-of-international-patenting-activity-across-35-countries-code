# A 1 km² dataset of international patenting activity across 35 countries

[Data](https://doi.org/10.5281/zenodo.23982057) · [Code archive](https://doi.org/10.5281/zenodo.23981846)

Applicant and inventor addresses from the August 2026 REGPAT release are geocoded and spatially allocated to the GHSL 1 km grid for 2000–2024, with separate annual outputs for each role.

## Installation

Requires Python 3.11+. Figures also require R with `ggplot2` and `jsonlite`. Version constraints are in `requirements/`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ".[metadata,raster,validation]"
```

## Inputs and configuration

Inputs are the four REGPAT applicant/inventor tables, an application-date master with `release.json`, PATSTAT/OPS family links, OECD TL3 boundaries (2024, Iceland 2020), the August 2026 US lookup, and GHSL GHS-BUILT-S R2023A 100 m rasters. Obtain REGPAT and OECD boundaries from sti.microdatalab@oecd.org.

Geocoding uses private Nominatim 5.3.3 with the GB/NI postcode supplement, at least 16 CPUs and 64 GB RAM. The [service configuration](config/geographic_service.template.json) records database identity, query/statement timeouts and [measured capacity](config/capacity_manifest.example.json) for eight clients with zero delay.

Set paths and request budgets in [run.env.example](config/run.env.example), the [geographic policy](config/geographic_policy.template.json) and [GHSL assets](config/ghsl_assets.template.json). Reuse compatible prepared inputs and services; JSON paths are literal.

```sh
set -a
. /data/patent-run.env
set +a
bash scripts/run_pipeline.sh
```

## Pipeline

Source directories are numbered in processing order. Commands provide `--help`.

| Stage | Processing |
| --- | --- |
| 01 `ingest` | Select DOCDB family representatives by verified application filing date and retain the source address links |
| 02 `clean` | Prepare address fields using [20 general and 65 country-specific rules](docs/address-preparation-rules.md) |
| 03 `geocode run` | Search within source-region bounds and compare returned address components |
| 04 `handoff`, `geographies` | Retrieve street geometries and construct postcode areas |
| 05 `register-ghsl`, `sample-ghsl`, `allocate` | Apply filing-year built-surface weights and aggregate to 1 km cells |
| 06 `aggregate-grid` | Calculate separate inventor and applicant fractional-family panels |
| 07 `validate-masking`, `validate` | Evaluate independent masked searches and matched external references |
| 08 `figures-tables` | Produce the configured tables and figures |

Stages 04–05 run in resumable batches, with separate working and durable-cache directories. Preserve outputs and caches. To pause, create `GEOGRAPHIC_STOP_FILE`; remove it and resume with `bash scripts/run_pipeline.sh --from-stage 4`.

## Spatial allocation and outputs

Direct addresses are assigned to their containing grid cells. Street locations use the mapped road geometry. Postcodes use a matching polygon where available; otherwise, a point that agrees with the country, postcode and source TL3 establishes its containing 1 km cell. Queries at neighbouring cell centres extend the inferred area.

Mapped and inferred postcode allocations are limited to 50 km² of raster cells, with construction methods and extent status retained in `04_inputs/postcode_sizes.csv`. Street and postcode allocations use epoch-specific GHSL weights at 100 m before aggregation to 1 km.

Each DOCDB family uses its earliest verified application filing date within REGPAT. Each role contributes one unit, divided among distinct recorded names and then each name's distinct addresses. Unlocated shares remain withheld.

Stage 06 writes inventor and applicant panels with `grid_cell_id`, `patent_year` and `activity`:

| Suffix | Locations included |
| --- | --- |
| `direct` | Direct addresses |
| `direct_street` | Direct addresses and streets |
| `direct_street_pcdb` | The above plus mapped postcode areas |
| `direct_street_pcdb_rg` | The above plus inferred postcode areas, including single-cell estimates |

Tiers are cumulative; do not add them together. Each `_highest_weight_cell.csv` companion assigns address shares to their highest-weight cells, preserving total activity.

## Validation and figures

Internal validation masks 10,000 direct addresses and compares the resulting allocations with their saved full-address coordinates. Prepare masks with `mask`, geocode and allocate them independently using a fresh cache, then supply the `MASKED_*` inputs to stage 07. External validation compares matched application–role 1 km distributions with Morrison and de Rassenfosse.

Stages 07–08 run when their inputs are supplied, or separately using `--from-stage 7` and `--from-stage 8`. Set `FIGURES_CONFIG` from [figures_tables.template.json](config/figures_tables.template.json) for tables, activity maps and comparison figures. The configuration includes the Harlow street, Wuppertal postcode boundary and Murray Hill inferred-postcode examples; Manchester and Manhattan inventor maps; and the applicant–inventor comparison. Set the saved observation/application IDs and input paths for the three address examples. Stage 08 checks their publication links and weights against stages 01 and 05, then writes the figures, tables and dataset and masking text macros.

```sh
python -m unittest discover -s tests -q
```
