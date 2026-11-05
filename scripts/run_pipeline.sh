#!/usr/bin/env bash
# Run the pipeline using the supplied data and service configuration
set -euo pipefail
FROM_STAGE=1
if [[ $# == 2 && "$1" == "--from-stage" && "$2" =~ ^[1-8]$ ]]; then
  FROM_STAGE="$2"
  shift 2
fi
if [[ $# -gt 0 ]]; then
  echo 'Usage: run_pipeline.sh [--from-stage 1-8]' >&2
  exit 2
fi
pipeline_repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Verify completed steps before reuse; otherwise execute and save a receipt
step() {
  local stage="$1" completed="$2"
  shift 2
  if (( FROM_STAGE <= stage )); then
    local reuse=()
    if (( FROM_STAGE > 1 )); then reuse+=(--reuse); fi
    python "$pipeline_repository/scripts/pipeline_stage.py" --marker "$completed" \
      --receipt "$RUN_OUTPUT/.pipeline/${completed#"$RUN_OUTPUT/"}.receipt.json" ${reuse[@]+"${reuse[@]}"} -- "$@"
  fi
}
# Stage 07: internal masked evaluation first, then each supplied external comparator
validate() {
  if [[ -n "${MASKED_ALLOCATION_MANIFEST:-}" ]]; then
    step 7 "$RUN_OUTPUT/07_validation/01_internal/summary.json" patent-code validate-masking --allocation-manifest "$MASKED_ALLOCATION_MANIFEST" \
      --query-manifest "${MASKED_QUERY_MANIFEST:?Supply the actual independent masked-query manifest}" \
      --truth "${MASKED_TRUTH:?Supply the held-out truth file}" \
      --output-dir "$RUN_OUTPUT/07_validation/01_internal"
  fi
  local options=(--activity-manifest "$RUN_OUTPUT/06_activity/manifest.json"
    --start-year "${VALIDATION_START_YEAR:-2000}" --end-year "${VALIDATION_END_YEAR:-2024}")
  if [[ -n "${MORRISON_DESCRIPTORS:-}" ]]; then
    step 7 "$RUN_OUTPUT/07_validation/02_external/morrison/summary.json" patent-code validate "${options[@]}" --reference morrison \
      --morrison-descriptors "$MORRISON_DESCRIPTORS" --morrison-inventors "$MORRISON_INVENTORS" \
      --morrison-applicants "$MORRISON_APPLICANTS" --output-dir "$RUN_OUTPUT/07_validation/02_external/morrison"
  fi
  if [[ -n "${DE_RASSENFOSSE_INVENTORS:-}" ]]; then
    local id_options=()
    if [[ -n "${DE_RASSENFOSSE_ID_MAP:-}" ]]; then
      id_options=(--de-rassenfosse-id-map "$DE_RASSENFOSSE_ID_MAP")
    fi
    step 7 "$RUN_OUTPUT/07_validation/02_external/de_rassenfosse/summary.json" patent-code validate "${options[@]}" --reference de-rassenfosse \
      --de-rassenfosse-inventors "$DE_RASSENFOSSE_INVENTORS" --de-rassenfosse-applicants "$DE_RASSENFOSSE_APPLICANTS" \
      ${id_options[@]+"${id_options[@]}"} --output-dir "$RUN_OUTPUT/07_validation/02_external/de_rassenfosse"
  fi
}
# Stage 08: render figures and tables from completed outputs
if (( FROM_STAGE == 8 )); then
  : "${RUN_OUTPUT:?Directory for this run}"
  step 8 "$RUN_OUTPUT/08_figures_tables/manifest.json" patent-code figures-tables --config "${FIGURES_CONFIG:?Paper-output configuration}" --output-dir "$RUN_OUTPUT/08_figures_tables"
  exit
fi
if [[ -n "${MORRISON_DESCRIPTORS:-}${MORRISON_INVENTORS:-}${MORRISON_APPLICANTS:-}" ]]; then
  : "${MORRISON_DESCRIPTORS:?Supply all three Morrison files}"
  : "${MORRISON_INVENTORS:?Supply all three Morrison files}"
  : "${MORRISON_APPLICANTS:?Supply all three Morrison files}"
fi
if [[ -n "${DE_RASSENFOSSE_INVENTORS:-}${DE_RASSENFOSSE_APPLICANTS:-}${DE_RASSENFOSSE_ID_MAP:-}" ]]; then
  : "${DE_RASSENFOSSE_INVENTORS:?Supply both de Rassenfosse files}"
  : "${DE_RASSENFOSSE_APPLICANTS:?Supply both de Rassenfosse files}"
fi
if (( FROM_STAGE == 7 )); then
  : "${RUN_OUTPUT:?Directory for this run}"
  if [[ -z "${MASKED_ALLOCATION_MANIFEST:-}${MORRISON_DESCRIPTORS:-}${DE_RASSENFOSSE_INVENTORS:-}" ]]; then
    echo 'Supply masked-run or external-reference inputs for validation.' >&2
    exit 2
  fi
  validate
  exit
fi
# Validate inputs for the requested starting stage
: "${RUN_OUTPUT:?Directory for this run}"
if (( FROM_STAGE <= 1 )); then
  : "${REGPAT_RELEASE:?Path to the extracted REGPAT release}"
fi
if (( FROM_STAGE <= 3 )); then
  : "${BOUNDARIES:?Country and region boundaries}"
  : "${REGION_PREFLIGHT:?REGPAT region mapping}"
  : "${WORKERS:?Geocoder worker count}"
  : "${MAX_IN_FLIGHT:?Concurrent request limit}"
  : "${REQUEST_BUDGET:?Reviewed geocoder request budget}"
fi
if (( FROM_STAGE <= 4 )) || { (( FROM_STAGE <= 5 )) && [[ -n "${GEOGRAPHIC_BATCH_SIZE:-}" ]]; }; then
  : "${GEOGRAPHIC_POLICY:?Geographic area policy JSON}"
  : "${GEOGRAPHIC_SERVICE:?Geographic service JSON}"
  GEOCODER_ENDPOINT="$(python - "$GEOGRAPHIC_SERVICE" "${GEOCODER_ENDPOINT:-}" "${WORKERS:-}" "${MAX_IN_FLIGHT:-}" <<'PYSERVICE'
import json,sys
from pathlib import Path
from geographic_uncertainty.retrieve_and_rasterise import _service,check_capacity
service=json.loads(Path(sys.argv[1]).read_text())
check_capacity(service,workers=int(sys.argv[3]) if sys.argv[3] else None,max_in_flight=int(sys.argv[4]) if sys.argv[4] else None)
endpoint,_=_service(service)
if sys.argv[2] and sys.argv[2].rstrip('/') != endpoint:
    raise SystemExit('GEOCODER_ENDPOINT differs from GEOGRAPHIC_SERVICE')
print(endpoint)
PYSERVICE
)"
fi
if (( FROM_STAGE <= 5 )); then
  : "${GHSL_SOURCE_MANIFEST:?GHSL asset manifest JSON}"
  GRID_CONTRACT="${GRID_CONTRACT:-$pipeline_repository/config/ghsl_grid.json}"
fi
if (( FROM_STAGE <= 3 )) && [[ -n "${MAX_ADDRESSES:-}" && ! "$MAX_ADDRESSES" =~ ^[1-9][0-9]*$ ]]; then
  echo 'MAX_ADDRESSES must be a positive integer.' >&2
  exit 2
fi
mkdir -p "$RUN_OUTPUT"
RUN_OUTPUT="$(cd "$RUN_OUTPUT" && pwd)"
if (( FROM_STAGE <= 1 )); then
# Stage 01: use the verified canonical dates and preserve full family-role source links
year_sources=()
if [[ -n "${METADATA_CACHE:-}" ]]; then year_sources+=(--metadata-cache "$METADATA_CACHE"); fi
if [[ -n "${METADATA_CACHE_BACKUP:-}" ]]; then year_sources+=(--metadata-cache-backup "$METADATA_CACHE_BACKUP"); fi
if [[ -n "${APPLICATION_MASTER:-}" ]]; then year_sources+=(--application-master "$APPLICATION_MASTER"); fi
if [[ -n "${PATSTAT_DB:-}" ]]; then year_sources+=(--patstat-db "$PATSTAT_DB"); fi
if [[ -n "${OPS_CACHE_DB:-}" ]]; then year_sources+=(--ops-cache-db "$OPS_CACHE_DB"); fi
if [[ -z "${PATSTAT_DB:-}${OPS_CACHE_DB:-}${FAMILY_BACKBONE:-}" ]]; then
  echo 'Supply PATSTAT_DB, OPS_CACHE_DB or both for DOCDB families and application filing dates.' >&2
  exit 1
fi
if [[ -n "${FAMILY_BACKBONE:-}" ]]; then year_sources+=(--family-backbone "$FAMILY_BACKBONE"); fi
: "${END_YEAR:?Set the final included application filing year explicitly, e.g. 2024}"
: "${APPLICATION_MASTER:?Supply the verified latest canonical application master}"
: "${APPLICATION_MASTER_RELEASE:?Supply its verified release ID}"
python - "$APPLICATION_MASTER" "$APPLICATION_MASTER_RELEASE" <<'PYMASTER'
import json, sys
from pathlib import Path
master = Path(sys.argv[1]).resolve(strict=True)
identity = json.loads((master.parent / 'release.json').read_text())
if identity.get('validated') is not True or identity.get('release_id') != sys.argv[2]:
    raise SystemExit('Canonical application master release mismatch or not validated')
PYMASTER
patent-code ingest --input "$REGPAT_RELEASE" "${year_sources[@]}" \
  --start-year "${START_YEAR:-2000}" --end-year "$END_YEAR" --output "$RUN_OUTPUT/01_addresses.csv"

# Resolve families for the selected applications when no backbone was supplied
if [[ -z "${FAMILY_BACKBONE:-}" ]]; then
  backbone_sources=()
  if [[ -n "${PATSTAT_DB:-}" ]]; then
    python - "$RUN_OUTPUT/01_addresses.links.csv" "$RUN_OUTPUT/01_application_ids.txt" <<'PY'
import csv, sys
with open(sys.argv[1], newline='', encoding='utf-8') as handle:
    ids = sorted({row['appln_id'] for row in csv.DictReader(handle) if row['appln_id']})
with open(sys.argv[2], 'x', encoding='utf-8') as handle:
    handle.write(''.join(value + '\n' for value in ids))
PY
    patent-code map-families --regpat-ids "$RUN_OUTPUT/01_application_ids.txt" \
      --patstat-db "$PATSTAT_DB" --output-db "$RUN_OUTPUT/01_patstat_families.sqlite" \
      --summary "$RUN_OUTPUT/01_patstat_families.json"
    backbone_sources+=(--base-family-db "$RUN_OUTPUT/01_patstat_families.sqlite")
  fi
  if [[ -n "${OPS_CACHE_DB:-}" ]]; then backbone_sources+=(--ops-cache-db "$OPS_CACHE_DB"); fi
  FAMILY_BACKBONE="$RUN_OUTPUT/01_family_backbone.sqlite"
  patent-code build-backbone "${backbone_sources[@]}" --output-db "$FAMILY_BACKBONE" \
    --summary "$RUN_OUTPUT/01_family_backbone.json"
fi
else
  FAMILY_BACKBONE="${FAMILY_BACKBONE:-$RUN_OUTPUT/01_family_backbone.sqlite}"
fi
if (( FROM_STAGE <= 2 )); then
# Stage 02: prepare query fields while retaining the original address and cleaning audit
step 2 "$RUN_OUTPUT/02_cleaned.csv" patent-code clean --input "$RUN_OUTPUT/01_addresses.csv" --output "$RUN_OUTPUT/02_cleaned.csv"
fi
if (( FROM_STAGE <= 3 )); then
# Stage 03: bounded private-service requests, explicit zero delay and a reusable response cache
geocode_options=(--input "$RUN_OUTPUT/02_cleaned.csv"
  --source-links "$RUN_OUTPUT/01_addresses.links.csv" --boundaries "$BOUNDARIES"
  --tl3-property "${TL3_PROPERTY:-TL3_CODE}"
  --region-preflight "$REGION_PREFLIGHT"
  --endpoint "$GEOCODER_ENDPOINT" --service-identity "$GEOGRAPHIC_SERVICE" --workers "$WORKERS" --max-in-flight "$MAX_IN_FLIGHT"
  --request-budget "$REQUEST_BUDGET" --timeout-seconds "${TIMEOUT_SECONDS:-31}"
  --max-attempts "${MAX_ATTEMPTS:-2}" --delay-seconds 0 --cache "${GEOCODER_CACHE:-$RUN_OUTPUT/03_cache}"
  --output "$RUN_OUTPUT/03_geocoding")
if [[ -n "${GEOCODER_STOP_FILE:-}" ]]; then geocode_options+=(--stop-file "$GEOCODER_STOP_FILE"); fi
if [[ -n "${MAX_ADDRESSES:-}" ]]; then
  geocode_options+=(--max-rows "$MAX_ADDRESSES")
fi
step 3 "$RUN_OUTPUT/03_geocoding/run_manifest.json" patent-code geocode run "${geocode_options[@]}"
fi
# Stages 04–05: choose resumable local batches, or the same commands over the whole input
if [[ -n "${GEOGRAPHIC_BATCH_SIZE:-}" ]] && (( FROM_STAGE <= 5 )); then
  step 5 "$RUN_OUTPUT/05_ghsl_registry.json" patent-code register-ghsl --source-manifest "$GHSL_SOURCE_MANIFEST" \
    --grid-contract "$GRID_CONTRACT" --output "$RUN_OUTPUT/05_ghsl_registry.json"
  geographic_batch_options=(--run-batches --geocoder-dir "$RUN_OUTPUT/03_geocoding" --family-backbone "$FAMILY_BACKBONE"
    --batch-size "$GEOGRAPHIC_BATCH_SIZE" --working-dir "${GEOGRAPHIC_WORK_DIR:-$RUN_OUTPUT/.geographic-work}"
    --service-identity "$GEOGRAPHIC_SERVICE" --policy "$GEOGRAPHIC_POLICY" --grid-contract "$GRID_CONTRACT"
    --ghsl-registry "$RUN_OUTPUT/05_ghsl_registry.json" --output-dir "$RUN_OUTPUT")
  if (( FROM_STAGE > 1 )); then geographic_batch_options+=(--resume); fi
  if [[ -n "${GEOGRAPHIC_DURABLE_CACHE:-}" ]]; then geographic_batch_options+=(--durable-cache "$GEOGRAPHIC_DURABLE_CACHE"); fi
  if [[ -n "${GEOGRAPHIC_STOP_FILE:-}" ]]; then geographic_batch_options+=(--stop-file "$GEOGRAPHIC_STOP_FILE"); fi
  step 5 "$RUN_OUTPUT/geographic-batches-complete.json" patent-code handoff "${geographic_batch_options[@]}"
else
if (( FROM_STAGE <= 4 )); then
# Stage 04: retrieve missing geometry and construct audited street/postcode areas
step 4 "$RUN_OUTPUT/04_inputs/handoff.json" patent-code handoff --geocoder-dir "$RUN_OUTPUT/03_geocoding" \
  --family-backbone "$FAMILY_BACKBONE" --output-dir "$RUN_OUTPUT/04_inputs"
step 4 "$RUN_OUTPUT/04_requests/stage_a_request_manifest.json" patent-code geographies prepare --runner "$RUN_OUTPUT/04_inputs/runner.csv" \
  --routing "$RUN_OUTPUT/04_inputs/routing.csv" --support-seed-manifest "$RUN_OUTPUT/04_inputs/seeds.json" \
  --final-evidence "$RUN_OUTPUT/04_inputs/handoff.json" --grid-contract "$GRID_CONTRACT" \
  --policy "$GEOGRAPHIC_POLICY" --output-dir "$RUN_OUTPUT/04_requests"
step 4 "$RUN_OUTPUT/04_responses/response_ledger_manifest.json" patent-code geographies acquire --requests "$RUN_OUTPUT/04_requests/support_requests.csv" \
  --request-manifest "$RUN_OUTPUT/04_requests/stage_a_request_manifest.json" \
  --service-identity "$GEOGRAPHIC_SERVICE" --output-dir "$RUN_OUTPUT/04_responses"
step 4 "$RUN_OUTPUT/04_support/support_inputs.json" patent-code geographies finalize --requests "$RUN_OUTPUT/04_requests/support_requests.csv" \
  --request-manifest "$RUN_OUTPUT/04_requests/stage_a_request_manifest.json" \
  --runner "$RUN_OUTPUT/04_inputs/runner.csv" --routing "$RUN_OUTPUT/04_inputs/routing.csv" \
  --final-evidence "$RUN_OUTPUT/04_inputs/handoff.json" --responses "$RUN_OUTPUT/04_responses/response_ledger.csv" \
  --response-manifest "$RUN_OUTPUT/04_responses/response_ledger_manifest.json" \
  --service-identity "$GEOGRAPHIC_SERVICE" --cross-stage-release "$RUN_OUTPUT/04_inputs/handoff.json" \
  --grid-contract "$GRID_CONTRACT" --output-dir "$RUN_OUTPUT/04_support"
fi
if (( FROM_STAGE <= 5 )); then
# Expose the same postcode-size path for batched and non-batched outputs
cp -p "$RUN_OUTPUT/04_support/postcode_sizes.csv" "$RUN_OUTPUT/04_inputs/postcode_sizes.csv"
step 5 "$RUN_OUTPUT/05_ghsl_registry.json" patent-code register-ghsl --source-manifest "$GHSL_SOURCE_MANIFEST" \
  --grid-contract "$GRID_CONTRACT" --output "$RUN_OUTPUT/05_ghsl_registry.json"
# Stage 05: sample filing-year GHSL and allocate spatial shares to the aligned grid
step 5 "$RUN_OUTPUT/05_sampled/support_release.json" patent-code sample-ghsl --cross-stage-release "$RUN_OUTPUT/04_inputs/handoff.json" \
  --ghsl-registry "$RUN_OUTPUT/05_ghsl_registry.json" --grid-contract "$GRID_CONTRACT" \
  --support-inputs "$RUN_OUTPUT/04_support/support_inputs.json" --output-dir "$RUN_OUTPUT/05_sampled"
step 5 "$RUN_OUTPUT/05_grid/final_grid_manifest.json" patent-code allocate --cross-stage-release "$RUN_OUTPUT/04_inputs/handoff.json" \
  --support-release "$RUN_OUTPUT/05_sampled/support_release.json" --output-dir "$RUN_OUTPUT/05_grid"
fi
fi

# Stage 06: apply saved or freshly calculated family fractions to the reconciled grid weights
aggregation_command=(patent-code aggregate-grid --allocation-manifest "$RUN_OUTPUT/05_grid/final_grid_manifest.json"
  --family-backbone "$FAMILY_BACKBONE" --output-dir "$RUN_OUTPUT/06_activity")
if [[ -n "${FAMILY_FRACTIONS:-}" ]]; then aggregation_command+=(--family-fractions "$FAMILY_FRACTIONS"); fi
step 6 "$RUN_OUTPUT/06_activity/manifest.json" "${aggregation_command[@]}"

if [[ -n "${MASKED_ALLOCATION_MANIFEST:-}${MORRISON_DESCRIPTORS:-}${DE_RASSENFOSSE_INVENTORS:-}" ]]; then validate; fi

# Stage 08: produce configured paper outputs from the completed data and validation
if [[ -n "${FIGURES_CONFIG:-}" ]]; then
  step 8 "$RUN_OUTPUT/08_figures_tables/manifest.json" patent-code figures-tables --config "$FIGURES_CONFIG" --output-dir "$RUN_OUTPUT/08_figures_tables"
fi
