"""所有权流转服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS title_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('registrar','finance','buyer','operations','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_parties (
    party_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_assets (
    asset_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    owner_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    custodian_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    operator_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    location TEXT NOT NULL,
    rights_revision INTEGER NOT NULL DEFAULT 1 CHECK(rights_revision > 0),
    restriction_revision INTEGER NOT NULL DEFAULT 1 CHECK(restriction_revision > 0),
    created_by TEXT NOT NULL REFERENCES title_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rights_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES title_assets(asset_id),
    seq INTEGER NOT NULL CHECK(seq > 0),
    owner_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    custodian_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    operator_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    location TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('genesis','transfer','reversal')),
    source_id TEXT NOT NULL,
    prev_fact_id INTEGER REFERENCES rights_facts(fact_id),
    effective_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(asset_id, seq),
    UNIQUE(asset_id, source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS idx_rights_facts_asset_time
ON rights_facts(asset_id, effective_at, seq);

CREATE TABLE IF NOT EXISTS restrictions (
    restriction_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES title_assets(asset_id),
    kind TEXT NOT NULL CHECK(kind IN ('pledge','recall')),
    holder_party_id TEXT REFERENCES title_parties(party_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','released','lifted')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    imposed_by TEXT NOT NULL REFERENCES title_users(user_id),
    imposed_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES title_users(user_id),
    resolved_at TEXT,
    released_by_transfer_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_restrictions_asset_status
ON restrictions(asset_id, status);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    seller_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    buyer_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    state TEXT NOT NULL DEFAULT 'collecting' CHECK(state IN ('collecting','closed','cancelled')),
    basis_revision INTEGER NOT NULL DEFAULT 1 CHECK(basis_revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES title_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS transfer_items (
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    asset_id TEXT NOT NULL REFERENCES title_assets(asset_id),
    base_fact_id INTEGER NOT NULL REFERENCES rights_facts(fact_id),
    base_rights_revision INTEGER NOT NULL,
    base_restriction_revision INTEGER NOT NULL,
    frozen_owner_party_id TEXT NOT NULL,
    frozen_custodian_party_id TEXT NOT NULL,
    frozen_operator_party_id TEXT NOT NULL,
    frozen_location TEXT NOT NULL,
    frozen_restrictions_json TEXT NOT NULL,
    target_owner_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    target_custodian_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    target_operator_party_id TEXT NOT NULL REFERENCES title_parties(party_id),
    target_location TEXT NOT NULL,
    PRIMARY KEY (transfer_id, asset_id)
);

CREATE TABLE IF NOT EXISTS asset_transfer_locks (
    asset_id TEXT PRIMARY KEY REFERENCES title_assets(asset_id),
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id)
);

CREATE TABLE IF NOT EXISTS consents (
    consent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    scope TEXT NOT NULL,
    consent_type TEXT NOT NULL CHECK(consent_type IN ('buyer_accept','financier_release','quality_confirm')),
    basis_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'signed' CHECK(status IN ('signed','superseded')),
    actor_id TEXT NOT NULL REFERENCES title_users(user_id),
    note TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL,
    UNIQUE(transfer_id, scope, basis_revision)
);

CREATE INDEX IF NOT EXISTS idx_consents_transfer
ON consents(transfer_id, status, basis_revision);

CREATE TABLE IF NOT EXISTS reversals (
    reversal_id TEXT PRIMARY KEY,
    transfer_id TEXT NOT NULL UNIQUE REFERENCES transfers(transfer_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES title_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS title_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_title_audit_entity
ON title_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
