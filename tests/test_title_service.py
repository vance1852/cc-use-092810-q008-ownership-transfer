from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from battery_title.clock import FrozenClock
from battery_title.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    SettlementBlocked,
    ValidationFailed,
)
from battery_title.service import TitleService


def deal_body(transfer_id: str, asset_id: str, revision: int, *, buyer="buyer-co",
              custodian="site-b", conditions=None, restrictions=None, location="B 仓"):
    return {
        "transfer_id": transfer_id,
        "seller_party": "seller-co",
        "buyer_party": buyer,
        "idempotency_key": f"key-{transfer_id}",
        "basis_version": "ledger-v1",
        "note": "并购交割",
        "assets": [{"asset_id": asset_id, "revision": revision}],
        "claims": [
            {"kind": "owner", "current_party": "seller-co", "next_party": buyer},
            {"kind": "custodian", "current_party": "site-a", "next_party": custodian},
            {"kind": "operator", "current_party": "om-co", "next_party": buyer},
        ],
        "custodial_location": location,
        "restrictions": restrictions or [],
        "conditions": conditions or [],
    }


class TitleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.service = TitleService(self.connection, self.clock)
        for user_id, role in (
            ("seller", "seller"), ("buyer", "buyer"), ("buyer2", "buyer"),
            ("bank", "financier"), ("qa", "quality"),
            ("finance", "finance"), ("ops", "ops"), ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        for party_id, kind in (
            ("seller-co", "company"), ("buyer-co", "company"), ("rival-co", "company"),
            ("bank-x", "financier"), ("site-a", "site"), ("site-b", "site"),
            ("om-co", "company"),
        ):
            self.service.register_party("seller", party_id, party_id, kind)

    def tearDown(self) -> None:
        self.connection.close()

    def _asset(self, asset_id="batt-1") -> int:
        self.service.register_asset("seller", {
            "asset_id": asset_id, "name": asset_id,
            "owner_party": "seller-co", "custodian_party": "site-a",
            "operator_party": "om-co", "custodial_location": "A 仓",
        })
        return self.service.title_at("finance", asset_id)["basis_revision"]

    def _accept(self, user="buyer", transfer_id="deal-1", key="acc-1"):
        return self.service.give_consent(user, transfer_id, {
            "condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": key})

    def test_full_flow_freezes_gathers_and_settles_atomically(self) -> None:
        revision = self._asset()
        self.service.put_restriction("bank", "batt-1", "pledge-1", "pledge", "bank-x", "并购贷质押")
        revision = self.service.title_at("finance", "batt-1")["basis_revision"]

        frozen = self.service.propose_transfer("seller", deal_body(
            "deal-1", "batt-1", revision,
            restrictions=[{"restriction_id": "pledge-1", "kind": "pledge",
                           "holder_party": "bank-x", "status": "active", "note": "并购贷质押"}],
            conditions=[{"condition_id": "rel-1", "party": "financier", "kind": "release",
                         "restriction_id": "pledge-1"}]))
        self.assertEqual(frozen["state"], "proposed")
        self.assertEqual(len(frozen["frozen_sha256"]), 64)
        # 冻结快照由台账自动汇集，包含质押限制与自动注入的买方接受条件
        self.assertEqual([r["restriction_id"] for r in frozen["restrictions"]], ["pledge-1"])
        self.assertIn("@acceptance", [c["condition_id"] for c in frozen["conditions"]])

        with self.assertRaises(SettlementBlocked) as blocked:
            self.service.settle_transfer("seller", "deal-1")
        reasons = blocked.exception.details
        self.assertIn("awaiting_consent:@acceptance", reasons)
        self.assertIn("unresolved_restriction:pledge-1:pledge", reasons)
        # 阻断不提前改变权属
        self.assertEqual(self.service.title_at("finance", "batt-1")["owner"], "seller-co")

        self._accept()
        self.service.give_consent("bank", "deal-1", {
            "condition_id": "rel-1", "outcome": "cleared", "idempotency_key": "rel",
            "note": "贷款清偿"})
        settled = self.service.settle_transfer("seller", "deal-1")
        title = self.service.title_at("finance", "batt-1")
        self.assertEqual((title["owner"], title["custodian"], title["operator"]),
                         ("buyer-co", "site-b", "buyer-co"))
        self.assertEqual(title["custodial_location"], "B 仓")
        self.assertEqual(title["restrictions"], [])  # 质押随解押确认解除
        self.assertEqual(settled["state"], "settled")

    def test_settle_retry_does_not_create_second_transfer(self) -> None:
        revision = self._asset()
        self.service.propose_transfer("seller", deal_body("deal-1", "batt-1", revision))
        self._accept()
        first = self.service.settle_transfer("seller", "deal-1")
        second = self.service.settle_transfer("seller", "deal-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["event_ids"], first["event_ids"])
        changes = self.connection.execute(
            "SELECT count(*) FROM title_events WHERE event_type='title_changed' AND transfer_id='deal-1'"
        ).fetchone()[0]
        self.assertEqual(changes, 1)

    def test_propose_and_consent_are_idempotent(self) -> None:
        revision = self._asset()
        body = deal_body("deal-1", "batt-1", revision)
        first = self.service.propose_transfer("seller", body)
        second = self.service.propose_transfer("seller", body)
        self.assertEqual(first, second)
        self._accept()
        replayed = self._accept()
        self.assertTrue(replayed["replayed"])
        count = self.connection.execute(
            "SELECT count(*) FROM transfer_consents WHERE transfer_id='deal-1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        # 同一幂等键不同内容应冲突
        with self.assertRaises(Conflict):
            self.service.give_consent("buyer", "deal-1", {
                "condition_id": "@acceptance", "outcome": "rejected", "idempotency_key": "acc-1"})

    def test_basis_revision_invalidates_open_signatures(self) -> None:
        revision = self._asset()
        self.service.propose_transfer("seller", deal_body("deal-1", "batt-1", revision))
        self._accept()
        self.service.revise_asset("qa", "batt-1", None, "证据包补正")
        # 不能再向失效提案追加签署
        with self.assertRaises(InvalidState):
            self._accept(key="acc-2")
        # 交割终局失败：同意保留，权属不变
        with self.assertRaises(SettlementBlocked) as blocked:
            self.service.settle_transfer("seller", "deal-1")
        self.assertTrue(any(r.startswith("stale_basis:") for r in blocked.exception.details))
        detail = self.service.transfer_detail("auditor", "deal-1")
        self.assertEqual(detail["state"], "failed")
        self.assertEqual(len(detail["consents"]), 1)
        self.assertEqual(self.service.title_at("finance", "batt-1")["owner"], "seller-co")
        # 失败后再次交割返回相同阻断原因，而非第二次转让
        with self.assertRaises(SettlementBlocked):
            self.service.settle_transfer("seller", "deal-1")

    def test_contest_has_deterministic_winner(self) -> None:
        revision = self._asset()
        self.service.propose_transfer("seller", deal_body("deal-a", "batt-1", revision, buyer="buyer-co"))
        self.clock.advance(seconds=1)
        self.service.propose_transfer("seller", deal_body(
            "deal-b", "batt-1", revision, buyer="rival-co", custodian="site-a", location="A 仓"))
        self._accept("buyer", "deal-a", "acc-a")
        self._accept("buyer2", "deal-b", "acc-b")
        # 领跑者是更早的 deal-a，deal-b 即便先尝试交割也被阻断
        with self.assertRaises(SettlementBlocked) as blocked:
            self.service.settle_transfer("seller", "deal-b")
        self.assertIn("contest_leader:deal-a", blocked.exception.details)
        self.service.settle_transfer("seller", "deal-a")
        loser = self.service.transfer_detail("auditor", "deal-b")
        self.assertEqual(loser["state"], "failed")
        self.assertEqual(loser["block_reason"], "lost_contest:deal-a")
        self.assertEqual(self.service.title_at("finance", "batt-1")["owner"], "buyer-co")

    def test_recall_requires_quality_clearance_and_rejection_fails(self) -> None:
        revision = self._asset()
        self.service.put_restriction("qa", "batt-1", "rc-1", "recall", None, "厂家召回")
        revision = self.service.title_at("finance", "batt-1")["basis_revision"]
        self.service.propose_transfer("seller", deal_body(
            "deal-1", "batt-1", revision,
            conditions=[{"condition_id": "q-1", "party": "quality", "kind": "quality_clearance",
                         "restriction_id": "rc-1"}]))
        self._accept()
        self.service.give_consent("qa", "deal-1", {
            "condition_id": "q-1", "outcome": "rejected", "idempotency_key": "q", "note": "召回未关闭"})
        with self.assertRaises(SettlementBlocked) as blocked:
            self.service.settle_transfer("seller", "deal-1")
        self.assertTrue(any(r.startswith("rejected_outcome:") for r in blocked.exception.details))
        self.assertEqual(self.service.transfer_detail("auditor", "deal-1")["state"], "failed")
        self.assertEqual(self.service.title_at("finance", "batt-1")["owner"], "seller-co")

    def test_cancel_before_settlement_preserves_history(self) -> None:
        revision = self._asset()
        self.service.propose_transfer("seller", deal_body("deal-1", "batt-1", revision))
        self._accept()
        result = self.service.cancel_transfer("seller", "deal-1", "买方融资不到位")
        self.assertEqual(result["state"], "cancelled")
        detail = self.service.transfer_detail("auditor", "deal-1")
        self.assertEqual(len(detail["consents"]), 1)  # 已完成同意保留
        self.assertTrue(detail["proposal"]["transfer_id"], "deal-1")  # 冻结提案保留
        chain_types = [e["event_type"] for e in self.service.rights_chain("auditor", "batt-1")["chain"]]
        self.assertEqual(chain_types, ["enrolled"])  # 权属从未改变
        with self.assertRaises(InvalidState):
            self.service.cancel_transfer("seller", "deal-1", "再次撤销")

    def test_reverse_after_settlement_writes_inverse_facts(self) -> None:
        revision = self._asset()
        self.service.put_restriction("bank", "batt-1", "pledge-1", "pledge", "bank-x", "质押")
        revision = self.service.title_at("finance", "batt-1")["basis_revision"]
        self.service.propose_transfer("seller", deal_body(
            "deal-1", "batt-1", revision,
            conditions=[{"condition_id": "rel-1", "party": "financier", "kind": "release",
                         "restriction_id": "pledge-1"}]))
        self._accept()
        self.service.give_consent("bank", "deal-1", {
            "condition_id": "rel-1", "outcome": "cleared", "idempotency_key": "rel"})
        self.service.settle_transfer("seller", "deal-1")
        self.clock.advance(hours=3)
        result = self.service.reverse_transfer("seller", "deal-1", "质保索赔退回")
        self.assertEqual(len(result["event_ids"]), 1)
        title = self.service.title_at("finance", "batt-1")
        self.assertEqual((title["owner"], title["custodian"], title["operator"]),
                         ("seller-co", "site-a", "om-co"))
        self.assertEqual([r["restriction_id"] for r in title["restrictions"]], ["pledge-1"])
        chain = self.service.rights_chain("auditor", "batt-1")["chain"]
        self.assertEqual([e["event_type"] for e in chain],
                         ["enrolled", "restriction_put_on", "title_changed", "title_reversed"])
        reverse_event = next(e for e in chain if e["event_type"] == "title_reversed")
        changed_event = next(e for e in chain if e["event_type"] == "title_changed")
        self.assertEqual(reverse_event["reversed_event_id"], changed_event["event_id"])
        # 未交割的提案不能退回，只能撤销
        revision2 = self._asset("batt-9")
        self.service.propose_transfer("seller", deal_body("deal-9", "batt-9", revision2))
        with self.assertRaises(InvalidState):
            self.service.reverse_transfer("seller", "deal-9", "x")
        # 不存在的转让
        with self.assertRaises(NotFound):
            self.service.reverse_transfer("seller", "missing", "x")

    def test_point_in_time_queries_per_role(self) -> None:
        revision = self._asset()
        self.service.propose_transfer("seller", deal_body("deal-1", "batt-1", revision))
        self._accept()
        self.clock.advance(hours=1)
        self.service.settle_transfer("seller", "deal-1")
        # 财务看所有者、运维看责任人，都能查时点快照
        before = self.service.title_at("finance", "batt-1", "2026-09-30T08:30:00Z")
        after = self.service.title_at("ops", "batt-1", "2026-09-30T09:30:00Z")
        self.assertEqual(before["owner"], "seller-co")
        self.assertEqual(after["owner"], "buyer-co")
        self.assertEqual(after["operator"], "buyer-co")

    def test_role_separation(self) -> None:
        revision = self._asset()
        # 只读角色不能发起/签署
        with self.assertRaises(Forbidden):
            self.service.propose_transfer("finance", deal_body("deal-x", "batt-1", revision))
        with self.assertRaises(Forbidden):
            self.service.give_consent("ops", "deal-x", {
                "condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": "k"})
        # 融资方不能替买方接受
        self.service.propose_transfer("seller", deal_body("deal-1", "batt-1", revision))
        with self.assertRaises(Forbidden):
            self.service.give_consent("bank", "deal-1", {
                "condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": "k"})
        # 财务/运维不能验审计链
        with self.assertRaises(Forbidden):
            self.service.audit_chain("finance")
        self.assertTrue(self.service.audit_chain("auditor")["valid"])

    def test_seller_must_own_asset_and_frozen_revision_must_match(self) -> None:
        self._asset()
        # 卖方非所有人
        body = deal_body("deal-1", "batt-1", 1, buyer="buyer-co")
        body["seller_party"] = "rival-co"
        with self.assertRaises(InvalidState):
            self.service.propose_transfer("seller", body)
        # 依据版本不匹配
        with self.assertRaises(Conflict):
            self.service.propose_transfer("seller", deal_body("deal-2", "batt-1", 99))
        # 不存在的资产
        with self.assertRaises(NotFound):
            self.service.propose_transfer("seller", deal_body("deal-3", "nope", 1))

    def test_claims_validation(self) -> None:
        self._asset()
        body = deal_body("deal-z", "batt-1", 1)
        body["claims"] = [
            {"kind": "custodian", "current_party": "site-a", "next_party": "site-b"}]
        with self.assertRaises(ValidationFailed):
            self.service.propose_transfer("seller", body)


if __name__ == "__main__":
    unittest.main()
