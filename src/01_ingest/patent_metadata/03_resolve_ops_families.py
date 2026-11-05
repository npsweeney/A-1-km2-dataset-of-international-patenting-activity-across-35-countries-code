"""Update the reusable OPS metadata cache for selected REGPAT applications"""
from __future__ import annotations
import argparse
import csv
import json
from uuid import uuid4
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import requests
OPS_BASE_URL = 'https://ops.epo.org/3.2'
OPS_TOKEN_URL = f'{OPS_BASE_URL}/auth/accesstoken'
OPS_BATCH_URL = f'{OPS_BASE_URL}/rest-services/published-data/publication/docdb/biblio'
OPS_WEEKLY_LIMIT_BYTES = 4000000000

class OPSClient:

    def __init__(self, key: str, secret: str, *, quota_state: Path, request_delay_seconds: float=1.0) -> None:
        self.key = key
        self.secret = secret
        self.quota_state = quota_state
        self.session = requests.Session()
        self.token = ''
        self.token_expires_at = 0.0
        self.request_delay_seconds = request_delay_seconds
        self.last_request_at = 0.0
        self.application_dates = []

    def _authenticate(self) -> None:
        response = self.session.post(OPS_TOKEN_URL, auth=(self.key, self.secret), data={'grant_type': 'client_credentials'}, timeout=60)
        response.raise_for_status()
        payload = response.json()
        self.token = payload['access_token']
        expires_in = float(payload.get('expires_in', 1200))
        self.token_expires_at = time.monotonic() + max(0.0, expires_in - 60.0)

    @staticmethod
    def _token_expired_response(response: requests.Response) -> bool:
        """Recognise OPS' non-standard HTTP 400 response for an expired token"""
        if response.status_code not in {400, 401}:
            return False
        return 'invalid_access_token' in response.text.lower()

    def _quota_used(self) -> int:
        if not self.quota_state.exists():
            return 0
        try:
            payload = json.loads(self.quota_state.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 0
        timestamp = str(payload.get('timestamp_utc', ''))
        now = datetime.now(timezone.utc)
        if timestamp[:10] and timestamp[:10] != now.date().isoformat():
            observed = str(payload.get('quota_week_utc', ''))
            current = f'{now.isocalendar().year}-W{now.isocalendar().week:02d}'
            if observed != current:
                return 0
        return int(payload.get('used_bytes', 0) or 0)

    def _record_quota(self, response: requests.Response) -> None:
        now = datetime.now(timezone.utc)
        raw_used = response.headers.get('x-registeredquotaperweek-used', '')
        used = int(raw_used) if raw_used.isdigit() else self._quota_used()
        payload = {'timestamp_utc': now.isoformat(), 'quota_week_utc': f'{now.isocalendar().year}-W{now.isocalendar().week:02d}', 'used_bytes': used, 'limit_bytes': OPS_WEEKLY_LIMIT_BYTES, 'used_pct': 100.0 * used / OPS_WEEKLY_LIMIT_BYTES, 'hard_stop_pct': 99.0, 'last_response_bytes': len(response.content), 'status_code': response.status_code, 'throttling_control': response.headers.get('X-Throttling-Control', '')}
        self.quota_state.parent.mkdir(parents=True, exist_ok=True)
        self.quota_state.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    def fetch_published_biblio_batch(self, references: list[str]) -> requests.Response:
        if self._quota_used() >= int(OPS_WEEKLY_LIMIT_BYTES * 0.99):
            raise SystemExit('OPS weekly quota has reached the configured 99% stop')
        for attempt in range(1, 6):
            if not self.token or time.monotonic() >= self.token_expires_at:
                self._authenticate()
            elapsed = time.monotonic() - self.last_request_at
            if self.last_request_at and elapsed < self.request_delay_seconds:
                time.sleep(self.request_delay_seconds - elapsed)
            response = self.session.post(OPS_BATCH_URL, data=','.join(references), headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/ops+xml, application/xml;q=0.9', 'Content-Type': 'text/plain; charset=utf-8'}, timeout=180)
            self.last_request_at = time.monotonic()
            self._record_quota(response)
            if self._token_expired_response(response):
                self.token = ''
                self.token_expires_at = 0.0
                continue
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(2 ** attempt, 60))
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

class OPSHTTP400Stop(RuntimeError):
    """Stop a run without declaring an individual reference absent"""

def base_reference(value: str) -> str:
    parts = value.strip().split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 else value.strip()

def normalize_family(value: str) -> str:
    value = value.strip()
    return str(int(value)) if value.isdigit() else value

def initialize(connection: sqlite3.Connection, queue: Path) -> None:
    connection.executescript('\n        CREATE TABLE IF NOT EXISTS application (\n          appln_id TEXT PRIMARY KEY,\n          regpat_source TEXT NOT NULL,\n          source_application_number TEXT NOT NULL,\n          source_publication_number TEXT NOT NULL,\n          primary_reference TEXT NOT NULL,\n          alternate_reference TEXT NOT NULL,\n          family_id TEXT,\n          resolution_source TEXT\n        ) WITHOUT ROWID;\n        CREATE TABLE IF NOT EXISTS application_date_evidence (\n          reference TEXT NOT NULL, application_country TEXT NOT NULL, application_number TEXT NOT NULL,\n          document_id_type TEXT NOT NULL, filing_date TEXT NOT NULL, record_id TEXT NOT NULL,\n          application_reference_xml TEXT NOT NULL,\n          PRIMARY KEY(reference,application_country,application_number,document_id_type,filing_date,record_id)\n        );\n        CREATE TABLE IF NOT EXISTS publication (\n          reference TEXT PRIMARY KEY,\n          status TEXT NOT NULL,\n          family_id TEXT,\n          source TEXT,\n          attempted_at_utc TEXT,\n          error TEXT\n        ) WITHOUT ROWID;\n        ')
    if connection.execute('SELECT COUNT(*) FROM application').fetchone()[0]:
        return
    with queue.open(newline='', encoding='utf-8') as handle:
        for rows in _batches(csv.DictReader(handle), 50000):
            connection.executemany('INSERT INTO application VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)', [(row['appln_id'], row['regpat_source'], row['source_application_number'], row['source_publication_number'], row['ops_reference_primary'], row['ops_reference_alternate']) for row in rows])
            refs = {(row['ops_reference_primary'], 'pending') for row in rows if row['ops_reference_primary']}
            refs.update(((row['ops_reference_alternate'], 'blocked') for row in rows if row['ops_reference_alternate']))
            connection.executemany('INSERT OR IGNORE INTO publication(reference,status) VALUES (?,?)', refs)
            connection.commit()

def _batches(values, size: int):
    batch = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch

# Reuse saved responses before making further OPS requests
def preload_cache(connection: sqlite3.Connection, cache_csv: Path) -> int:
    marker = connection.execute("SELECT COUNT(*) FROM publication WHERE source='archive_cache'").fetchone()[0]
    if marker:
        return marker
    wanted = {row[0] for row in connection.execute('SELECT reference FROM publication')}
    found: dict[str, set[str]] = defaultdict(set)
    with cache_csv.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            reference = base_reference(row.get('publication_docdb', ''))
            family = normalize_family(row.get('simple_family_id', '') or row.get('family_id', ''))
            if reference in wanted and family:
                found[reference].add(family)
    now = datetime.now(timezone.utc).isoformat()
    for reference, families in found.items():
        if len(families) == 1:
            connection.execute("UPDATE publication SET status='resolved',family_id=?,source='archive_cache',attempted_at_utc=? WHERE reference=?", (next(iter(families)), now, reference))
        else:
            connection.execute("UPDATE publication SET status='ambiguous',source='archive_cache',attempted_at_utc=?,error=? WHERE reference=?", (now, ';'.join(sorted(families)), reference))
    propagate(connection)
    connection.commit()
    return len(found)

# Carry resolved family links to the application records sharing that reference
def propagate(connection: sqlite3.Connection) -> None:
    connection.execute("\n        UPDATE application SET\n          family_id=(SELECT family_id FROM publication WHERE reference=primary_reference),\n          resolution_source=(SELECT source FROM publication WHERE reference=primary_reference)\n        WHERE family_id IS NULL AND EXISTS (\n          SELECT 1 FROM publication WHERE reference=primary_reference AND status='resolved'\n        )\n        ")
    connection.execute("\n        UPDATE application SET\n          family_id=(SELECT family_id FROM publication WHERE reference=alternate_reference),\n          resolution_source=(SELECT source FROM publication WHERE reference=alternate_reference)\n        WHERE family_id IS NULL AND alternate_reference<>'' AND EXISTS (\n          SELECT 1 FROM publication WHERE reference=alternate_reference AND status='resolved'\n        )\n        ")
    connection.execute("\n        UPDATE publication SET status='pending'\n        WHERE status='blocked' AND reference IN (\n          SELECT a.alternate_reference FROM application a\n          JOIN publication p ON p.reference=a.primary_reference\n          WHERE a.family_id IS NULL AND p.status IN ('not_found','error','ambiguous')\n        )\n        ")

# Extract application filing dates without substituting publication dates
def parse_application_dates(content: bytes) -> list[tuple[str, ...]]:
    """Retain application-reference dates only, with exact identifiers and XML lineage"""
    records = []
    root = ET.fromstring(content)
    local = lambda node: node.tag.rsplit('}', 1)[-1]
    for document in root.iter():
        if local(document) != 'exchange-document':
            continue
        country, number = (document.get('country', '').strip(), document.get('doc-number', '').strip())
        if not country or not number:
            continue
        reference = f'{country}.{number}'
        for application in document.iter():
            if local(application) != 'application-reference':
                continue
            xml = ET.tostring(application, encoding='unicode')
            record_id = uuid4().hex
            for identifier in application:
                if local(identifier) != 'document-id':
                    continue
                fields = {local(child): (child.text or '').strip() for child in identifier}
                if fields.get('doc-number') and fields.get('date'):
                    records.append((reference, fields.get('country', ''), fields['doc-number'], identifier.get('document-id-type', ''), fields['date'], record_id, xml))
    return records

def saved_date_schema(connection):
    """Identify the two supported saved OPS formats without a dated task marker"""
    columns = {r[1] for r in connection.execute('PRAGMA table_info(application)')}
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'appln_id', 'publication_key', 'payload'} <= columns:
        return 'application_payload'
    if {'appln_id', 'regpat_source', 'source_application_number', 'primary_reference', 'alternate_reference'} <= columns and 'application_date_evidence' in tables:
        return 'ops_application_reference'
    raise ValueError('Unsupported OPS cache schema; use an application-payload cache or ops-resolve state')


