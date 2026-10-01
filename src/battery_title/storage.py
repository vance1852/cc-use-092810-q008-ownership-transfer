"""权属流转服务的 SQLite 模式和事务辅助。

设计要点：
- title_events 是只追加的权属事实流（事件溯源）。所有权、保管权、运维责任、限制的
  当前状态都是这些事件的折叠结果；交割失败不写事件，撤销/退回写新的反向事实，
  从不删除或改写历史。
- transfers 是一笔转让会话（冻结的提案与状态机），transfer_consents 是各前置条件
  的同意/确认记录，title_idempotency 保证发起与确认重试不产生第二次转让。
- 竞争交易不设单独序号表：所有 proposed 提案按 (created_at, transfer_id) 构成
  全局确定总序，最早者为领跑赢家，赢家交割后其余提案在同一事务内落败。
- title_audit_events 是与供应服务一致的哈希链，供审计独立验链。
"""

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
    role TEXT NOT NULL CHECK(role IN ('seller','buyer','financier','quality','finance','ops','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 主体目录：公司/场站/融资方等权属主体
CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('company','site','financier','vendor','other')),
    created_at TEXT NOT NULL
);

-- 资产目录：发起转让前必须登记，current_revision 是权属依据版本，单调递增
CREATE TABLE IF NOT EXISTS title_assets (
    asset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 1,
    evidence_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 只追加的权属事实流
CREATE TABLE IF NOT EXISTS title_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES title_assets(asset_id),
    -- enrolled              资产入账，建立初始三权与位置
    -- basis_revised         外部台账/证据升版（不改变三权，但作废旧签署）
    -- restriction_put_on    施加限制（质押/召回/留置/锁定）
    -- restriction_released  流程外解除限制
    -- title_changed         交割生效：三权与位置按冻结提案变更，关联限制解除
    -- title_reversed        交割后退回：与 title_changed 对称的反向事实
    event_type TEXT NOT NULL CHECK(event_type IN (
        'enrolled','basis_revised','restriction_put_on','restriction_released',
        'title_changed','title_reversed')),
    payload_json TEXT NOT NULL,
    -- 事实发生时资产的依据版本；任何依据变化使未完成签署失效
    basis_revision INTEGER NOT NULL,
    transfer_id TEXT,
    reversed_event_id INTEGER REFERENCES title_events(event_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_title_events_asset
ON title_events(asset_id, event_id);

CREATE INDEX IF NOT EXISTS idx_title_events_transfer
ON title_events(transfer_id);

-- 转让会话
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    seller_party TEXT NOT NULL,
    buyer_party TEXT NOT NULL,
    proposal_json TEXT NOT NULL,           -- 发起时冻结的完整提案（清单/声明/位置/限制/条件）
    proposal_sha256 TEXT NOT NULL UNIQUE,
    basis_version TEXT NOT NULL,           -- 外部台账/规则版本标签
    assets_revision_json TEXT NOT NULL,    -- 冻结时各资产 {asset_id: revision}
    state TEXT NOT NULL CHECK(state IN ('proposed','settled','failed','cancelled','reversed')),
    block_reason TEXT,                     -- 交割失败/被阻断/撤销原因
    settled_event_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfers_state ON transfers(state);

-- 前置条件的同意/确认
CREATE TABLE IF NOT EXISTS transfer_consents (
    consent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    condition_id TEXT NOT NULL,
    party_role TEXT NOT NULL,              -- buyer / financier / quality
    kind TEXT NOT NULL,                    -- acceptance / release / quality_clearance
    outcome TEXT NOT NULL CHECK(outcome IN ('cleared','rejected')),
    basis_key TEXT NOT NULL,               -- 给出同意时各资产依据版本的规范化快照
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    given_by TEXT NOT NULL,
    given_at TEXT NOT NULL,
    UNIQUE(transfer_id, condition_id),
    UNIQUE(transfer_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_consents_transfer
ON transfer_consents(transfer_id, condition_id);

-- 发起/交割的幂等记录（同意书的幂等由其 UNIQUE 约束承担）
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
