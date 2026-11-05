"""Command line interface"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from geocoding.search_and_select import run

# Run address searches with the supplied endpoint and request limits
def main() -> None:
    parser = argparse.ArgumentParser(prog='regpat-geocode')
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('run', help='geocode REGPAT addresses')
    command.add_argument('--input', required=True, type=Path)
    command.add_argument('--source-links', type=Path, help='stage-01 application links for deduplicated address inputs')
    command.add_argument('--boundaries', required=True, type=Path)
    command.add_argument('--tl3-property', default='TL3_CODE')
    command.add_argument('--us-county-boundaries', type=Path, help='optional US county GeoJSON used to validate REGPAT US+FIPS codes separately')
    command.add_argument('--us-county-property', default='GEOID')
    command.add_argument('--region-preflight', required=True, type=Path, help='verified versioned boundary/FIPS-to-TL3 manifest; resolves source geography before queries')
    command.add_argument('--output', required=True, type=Path)
    command.add_argument('--endpoint', required=True, help='explicit Nominatim base URL; no public-service default is permitted')
    command.add_argument('--service-identity', required=True, type=Path, help='Service JSON recording endpoint, database identity, data revision and Nominatim version')
    command.add_argument('--cache', type=Path, default=Path('data/cache/nominatim'))
    command.add_argument('--user-agent', default='patent-code/0.2.2')
    command.add_argument('--candidate-limit', type=int, default=5)
    command.add_argument('--minimum-evidence-score', type=float, default=0.35)
    command.add_argument('--delay-seconds', type=float, default=0.0, help='Private-service request pacing; default 0. Set explicitly only when required by the endpoint policy.')
    command.add_argument('--workers', type=int, default=8, help='parallel precise-query fetches; final decisions remain serial and deterministic')
    command.add_argument('--timeout-seconds', type=float, default=31.0, help='hard HTTP timeout for each Nominatim query')
    command.add_argument('--max-attempts', type=int, default=2, help='bounded attempts for transient 429, 5xx and timeout failures')
    command.add_argument('--retry-backoff-seconds', type=float, default=2.0, help='initial exponential backoff between transient-failure attempts')
    command.add_argument('--max-in-flight', type=int, default=8, help='maximum simultaneous requests even when more worker threads are used')
    command.add_argument('--request-budget', required=True, type=int, help='hard total network-attempt ceiling for the entire run')
    command.add_argument('--max-house-range-members', type=int, default=10, help='abstain rather than expanding a numeric house range beyond this size')
    command.add_argument('--max-rows', type=int)
    command.add_argument('--prepare-inputs-only', action='store_true', help='Validate and save the bounded-memory address/link index without network requests; rerun the same command without this flag to geocode')
    command.add_argument('--stop-file', type=Path, help='Pause after a committed address batch when this file exists; remove it before continuing')
    args = parser.parse_args()
    if urlparse(args.endpoint).hostname == 'nominatim.openstreetmap.org':
        raise SystemExit('Public Nominatim is not permitted for bulk processing; use a local or privately hosted endpoint')
    if args.workers < 1:
        raise SystemExit('--workers must be at least 1')
    if args.timeout_seconds <= 0:
        raise SystemExit('--timeout-seconds must be positive')
    if args.max_attempts < 1:
        raise SystemExit('--max-attempts must be at least 1')
    if args.retry_backoff_seconds < 0:
        raise SystemExit('--retry-backoff-seconds must not be negative')
    if args.max_in_flight < 1:
        raise SystemExit('--max-in-flight must be at least 1')
    if args.request_budget < 1:
        raise SystemExit('--request-budget must be at least 1')
    if args.max_house_range_members < 2:
        raise SystemExit('--max-house-range-members must be at least 2')
    manifest = run(input_path=args.input, source_links_path=args.source_links, boundary_path=args.boundaries, output_dir=args.output, tl3_property=args.tl3_property, endpoint=args.endpoint, cache_dir=args.cache, user_agent=args.user_agent, limit=args.candidate_limit, minimum_score=args.minimum_evidence_score, delay_seconds=args.delay_seconds, workers=args.workers, timeout_seconds=args.timeout_seconds, max_attempts=args.max_attempts, retry_backoff_seconds=args.retry_backoff_seconds, max_in_flight=args.max_in_flight, request_budget=args.request_budget, max_house_range_members=args.max_house_range_members, max_rows=args.max_rows, us_county_boundary_path=args.us_county_boundaries, us_county_property=args.us_county_property, region_preflight=args.region_preflight, regional_only=True, normalized_agreement=True, prepare_inputs_only=args.prepare_inputs_only, stop_file=args.stop_file, service_identity_path=args.service_identity)
    print(json.dumps(manifest['counts'], sort_keys=True))
if __name__ == '__main__':
    main()