def iter_saved_application_dates(connection):
    """Yield every exact-linked date, including conflicts for reconciliation"""
    if saved_date_schema(connection) == 'application_payload':
        for aid, reference, payload in connection.execute('SELECT appln_id,publication_key,payload FROM application'):
            record = json.loads(payload)
            for value in (record.get('filing_dates') or []) + ([record['filing_date']] if record.get('filing_date') else []):
                yield aid, reference, value
    else:
        # Same application-number/country and publication-reference guards as
        # application_filing_date; retaining all dates exposes disagreements
        sql = """SELECT a.appln_id,e.reference,e.filing_date FROM application a
                 JOIN application_date_evidence e
                 ON (e.reference=a.primary_reference OR e.reference=a.alternate_reference)
                 AND e.application_number=trim(a.source_application_number)
                 AND e.application_country=CASE
                     WHEN instr(upper(a.regpat_source),'PCT')>0 THEN 'WO'
                     WHEN instr(upper(a.regpat_source),'EPO')>0 THEN 'EP' END"""
        yield from connection.execute(sql)


def application_filing_date(connection, *, references, application_number, application_country):
    """Select a unique valid date only for the exact supplied application identity"""
    matches = []
    for reference in references:
        matches.extend(connection.execute('SELECT filing_date FROM application_date_evidence WHERE reference=? AND application_number=? AND application_country=?', (reference, application_number.strip(), application_country.strip())))
    if not matches:
        return {'status': 'missing', 'filing_date': None, 'filing_year': None}
    dates = {row[0] for row in matches}
    if len(dates) != 1:
        return {'status': 'conflict', 'filing_date': None, 'filing_year': None}
    date = next(iter(dates))
    try:
        if len(date) != 8 or not date.isascii() or (not date.isdigit()):
            raise ValueError('invalid application date')
        datetime.strptime(date, '%Y%m%d')
    except ValueError:
        return {'status': 'invalid', 'filing_date': None, 'filing_year': None}
    return {'status': 'valid', 'filing_date': date, 'filing_year': int(date[:4]), 'source': 'epo_ops_application_reference', 'application_number': application_number, 'application_country': application_country, 'references': list(references)}

