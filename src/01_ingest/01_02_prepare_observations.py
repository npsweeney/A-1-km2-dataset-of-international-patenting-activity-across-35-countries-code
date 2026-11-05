"""Prepare address observations and source-row links"""
from __future__ import annotations
import csv
import json
import re
from dataclasses import dataclass
from typing import Any
from cleaning.clean_addresses import CLEANING_FIELDS, cleaned_stage
INPUT_FIELDS = ('appln_id', 'person_id', 'reg_code', 'address', 'locality', 'postal_code', 'ctry_code')
ADDRESS_KEY_FIELDS = ('address', 'locality', 'postal_code', 'ctry_code', 'reg_code')
ACTOR_FIELDS = ('actor', 'role')
YEAR_FIELDS = ('year', 'patent_year', 'appln_filing_year')
US_ZIP_RE = re.compile('(?<!\\d)(\\d{5})(?:-(\\d{4}))?(?!\\d)')
US_TERMINAL_ZIP_RE = re.compile('(?<!\\d)(\\d{5})(?:-(\\d{4}))?(?!\\d)\\s*$')
SE_POSTCODE_RE = re.compile('^(?:SE?\\s*-?\\s*)?(\\d{3})\\s*(\\d{2})$', re.IGNORECASE)
SE_LEADING_POSTCODE_RE = re.compile('^\\s*(?:S\\s*-?\\s*)?(\\d{3})\\s+(\\d{2})(?:\\s+|$)', re.IGNORECASE)

def stable_id(row: dict[str, str]) -> str:
    """Return a stable observation identifier from exact source strings"""
    return 'address:' + json.dumps([row.get(field, '') for field in ADDRESS_KEY_FIELDS], ensure_ascii=False, separators=(',', ':'))

def clean_row(row: dict[str, Any]) -> dict[str, str]:
    """Read the supplied address fields"""
    cleaned = {field: str(row.get(field) or '').strip() for field in INPUT_FIELDS}
    if not cleaned['locality']:
        cleaned['locality'] = str(row.get('city') or '').strip()
    if any((field in row for field in CLEANING_FIELDS)):
        cleaned.update({field: str(row[field] or '') for field in CLEANING_FIELDS if field in row})
    return cleaned


# Recover labelled postal information for queries while preserving the original source fields
def derive_query_postcode(row: dict[str, str]) -> tuple[str, str]:
    """Prepare the query postcode and retain the original source fields"""
    source = row.get('postal_code', '').strip()
    country = row.get('ctry_code', '').upper()
    if country == 'GB':
        compact = re.sub('\\s+', '', source).upper()
        if re.fullmatch('(?:[A-Z]{1,2}\\d[A-Z\\d]?\\d[A-Z]{2}|GIR0AA)', compact):
            return (compact[:-3] + ' ' + compact[-3:], 'source_uk_postcode_spacing')
        return (source, 'source')
    if country == 'NL':
        complete = re.fullmatch('(?:NL-)?([1-9]\\d{3})\\s*([A-Z]{2})', source, re.I)
        if complete:
            return (' '.join(complete.groups()).upper(), 'source_valid_nl_postcode')
        matches = re.findall('(?:^|,)\\s*(?:NL-)?([1-9]\\d{3})\\s*([A-Z]{2})(?=\\s+[A-Za-zÀ-ÿ]|\\s*(?:,|$))', row.get('address', ''), re.I)
        matches = set(((a, b.upper()) for a, b in matches))
        if len(matches) == 1:
            number, letters = next(iter(matches))
            if not source or source == number:
                return (f'{number} {letters}', 'derived_address_nl_postcode')
        return (source, 'invalid_or_ambiguous_nl_postcode' if source else 'missing_nl_postcode')
    if country == 'LU' and (not source):
        matches = re.findall('(?:^|,)\\s*(?:L-)?(\\d{4})\\s+Luxembourg(?:-Ville)?\\s*(?=,|$)', row.get('address', ''), re.I)
        if len(set(matches)) == 1:
            return (matches[0], 'derived_explicit_luxembourg_postcode')
    if row.get('ctry_code', '').upper() == 'DE' and (not source):
        matches = [re.fullmatch("(?:D(?:E)?-)?(\\d{5})([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]*)", part.strip()) for part in row.get('address', '').split(',')]
        matches = [m for m in matches if m and (not re.search('(?i)straße|strasse|weg\\b|platz\\b|allee\\b', m.group(2)))]
        if len(matches) == 1:
            return (matches[0].group(1), 'derived_joined_address_de_postcode')
        return (source, 'source')
    if row.get('ctry_code', '').upper() == 'PL':
        match = re.fullmatch('(?:PL-)?(\\d{2})[- ]?(\\d{3})', source, re.IGNORECASE)
        if match:
            return ('-'.join(match.groups()), 'source_valid_pl_postcode')
        return (source, 'invalid_or_ambiguous_pl_postcode')
    if row.get('ctry_code', '').upper() == 'SE':
        source_match = SE_POSTCODE_RE.fullmatch(source)
        if source_match:
            return (''.join(source_match.groups()), 'source_valid_se_postcode')
        address_match = SE_LEADING_POSTCODE_RE.match(row.get('address', ''))
        if address_match:
            return (''.join(address_match.groups()), 'derived_leading_address_se_postcode')
        return (source, 'invalid_or_ambiguous_se_postcode')
    if row.get('ctry_code', '').upper() != 'US':
        return (source, 'source')
    if US_ZIP_RE.fullmatch(source):
        return (source, 'source_valid_us_zip')
    match = US_TERMINAL_ZIP_RE.search(row.get('address', ''))
    if match:
        base, extension = match.groups()
        value = f'{base}-{extension}' if extension else base
        return (value, 'derived_terminal_address_us_zip')
    return ('', 'invalid_or_ambiguous_us_zip')

