"""权属流转离线验收：在临时 SQLite 中跑通电站并购交割的完整权属生命周期。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import SettlementBlocked
from .service import TitleService
from .storage import connect


def _deal_body(transfer_id: str, asset_id: str, revision: int, conditions: list | None = None) -> dict:
    return {
        "transfer_id": transfer_id,
        "seller_party": "seller-co",
        "buyer_party": "buyer-co",
        "idempotency_key": f"key-{transfer_id}",
        "basis_version": "ledger-2026-09",
        "note": "电站并购交割包",
        "assets": [{"asset_id": asset_id, "revision": revision}],
        "claims": [
            {"kind": "owner", "current_party": "seller-co", "next_party": "buyer-co"},
            {"kind": "custodian", "current_party": "site-a", "next_party": "site-b"},
            {"kind": "operator", "current_party": "om-co", "next_party": "buyer-co"},
        ],
        "custodial_location": "并购标的场站 B 区交割位",
        "restrictions": [],
        "conditions": conditions or [],
    }


def run(workspace: Path) -> dict[str, object]:
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="battery-title-") as temporary:
        connection = connect(Path(temporary) / "title.sqlite3")
        try:
            service = TitleService(connection, clock)
            # 用户：卖方、两家买方、融资方、质量方、财务/运维/审计只读角色
            for user_id, role in (
                ("seller-1", "seller"), ("buyer-1", "buyer"), ("buyer-2", "buyer"),
                ("bank-1", "financier"), ("quality-1", "quality"),
                ("finance-1", "finance"), ("ops-1", "ops"), ("auditor-1", "auditor"),
            ):
                service.create_user(user_id, user_id, role)
            for party_id, kind in (
                ("seller-co", "company"), ("buyer-co", "company"), ("rival-co", "company"),
                ("bank-x", "financier"), ("site-a", "site"), ("site-b", "site"),
                ("om-co", "company"),
            ):
                service.register_party("seller-1", party_id, party_id, kind)

            # 资产入账：法律所有、现场保管、运维责任分属三家
            service.register_asset("seller-1", {
                "asset_id": "batt-001", "name": "并购标的电池簇 001",
                "owner_party": "seller-co", "custodian_party": "site-a",
                "operator_party": "om-co", "custodial_location": "A 场站 3 号仓",
            })
            enrolled_revision = service.title_at("finance-1", "batt-001")["basis_revision"]

            # 融资方登记质押
            service.put_restriction("bank-1", "batt-001", "pledge-1", "pledge", "bank-x", "并购贷款质押")
            pledged = service.title_at("finance-1", "batt-001")
            pledge_revision = pledged["basis_revision"]

            # 卖方发起转让，冻结清单/声明/位置/质押与解押条件
            body = _deal_body("deal-main", "batt-001", pledge_revision, [
                {"condition_id": "cond-release", "party": "financier", "kind": "release",
                 "restriction_id": "pledge-1"},
            ])
            frozen = service.propose_transfer("seller-1", body)
            frozen_sha = frozen["frozen_sha256"]

            # 条件未齐：交割被阻断，权属不变
            blocked_once: list[str] = []
            try:
                service.settle_transfer("seller-1", "deal-main")
            except SettlementBlocked as exc:
                blocked_once = list(exc.details or [])

            # 买方接受、融资方解押确认
            service.give_consent("buyer-1", "deal-main", {
                "condition_id": "@acceptance", "outcome": "cleared",
                "idempotency_key": "accept-1", "note": "现场验收通过"})
            service.give_consent("bank-1", "deal-main", {
                "condition_id": "cond-release", "outcome": "cleared",
                "idempotency_key": "release-1", "note": "贷款已清偿，同意解押"})

            # 竞争交易：同一资产的第二家买方面对确定赢家，不能抢先交割
            rival = _deal_body("deal-rival", "batt-001", pledge_revision)
            rival["buyer_party"] = "rival-co"
            rival["claims"][0]["next_party"] = "rival-co"
            rival["claims"][1]["next_party"] = "site-a"
            rival["claims"][2]["next_party"] = "rival-co"
            service.propose_transfer("seller-1", rival)
            service.give_consent("buyer-2", "deal-rival", {
                "condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": "accept-rival"})
            rival_blocked: list[str] = []
            try:
                service.settle_transfer("seller-1", "deal-rival")
            except SettlementBlocked as exc:
                rival_blocked = list(exc.details or [])

            # 赢家原子交割；竞争提案同事务落败
            settled = service.settle_transfer("seller-1", "deal-main")
            settled_replay = service.settle_transfer("seller-1", "deal-main")
            after = service.title_at("finance-1", "batt-001")
            rival_detail = service.transfer_detail("auditor-1", "deal-rival")

            # 交割后退回：生成反向事实，三权与质押恢复
            clock.advance(hours=6)
            reversed_result = service.reverse_transfer("seller-1", "deal-main", "交割后质保索赔，整包退回")
            after_reverse = service.title_at("ops-1", "batt-001")

            # 第二包资产：依据版本变化使未完成签署失效，失败保留同意且权属不变
            service.register_asset("seller-1", {
                "asset_id": "batt-002", "name": "并购标的电池簇 002",
                "owner_party": "seller-co", "custodian_party": "site-a",
                "operator_party": "om-co", "custodial_location": "A 场站 4 号仓"})
            rev2 = service.title_at("finance-1", "batt-002")["basis_revision"]
            service.propose_transfer("seller-1", _deal_body("deal-stale", "batt-002", rev2))
            service.give_consent("buyer-1", "deal-stale", {
                "condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": "accept-stale"})
            service.revise_asset("quality-1", "batt-002", None, "召回核查证据包升版")
            stale_failure: list[str] = []
            try:
                service.settle_transfer("seller-1", "deal-stale")
            except SettlementBlocked as exc:
                stale_failure = list(exc.details or [])
            stale_detail = service.transfer_detail("auditor-1", "deal-stale")
            stale_title = service.title_at("finance-1", "batt-002")

            chain = service.rights_chain("auditor-1", "batt-001")
            audit = service.audit_chain("auditor-1")
            connection.commit()
        finally:
            connection.close()

    return {
        "status": "ok",
        "enrolled_revision": enrolled_revision,
        "pledge_revision": pledge_revision,
        "frozen_sha256_length": len(frozen_sha),
        "blocked_before_consents": blocked_once,
        "rival_blocked_reasons": rival_blocked,
        "settled_event_ids": settled["event_ids"],
        "settled_replay_same_events": settled_replay["event_ids"] == settled["event_ids"],
        "owner_after_settlement": after["owner"],
        "custodian_after_settlement": after["custodian"],
        "operator_after_settlement": after["operator"],
        "location_after_settlement": after["custodial_location"],
        "restrictions_released_at_settlement": after["restrictions"],
        "rival_state": rival_detail["state"],
        "rival_block_reason": rival_detail["block_reason"],
        "reversed_event_ids": reversed_result["event_ids"],
        "owner_after_reverse": after_reverse["owner"],
        "custodian_after_reverse": after_reverse["custodian"],
        "operator_after_reverse": after_reverse["operator"],
        "pledge_restored_after_reverse": [r["restriction_id"] for r in after_reverse["restrictions"]],
        "stale_failure_reasons": stale_failure,
        "stale_consents_preserved": len(stale_detail["consents"]),
        "stale_owner_unchanged": stale_title["owner"],
        "rights_chain": [event["event_type"] for event in chain["chain"]],
        "audit": audit,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电池权属流转离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