def parse_response(content: bytes) -> dict[str, tuple[str, str]]:
    results: dict[str, tuple[str, str]] = {}
    root = ET.fromstring(content)
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'exchange-document':
            continue
        country = element.attrib.get('country', '').strip()
        number = element.attrib.get('doc-number', '').strip()
        if not country or not number:
            continue
        reference = f'{country}.{number}'
        family = normalize_family(element.attrib.get('family-id', ''))
        status = element.attrib.get('status', '').strip()
        results[reference] = ('resolved' if family else 'not_found', family or status)
    return results

def fetch_batch_resilient(client: OPSClient, references: list[str]) -> dict[str, tuple[str, str, str]]:
    """Fetch a batch, splitting HTTP 400 batches until bad references are isolated"""
    try:
        response = client.fetch_published_biblio_batch(references)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 0
        if status_code == 400 and len(references) > 1:
            midpoint = len(references) // 2
            return {**fetch_batch_resilient(client, references[:midpoint]), **fetch_batch_resilient(client, references[midpoint:])}
        if status_code in {400, 404} and len(references) == 1:
            if status_code == 400:
                body = exc.response.text[:500] if exc.response is not None else ''
                raise OPSHTTP400Stop(f'OPS returned HTTP 400 for {references[0]}; preserving it as pending. Response: {body}') from exc
            return {references[0]: ('not_found', '', f'http_{status_code}_for_individual_reference')}
        raise
    if hasattr(client, 'application_dates'):
        client.application_dates.extend(parse_application_dates(response.content))
    parsed = parse_response(response.content)
    outcomes: dict[str, tuple[str, str, str]] = {}
    for reference in references:
        status, value = parsed.get(reference, ('not_found', 'absent_from_response'))
        outcomes[reference] = (status, value if status == 'resolved' else '', '' if status == 'resolved' else value)
    return outcomes