class DiskRows:
    """Ordered rows backed by SQLite; iteration keeps only one row in memory.

    Mutations made by the existing preflight loops are saved when iteration
    advances. Source order, exact text identities and repeated links are retained
    """
    def __init__(self, connection, name, *, unique=False, existing=None):
        if name not in {'observations', 'address_links', 'source_links', 'decisions'}:
            raise ValueError('Unknown input-index table')
        self.connection, self.name = connection, name
        if existing is not None:
            self.fields = set(existing['fields'])
            self.count = existing['count']
            return
        connection.execute(f'CREATE TABLE {name}(seq INTEGER PRIMARY KEY, identity TEXT, payload TEXT NOT NULL)')
        connection.execute(f'CREATE {"UNIQUE " if unique else ""}INDEX {name}_identity ON {name}(identity)')
        self.fields = set()
        self.count = 0

    def __len__(self):
        return self.count

    def get(self, identity):
        row = self.connection.execute(f'SELECT payload FROM {self.name} WHERE identity=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def append(self, row):
        self.connection.execute(f'INSERT INTO {self.name}(identity,payload) VALUES(?,?)',
                                (row.get('observation_id'), json.dumps(row, ensure_ascii=False)))
        self.fields.update(row)
        self.count += 1
        if self.count % 10000 == 0:
            self.connection.commit()

    def __iter__(self):
        for seq, payload in self.connection.execute(f'SELECT seq,payload FROM {self.name} ORDER BY seq'):
            row = json.loads(payload)
            yield row
            changed = json.dumps(row, ensure_ascii=False)
            if changed != payload:
                self.connection.execute(f'UPDATE {self.name} SET payload=? WHERE seq=?', (changed, seq))
        self.connection.commit()

    def retain_first(self, count):
        self.connection.execute(f'DELETE FROM {self.name} WHERE seq NOT IN (SELECT seq FROM {self.name} ORDER BY seq LIMIT ?)', (count,))
        self.count = self.connection.execute(f'SELECT count(*) FROM {self.name}').fetchone()[0]

@dataclass(frozen=True)
class ObservationBatch:
    """Deduplicated observations plus the lossless source-row join-back"""
    observations: list[dict[str, str]] | DiskRows
    address_links: list[dict[str, int | str]] | DiskRows
    source_row_count: int

# Index distinct address inputs and their source application and person links
def read_observations(reader: csv.DictReader[str], *, max_rows: int | None=None, store=None, retain_links=True) -> ObservationBatch:
    """Read, exactly deduplicate, and retain links to every included source row.

    ``max_rows`` limits unique observations and
    then keeps every source link whose observation survived that limit
    """
    supplied_fields = set(reader.fieldnames or [])
    required = set(INPUT_FIELDS) - {'locality'}
    missing = sorted(required - supplied_fields)
    if 'locality' not in supplied_fields and 'city' not in supplied_fields:
        missing.append('locality (or city)')
    if missing:
        raise ValueError(f"Input is missing columns: {', '.join(missing)}")
    address_links = DiskRows(store, 'address_links') if store is not None else []
    unique_by_key = DiskRows(store, 'observations', unique=True) if store is not None else {}
    for source_row_number, raw_row in enumerate(reader, start=2):
        observation = clean_row(raw_row)
        observation['raw_input_json'] = json.dumps({field: raw_row.get(field) for field in reader.fieldnames or []}, ensure_ascii=False, sort_keys=True)
        observation['observation_id'] = stable_id(observation)
        query_postcode, query_postcode_method = derive_query_postcode(observation)
        observation['query_postal_code'] = query_postcode
        observation['query_postal_code_method'] = query_postcode_method
        cleaned_stage(observation)
        key = tuple((observation[field] for field in ADDRESS_KEY_FIELDS))
        previous = unique_by_key.get(observation['observation_id'] if store is not None else key)
        if previous is not None and any((previous.get(field) != observation.get(field) for field in CLEANING_FIELDS)):
            raise ValueError('Duplicate source address has conflicting cleaned-address columns')
        if store is not None:
            if previous is None:
                unique_by_key.append(observation)
        else:
            unique_by_key.setdefault(key, observation)
        if retain_links:
            address_links.append({'source_row_number': source_row_number, 'appln_id': observation['appln_id'], 'person_id': observation['person_id'], 'observation_id': observation['observation_id'], 'actor': next((str(raw_row.get(field) or '').strip() for field in ACTOR_FIELDS if str(raw_row.get(field) or '').strip()), ''), 'year': next((str(raw_row.get(field) or '').strip() for field in YEAR_FIELDS if str(raw_row.get(field) or '').strip()), '')})
    if store is not None:
        observations = unique_by_key
        if max_rows is not None:
            observations.retain_first(max_rows)
        store.execute('DELETE FROM address_links WHERE identity NOT IN (SELECT identity FROM observations)')
        address_links.count = store.execute('SELECT count(*) FROM address_links').fetchone()[0]
        store.commit()
    else:
        observations = list(unique_by_key.values())
        observations = observations[:max_rows] if max_rows is not None else observations
        retained_ids = {row['observation_id'] for row in observations}
        address_links = [row for row in address_links if str(row['observation_id']) in retained_ids]
    return ObservationBatch(observations, address_links, len(address_links))

# Link distinct addresses to source applications and roles
def read_source_links(reader, observations, *, allow_unselected=False, store=None, resume=False):
    """Restore stage-01 application links after address deduplication"""
    required = {'appln_id', 'person_id', 'observation_id', 'actor', 'patent_year', 'address', 'city', 'postal_code', 'reg_code', 'ctry_code'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Incomplete stage-01 application link table')
    selected = {row['observation_id'] for row in observations} if store is None else None
    if resume and (store is None or allow_unselected):
        raise ValueError('Partial link resume requires a complete disk-backed cohort')
    skipped = 0
    if resume and store.execute("SELECT 1 FROM sqlite_master WHERE name='source_links'").fetchone():
        count, maximum = store.execute('SELECT count(*),max(seq) FROM source_links').fetchone()
        if count != (maximum or 0):
            raise ValueError('Partial link index is not a contiguous source prefix')
        links = DiskRows(store, 'source_links', existing=dict(count=count,
                         fields=sorted(set(reader.fieldnames) | {'source_row_number', 'year'})))
        skipped = count
    else:
        links = DiskRows(store, 'source_links') if store is not None else []
    linked = set() if store is None else None
    if store is not None:
        store.execute('CREATE TABLE IF NOT EXISTS linked(identity TEXT PRIMARY KEY) WITHOUT ROWID')
        if resume:
            # A commit can occur between append and the coverage insert. Recover
            # that last committed row from the same transaction-safe prefix
            store.execute('INSERT OR IGNORE INTO linked SELECT identity FROM source_links')
            store.commit()
        from functools import lru_cache
        @lru_cache(maxsize=10000)
        def is_selected(identity):
            return store.execute('SELECT 1 FROM observations WHERE identity=?', (identity,)).fetchone() is not None
    from itertools import islice
    if skipped:
        consumed = sum(1 for _ in islice(reader, skipped))
        if consumed != skipped:
            raise ValueError('Source links are shorter than the committed checkpoint')
    for number, raw in enumerate(reader, start=2 + skipped):
        if None in raw or any((value is None for value in raw.values())):
            raise ValueError('Malformed application link row')
        identity = raw['observation_id']
        if identity != stable_id(clean_row(raw)):
            raise ValueError('Application link address identity differs from source fields')
        if not (is_selected(identity) if store is not None else identity in selected):
            if allow_unselected:
                continue
            raise ValueError('Application link has no input address')
        if not raw['appln_id'] or not raw['actor'] or (not raw['patent_year']):
            raise ValueError('Application link lacks application, role or filing year')
        link = dict(raw)
        link.update(source_row_number=raw.get('source_row', str(number)), year=raw['patent_year'])
        links.append(link)
        if store is not None:
            store.execute('INSERT OR IGNORE INTO linked VALUES(?)', (identity,))
        else:
            linked.add(identity)
    if store is not None:
        store.commit()
        complete = store.execute('SELECT count(*) FROM linked').fetchone()[0] == len(observations)
    else:
        complete = linked == selected
    if not complete:
        raise ValueError('An input address has no application link')
    return links
