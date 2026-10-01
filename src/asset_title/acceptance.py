"""贯通参与方登记、限制、多方签署、原子交割、时点查询与退回的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import TitleService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = TitleService(connection, clock)
    for user_id, role in (
        ("reg", "registrar"),
        ("fin", "finance"),
        ("fin2", "finance"),
        ("buy", "buyer"),
        ("ops", "operations"),
        ("aud", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    for party_id, name in (
        ("seller-co", "华东储能资产公司"),
        ("buyer-co", "北方新能源并购基金"),
        ("bank-co", "华信融资租赁"),
        ("field-ops", "恒运运维服务"),
    ):
        service.register_party("reg", party_id, name)
    service.register_asset(
        "reg",
        {
            "asset_id": "bat-001",
            "description": "LFP 储能电池簇 40 尺舱",
            "owner_party_id": "seller-co",
            "custodian_party_id": "field-ops",
            "operator_party_id": "field-ops",
            "location": "华东储能电站A区",
        },
    )
    service.impose_restriction(
        "fin",
        {
            "restriction_id": "pledge-001",
            "asset_id": "bat-001",
            "kind": "pledge",
            "holder_party_id": "bank-co",
            "reason": "并购贷款质押担保",
        },
    )
    service.impose_restriction(
        "ops",
        {
            "restriction_id": "recall-001",
            "asset_id": "bat-001",
            "kind": "recall",
            "reason": "BMS 固件批次召回待确认",
        },
    )
    blocked = service.asset_blockers("aud", "bat-001")
    initiated = service.initiate_transfer(
        "fin",
        {
            "transfer_id": "t-001",
            "seller_party_id": "seller-co",
            "buyer_party_id": "buyer-co",
            "items": [{"asset_id": "bat-001", "target_location": "华东储能电站B区"}],
            "idempotency_key": "t-001-init",
        },
    )
    replayed = service.initiate_transfer(
        "fin",
        {
            "transfer_id": "t-001",
            "seller_party_id": "seller-co",
            "buyer_party_id": "buyer-co",
            "items": [{"asset_id": "bat-001", "target_location": "华东储能电站B区"}],
            "idempotency_key": "t-001-init",
        },
    )
    service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept", "note": "尽调通过"})
    service.sign_consent("fin2", "t-001", {"consent_type": "financier_release", "restriction_id": "pledge-001"})
    service.sign_consent("ops", "t-001", {"consent_type": "quality_confirm", "restriction_id": "recall-001"})
    clock.advance(hours=6)
    closed = service.close_transfer("fin", "t-001")
    after_close = service.asset_rights("fin", "bat-001")
    before_close = service.asset_rights("aud", "bat-001", as_of="2026-09-24T09:00:00Z")
    clock.advance(hours=2)
    reversed_result = service.reverse_transfer("fin", "t-001", "rev-001", "交割后融资先决条件未满足，双方协议退回")
    chain = service.asset_chain("aud", "bat-001")
    result = {
        "status": "ok",
        "blocked_before": blocked,
        "initiated": initiated,
        "initiate_replay_identical": replayed == initiated,
        "closed": closed,
        "owner_after_close": after_close["owner_party_id"],
        "owner_before_close": before_close["owner_party_id"],
        "reversal": reversed_result,
        "chain_facts": [
            {"seq": fact["seq"], "source_kind": fact["source_kind"], "owner_party_id": fact["owner_party_id"]}
            for fact in chain["facts"]
        ],
        "audit": service.audit_chain("aud"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行资产所有权流转服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
