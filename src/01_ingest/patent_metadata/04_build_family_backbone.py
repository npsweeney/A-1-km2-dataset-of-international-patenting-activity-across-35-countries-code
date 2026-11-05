"""Build application-to-family mappings from PATSTAT and OPS"""
from __future__ import annotations
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def source_state(path: Path) -> dict:
    return {'path': str(path.resolve())}

def _columns(connection: sqlite3.Connection, schema: str, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA {schema}.table_info({table})')}

def _add_v2_columns(connection: sqlite3.Connection) -> None:
    existing = _columns(connection, 'main', 'regpat_family_backbone')
    additions = {'family_resolution_conflict': 'INTEGER NOT NULL DEFAULT 0', 'regpat_source': 'TEXT', 'source_application_number': 'TEXT', 'source_publication_number': 'TEXT', 'ops_primary_reference': 'TEXT', 'ops_alternate_reference': 'TEXT'}
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE regpat_family_backbone ADD COLUMN {name} {declaration}')

# Combine the recorded family evidence into the application-to-family backbone
def build(base_db: Path | None, ops_db: Path | None, output_db: Path) -> dict:
    if base_db is None and ops_db is None:
        raise ValueError('Supply a PATSTAT family database or an OPS cache')
    if any((path is not None and path.resolve() == output_db.resolve() for path in (base_db, ops_db))):
        raise ValueError('Output must differ from source databases')
    output_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_db, uri=True)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.execute('PRAGMA temp_store=FILE')
    connection.executescript('\n        CREATE TABLE IF NOT EXISTS regpat_family_backbone (\n            appln_id TEXT PRIMARY KEY,\n            canonical_family_id TEXT NOT NULL,\n            family_id_kind TEXT NOT NULL,\n            resolution_source TEXT NOT NULL,\n            resolution_status TEXT NOT NULL,\n            patstat_docdb_family_id TEXT,\n            patstat_inpadoc_family_id TEXT,\n            ops_docdb_family_id TEXT,\n            refreshed_at_utc TEXT NOT NULL\n        ) WITHOUT ROWID;\n        CREATE INDEX IF NOT EXISTS backbone_canonical_family\n            ON regpat_family_backbone(canonical_family_id);\n        CREATE INDEX IF NOT EXISTS backbone_resolution_status\n            ON regpat_family_backbone(resolution_status);\n        CREATE TABLE IF NOT EXISTS backbone_metadata (\n            key TEXT PRIMARY KEY,\n            value_json TEXT NOT NULL\n        ) WITHOUT ROWID;\n        ')
    _add_v2_columns(connection)
    connection.execute('ATTACH DATABASE ? AS ops', (str(ops_db.resolve().as_uri()) + '?mode=ro' if ops_db else ':memory:',))
    if ops_db is None:
        connection.executescript('CREATE TABLE ops.application(appln_id TEXT, family_id TEXT, primary_reference TEXT); CREATE TABLE ops.publication(reference TEXT,status TEXT);')
    connection.execute('ATTACH DATABASE ? AS base', (str(base_db.resolve().as_uri()) + '?mode=ro' if base_db else ':memory:',))
    if base_db is None:
        connection.execute('CREATE TABLE base.application_family AS SELECT appln_id, NULL AS docdb_family_id, NULL AS inpadoc_family_id FROM ops.application')
    ops_columns = _columns(connection, 'ops', 'application')
    optional = lambda name: f'o.{name}' if name in ops_columns else 'NULL'
    refreshed = datetime.now(timezone.utc).isoformat()
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('DROP TABLE IF EXISTS temp.backbone_refresh')
    connection.execute(f"\n        CREATE TEMP TABLE backbone_refresh AS\n        SELECT\n            b.appln_id,\n            CASE\n                WHEN NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') IS NOT NULL\n                    THEN 'docdb:' || NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '')\n                WHEN NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') IS NOT NULL\n                    THEN 'docdb:' || NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '')\n                ELSE 'regpat-appln:' || b.appln_id\n            END AS canonical_family_id,\n            CASE\n                WHEN NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') IS NOT NULL OR NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') IS NOT NULL\n                    THEN 'docdb'\n                ELSE 'regpat_application_fallback'\n            END AS family_id_kind,\n            CASE\n                WHEN NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') IS NOT NULL THEN 'patstat'\n                WHEN NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') IS NOT NULL THEN 'epo_ops'\n                ELSE 'unresolved'\n            END AS resolution_source,\n            CASE\n                WHEN NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') IS NOT NULL OR NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') IS NOT NULL\n                    THEN 'resolved'\n                WHEN p.status = 'pending' THEN 'ops_pending'\n                WHEN p.status = 'error' THEN 'ops_error'\n                ELSE 'unresolved'\n            END AS resolution_status,\n            NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') AS patstat_docdb_family_id,\n            NULLIF(NULLIF(TRIM(CAST(b.inpadoc_family_id AS TEXT)), '0'), '') AS patstat_inpadoc_family_id,\n            NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') AS ops_docdb_family_id,\n            ? AS refreshed_at_utc,\n            CASE WHEN NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') IS NOT NULL\n                       AND NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') IS NOT NULL\n                       AND NULLIF(NULLIF(TRIM(CAST(b.docdb_family_id AS TEXT)), '0'), '') <> NULLIF(NULLIF(TRIM(CAST(o.family_id AS TEXT)), '0'), '') THEN 1 ELSE 0 END,\n            {optional('regpat_source')},\n            {optional('source_application_number')},\n            {optional('source_publication_number')},\n            {optional('primary_reference')},\n            {optional('alternate_reference')}\n        FROM base.application_family AS b\n        LEFT JOIN ops.application AS o USING (appln_id)\n        LEFT JOIN ops.publication AS p ON p.reference = o.primary_reference\n        ", (refreshed,))
    connection.execute('\n        INSERT INTO regpat_family_backbone (\n            appln_id,canonical_family_id,family_id_kind,resolution_source,\n            resolution_status,patstat_docdb_family_id,patstat_inpadoc_family_id,\n            ops_docdb_family_id,refreshed_at_utc,family_resolution_conflict,\n            regpat_source,source_application_number,source_publication_number,\n            ops_primary_reference,ops_alternate_reference\n        ) SELECT * FROM backbone_refresh\n        WHERE 1\n        ON CONFLICT(appln_id) DO UPDATE SET\n            canonical_family_id=excluded.canonical_family_id,\n            family_id_kind=excluded.family_id_kind,\n            resolution_source=excluded.resolution_source,\n            resolution_status=excluded.resolution_status,\n            patstat_docdb_family_id=excluded.patstat_docdb_family_id,\n            patstat_inpadoc_family_id=excluded.patstat_inpadoc_family_id,\n            ops_docdb_family_id=excluded.ops_docdb_family_id,\n            refreshed_at_utc=excluded.refreshed_at_utc,\n            family_resolution_conflict=excluded.family_resolution_conflict,\n            regpat_source=excluded.regpat_source,\n            source_application_number=excluded.source_application_number,\n            source_publication_number=excluded.source_publication_number,\n            ops_primary_reference=excluded.ops_primary_reference,\n            ops_alternate_reference=excluded.ops_alternate_reference\n        ')
    connection.execute('DELETE FROM regpat_family_backbone WHERE appln_id NOT IN (SELECT appln_id FROM backbone_refresh)')
    counts = dict(connection.execute('SELECT resolution_status,COUNT(*) FROM regpat_family_backbone GROUP BY resolution_status'))
    sources = {'patstat': source_state(base_db) if base_db else None, 'epo_ops': source_state(ops_db) if ops_db else None}
    summary = {'schema_version': 'regpat_family_backbone_v2', 'generated_at_utc': refreshed, 'total_regpat_applications': sum(counts.values()), 'status_counts': counts, 'resolved_applications': counts.get('resolved', 0), 'unresolved_applications': sum((value for key, value in counts.items() if key != 'resolved')), 'coverage_pct': round(100 * counts.get('resolved', 0) / sum(counts.values()), 6) if sum(counts.values()) else 0.0, 'canonical_id_rule': 'PATSTAT DOCDB, then EPO OPS DOCDB, then explicit REGPAT application fallback', 'family_resolution_conflicts': connection.execute('SELECT COUNT(*) FROM regpat_family_backbone WHERE family_resolution_conflict=1').fetchone()[0], 'sources': sources}
    for key, value in (('summary', summary), ('sources', sources)):
        connection.execute('INSERT INTO backbone_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json', (key, json.dumps(value, sort_keys=True)))
    connection.commit()
    connection.close()
    return summary

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-family-db', type=Path)
    parser.add_argument('--ops-cache-db', type=Path)
    parser.add_argument('--output-db', required=True, type=Path)
    parser.add_argument('--summary', required=True, type=Path)
    args = parser.parse_args()
    summary = build(args.base_family_db, args.ops_cache_db, args.output_db)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps(summary, indent=2, sort_keys=True))
if __name__ == '__main__':
    main()
