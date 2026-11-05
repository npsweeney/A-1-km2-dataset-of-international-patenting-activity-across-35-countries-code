"""Durable Nominatim response cache and retry evidence"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

@dataclass
# Keep response caching, request limits and bounded retries in one client
class NominatimClient:
    endpoint: str
    cache_dir: Path
    user_agent: str
    delay_seconds: float = 0.0
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    max_in_flight: int = 4
    request_budget: int | None = None
    event_ledger_path: Path | None = None
    resume_events: bool = False
    data_identity: dict[str, str] | None = None
    _event_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _event_sequence: int = field(default=0, init=False, repr=False)
    _run_errors: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _run_errors_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cache_connection: sqlite3.Connection = field(init=False, repr=False)
    _request_slots: threading.BoundedSemaphore = field(init=False, repr=False)
    _in_flight: int = field(default=0, init=False, repr=False)
    _telemetry: dict[str, Any] = field(default_factory=lambda: {'attempted_requests': 0, 'cache_hits': 0, 'http_503_count': 0, 'retry_count': 0, 'terminal_transient_failures': 0, 'request_budget_exhausted': 0, 'response_bytes': 0, 'cache_writes': 0, 'cache_lock_wait_ns': 0, 'cache_lock_errors': 0, 'in_flight_peak': 0}, init=False, repr=False)

    def _count(self, key: str, amount: int=1) -> None:
        with self._run_errors_lock:
            self._telemetry[key] += amount

    def telemetry(self) -> dict[str, Any]:
        with self._run_errors_lock:
            return dict(self._telemetry)

    def _event(self, kind: str, **values: Any) -> None:
        if self.event_ledger_path is None:
            return
        with self._event_lock:
            self._event_sequence += 1
            with self.event_ledger_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'sequence': self._event_sequence, 'kind': kind, 'observed_at_utc': datetime.now(UTC).isoformat(), **values}, sort_keys=True) + '\n')

    def __post_init__(self) -> None:
        if self.event_ledger_path is not None:
            if self.resume_events and self.event_ledger_path.exists() and self.event_ledger_path.stat().st_size:
                with self.event_ledger_path.open('rb') as previous:
                    position = previous.seek(0, 2)
                    tail = b''
                    while position and b'\n' not in tail.rstrip(b'\n'):
                        size = min(8192, position)
                        position -= size
                        previous.seek(position)
                        tail = previous.read(size) + tail
                    self._event_sequence = json.loads(tail.splitlines()[-1])['sequence']
            self.event_ledger_path.touch(mode=384, exist_ok=self.resume_events)
        if self.max_in_flight < 1:
            raise ValueError('max_in_flight must be at least 1')
        if self.request_budget is not None and self.request_budget < 1:
            raise ValueError('request_budget must be positive')
        self._request_slots = threading.BoundedSemaphore(self.max_in_flight)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_connection = sqlite3.connect(self.cache_dir / 'nominatim-cache.sqlite3', timeout=30, check_same_thread=False)
        self._cache_connection.execute('PRAGMA journal_mode=WAL')
        self._cache_connection.execute('PRAGMA synchronous=NORMAL')
        self._cache_connection.execute('CREATE TABLE IF NOT EXISTS responses (cache_key TEXT PRIMARY KEY, url TEXT NOT NULL, payload TEXT NOT NULL, created_at_utc TEXT NOT NULL)')
        self._cache_connection.commit()
        initial_entries = int(self._cache_connection.execute('SELECT count(*) FROM responses').fetchone()[0])
        cache_path = str((self.cache_dir / 'nominatim-cache.sqlite3').resolve())
        # Bind reuse to the supplied database revision without changing saved response keys
        self._cache_connection.execute('CREATE TABLE IF NOT EXISTS service_identity(value TEXT NOT NULL)')
        saved = self._cache_connection.execute('SELECT value FROM service_identity').fetchall()
        encoded = json.dumps(self.data_identity, sort_keys=True) if self.data_identity is not None else None
        if saved and saved != [(encoded,)]:
            self._cache_connection.close()
            raise ValueError('Response cache database identity differs; preserve it and use a compatible cache')
        if encoded is not None and not saved:
            if initial_entries:
                self._cache_connection.close()
                raise ValueError('Nonempty response cache has no verified database identity; preserve it and use an identified cache')
            self._cache_connection.execute('INSERT INTO service_identity VALUES(?)', (encoded,))
        self._cache_connection.commit()
        self._telemetry['initial_cache_entries'] = initial_entries
        self._telemetry['cache_path'] = cache_path

    def _get_cached(self, cache_key: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        started = time.perf_counter_ns()
        try:
            with self._cache_lock:
                self._count('cache_lock_wait_ns', time.perf_counter_ns() - started)
                found = self._cache_connection.execute('SELECT payload,url,created_at_utc,cache_key FROM responses WHERE cache_key = ?', (cache_key,)).fetchone()
        except sqlite3.Error:
            self._count('cache_lock_errors')
            raise
        if found:
            payload = json.loads(found[0])
            return (payload, {'cache_origin': 'sqlite', 'cached_request_url': found[1], 'cached_at_utc': found[2]})
        return None

    def _store_cached(self, cache_key: str, url: str, payload: list[dict[str, Any]]) -> None:
        started = time.perf_counter_ns()
        try:
            with self._cache_lock:
                self._count('cache_lock_wait_ns', time.perf_counter_ns() - started)
                cursor = self._cache_connection.execute('INSERT OR IGNORE INTO responses(cache_key, url, payload, created_at_utc) VALUES (?, ?, ?, ?)', (cache_key, url, json.dumps(payload, ensure_ascii=False, sort_keys=True), datetime.now(UTC).isoformat()))
                self._cache_connection.commit()
                if cursor.rowcount:
                    self._count('cache_writes')
                    self._event('cache_write', cache_key=cache_key)
        except sqlite3.Error:
            self._count('cache_lock_errors')
            raise

    def search(self, query: str, country: str, limit: int, *, structured: dict[str, str] | None=None, viewbox: tuple[float, float, float, float] | None=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        params = dict(structured) if structured else {'q': query}
        params.update({'format': 'jsonv2', 'addressdetails': '1', 'namedetails': '1', 'extratags': '1', 'limit': str(limit)})
        if country:
            params['countrycodes'] = country.lower()
        if viewbox is not None:
            import math
            if len(viewbox) != 4 or not all((math.isfinite(v) for v in viewbox)):
                raise ValueError('Invalid regional viewbox')
            west, south, east, north = viewbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError('Invalid regional viewbox extent')
            params['viewbox'] = f'{west},{north},{east},{south}'
            params['bounded'] = '1'
        request_path = '/search?' + urllib.parse.urlencode(params)
        url = self.endpoint.rstrip('/') + request_path
        cache_identity = json.dumps({'endpoint': self.endpoint, 'parameters': params}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        cache_key = cache_identity
        cached = self._get_cached(cache_key)
        if cached is not None:
            self._count('cache_hits')
            self._event('cache_hit', cache_key=cache_key)
            payload, cache_provenance = cached
            return (payload, {'url': url, 'cache_key': cache_key, 'cache_hit': True, 'request_parameters': params, **cache_provenance})
        with self._run_errors_lock:
            prior_error = self._run_errors.get(cache_key)
        if prior_error:
            raise NominatimQueryError(prior_error['message'], url=url, cache_key=cache_key, attempts=prior_error['attempts'], attempt_errors=prior_error['attempt_errors'], retryable=prior_error.get('retryable', False), status_code=prior_error.get('status_code'), response_bytes=prior_error.get('response_bytes', 0))
        request = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
        attempt_errors: list[str] = []
        network_response_bytes = 0
        for attempt in range(1, self.max_attempts + 1):
            attempt_bytes = 0
            status_code = None
            with self._run_errors_lock:
                if self.request_budget is not None and self._telemetry['attempted_requests'] >= self.request_budget:
                    self._telemetry['request_budget_exhausted'] = 1
                    raise RequestBudgetExceeded(f'total request budget exhausted at {self.request_budget}')
                self._telemetry['attempted_requests'] += 1
            try:
                with self._request_slots:
                    with self._run_errors_lock:
                        self._in_flight += 1
                        self._telemetry['in_flight_peak'] = max(self._telemetry['in_flight_peak'], self._in_flight)
                    try:
                        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                            body = response.read()
                            status_code = getattr(response, 'status', 200)
                            attempt_bytes = len(body)
                        self._count('response_bytes', len(body))
                        network_response_bytes += len(body)
                        payload = json.loads(body.decode())
                    finally:
                        with self._run_errors_lock:
                            self._in_flight -= 1
            except Exception as exc:
                status_code = exc.code if isinstance(exc, HTTPError) else None
                if isinstance(exc, HTTPError):
                    error_body = exc.read()
                    attempt_bytes += len(error_body)
                    self._count('response_bytes', len(error_body))
                    network_response_bytes += len(error_body)
                if status_code == 503:
                    self._count('http_503_count')
                self._event('network_attempt', cache_key=cache_key, attempt=attempt, status_code=status_code, response_bytes=attempt_bytes, error=type(exc).__name__)
                error = f'{type(exc).__name__}: {exc}'
                attempt_errors.append(error)
                retryable = not isinstance(exc, HTTPError) or exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self.max_attempts:
                    if retryable:
                        self._count('terminal_transient_failures')
                    with self._run_errors_lock:
                        self._run_errors[cache_key] = {'message': error, 'attempts': attempt, 'attempt_errors': list(attempt_errors), 'retryable': retryable, 'status_code': status_code, 'response_bytes': network_response_bytes}
                    raise NominatimQueryError(error, url=url, cache_key=cache_key, attempts=attempt, attempt_errors=attempt_errors, retryable=retryable, status_code=status_code, response_bytes=network_response_bytes) from exc
                self._count('retry_count')
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * 2 ** (attempt - 1))
            else:
                self._event('network_attempt', cache_key=cache_key, attempt=attempt, status_code=status_code, response_bytes=attempt_bytes, error=None)
                break
        self._store_cached(cache_key, url, payload)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return (payload, {'url': url, 'cache_key': cache_key, 'cache_hit': False, 'cache_origin': 'network', 'attempts': attempt, 'attempt_errors': attempt_errors, 'request_parameters': params, 'response_bytes': network_response_bytes})

class NominatimQueryError(RuntimeError):

    def __init__(self, message: str, *, url: str, cache_key: str, attempts: int, attempt_errors: list[str], retryable: bool=False, status_code: int | None=None, response_bytes: int=0) -> None:
        super().__init__(message)
        self.meta = {'url': url, 'cache_key': cache_key, 'cache_hit': False, 'attempts': attempts, 'attempt_errors': attempt_errors, 'retryable': retryable, 'status_code': status_code, 'response_bytes': response_bytes}

class RequestBudgetExceeded(RuntimeError):
    """Raised before a network request can exceed the run's declared total budget"""