def write_summary(connection: sqlite3.Connection, path: Path, *, base_resolved: int, total: int, target: int) -> dict[str, object]:
    newly_resolved = connection.execute('SELECT COUNT(*) FROM application WHERE family_id IS NOT NULL').fetchone()[0]
    publication_counts = dict(connection.execute('SELECT status,COUNT(*) FROM publication GROUP BY status ORDER BY status'))
    resolved = base_resolved + newly_resolved
    payload = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'total_regpat_applications': total, 'base_patstat_resolved': base_resolved, 'newly_resolved_applications': newly_resolved, 'resolved_applications': resolved, 'coverage_pct': 100.0 * resolved / total if total else 0.0, 'target_resolved_applications': target, 'target_coverage_pct': 100.0 * target / total if total else 0.0, 'target_reached': resolved >= target, 'publication_status_counts': publication_counts}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(payload, sort_keys=True), flush=True)
    return payload

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--queue', required=True, type=Path)
    parser.add_argument('--archive-cache', type=Path)
    parser.add_argument('--state-db', required=True, type=Path)
    parser.add_argument('--summary', required=True, type=Path)
    parser.add_argument('--base-resolved', type=int, default=0)
    parser.add_argument('--total', type=int)
    parser.add_argument('--target', type=int)
    parser.add_argument('--quota-state', required=True, type=Path)
    parser.add_argument('--retry-errors', action='store_true', help='reset previously failed OPS publications to pending before resuming')
    parser.add_argument('--retry-http-400', action='store_true', help='reset references incorrectly labelled not-found by the old HTTP-400 handler')
    parser.add_argument('--request-delay-seconds', type=float, default=1.0)
    args = parser.parse_args()
    args.state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.state_db, timeout=120)
    connection.execute('PRAGMA busy_timeout=120000')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    initialize(connection, args.queue)
    if args.archive_cache is not None:
        preload_cache(connection, args.archive_cache)
    if args.total is None:
        args.total = args.base_resolved + connection.execute('SELECT COUNT(*) FROM application').fetchone()[0]
    if args.target is None:
        args.target = args.total
    if not 0 <= args.base_resolved <= args.target <= args.total:
        raise ValueError('Require 0 <= base-resolved <= target <= total')
    if args.retry_errors:
        connection.execute("UPDATE publication SET status='pending',error='' WHERE status='error'")
        connection.commit()
    if args.retry_http_400:
        connection.execute("UPDATE publication SET status='pending',error='' WHERE status='not_found' AND error='http_400_for_individual_reference'")
        connection.commit()
    summary = write_summary(connection, args.summary, base_resolved=args.base_resolved, total=args.total, target=args.target)
    if summary['target_reached']:
        return
    key = os.environ.get('OPS_KEY', '').strip()
    secret = os.environ.get('OPS_SECRET', '').strip()
    if not key or not secret:
        raise SystemExit('OPS_KEY and OPS_SECRET are required')
    client = OPSClient(key, secret, quota_state=args.quota_state, request_delay_seconds=args.request_delay_seconds)
    while True:
        refs = [row[0] for row in connection.execute("SELECT reference FROM publication WHERE status='pending' ORDER BY reference LIMIT 25")]
        if not refs:
            break
        now = datetime.now(timezone.utc).isoformat()
        try:
            client.application_dates.clear()
            outcomes = fetch_batch_resilient(client, refs)
            connection.executemany('INSERT OR IGNORE INTO application_date_evidence VALUES(?,?,?,?,?,?,?)', client.application_dates)
            for reference in refs:
                status, family_id, error = outcomes[reference]
                connection.execute("UPDATE publication SET status=?,family_id=?,source='epo_ops',attempted_at_utc=?,error=? WHERE reference=?", (status, family_id or None, now, error, reference))
        except (SystemExit, OPSHTTP400Stop) as exc:
            connection.commit()
            write_summary(connection, args.summary, base_resolved=args.base_resolved, total=args.total, target=args.target)
            connection.close()
            print(f'Safe stop: {exc}', flush=True)
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower():
                raise
            connection.rollback()
            print('Transient SQLite lock; preserving the batch as pending and retrying after 5 seconds.', flush=True)
            time.sleep(5)
            continue
        except Exception as exc:
            connection.rollback()
            for reference in refs:
                connection.execute("UPDATE publication SET status='error',source='epo_ops',attempted_at_utc=?,error=? WHERE reference=?", (now, f'{type(exc).__name__}: {exc}', reference))
        propagate(connection)
        connection.commit()
        summary = write_summary(connection, args.summary, base_resolved=args.base_resolved, total=args.total, target=args.target)
        if summary['target_reached']:
            break
    connection.close()
if __name__ == '__main__':
    main()
