from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from asset_title.acceptance import run as acceptance_run
from asset_title.api import JsonApplication
from asset_title.clock import FrozenClock
from asset_title.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from asset_title.service import TitleService


ROOT = Path(__file__).resolve().parents[1]


class TitleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TitleService(self.connection, self.clock)
        for user_id, role in (
            ("reg", "registrar"),
            ("fin", "finance"),
            ("fin2", "finance"),
            ("buy", "buyer"),
            ("ops", "operations"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        for party_id in ("seller-co", "buyer-co", "bank-co", "field-ops", "third-co"):
            self.service.register_party("reg", party_id, party_id)
        self.service.register_asset(
            "reg",
            {
                "asset_id": "bat-001",
                "description": "LFP 储能电池簇",
                "owner_party_id": "seller-co",
                "custodian_party_id": "field-ops",
                "operator_party_id": "field-ops",
                "location": "华东储能电站A区",
            },
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _initiate(self, transfer_id: str = "t-001", key: str = "t-001-init", **overrides) -> dict:
        payload = {
            "transfer_id": transfer_id,
            "seller_party_id": "seller-co",
            "buyer_party_id": "buyer-co",
            "items": [{"asset_id": "bat-001"}],
            "idempotency_key": key,
        }
        payload.update(overrides)
        return self.service.initiate_transfer("fin", payload)

    def _pledge(self, restriction_id: str = "pledge-001") -> dict:
        return self.service.impose_restriction(
            "fin",
            {
                "restriction_id": restriction_id,
                "asset_id": "bat-001",
                "kind": "pledge",
                "holder_party_id": "bank-co",
                "reason": "并购贷款质押担保",
            },
        )

    def _recall(self, restriction_id: str = "recall-001") -> dict:
        return self.service.impose_restriction(
            "ops",
            {
                "restriction_id": restriction_id,
                "asset_id": "bat-001",
                "kind": "recall",
                "reason": "BMS 固件批次召回",
            },
        )

    def _sign_all_and_close(self, transfer_id: str = "t-001") -> dict:
        actors = {"buyer_accept": "buy", "financier_release": "fin2", "quality_confirm": "ops"}
        status = self.service.transfer_status("fin", transfer_id)
        for required in status["required_consents"]:
            consent = {"consent_type": required["consent_type"]}
            if required["restriction_id"] is not None:
                consent["restriction_id"] = required["restriction_id"]
            self.service.sign_consent(actors[required["consent_type"]], transfer_id, consent)
        return self.service.close_transfer("fin", transfer_id)

    def test_full_closing_transfers_rights_and_releases_restrictions(self) -> None:
        self._pledge()
        self._recall()
        initiated = self._initiate()
        self.assertEqual(initiated["state"], "collecting")
        scopes = {item["scope"] for item in initiated["required_consents"]}
        self.assertEqual(scopes, {"buyer", "restriction:pledge-001", "restriction:recall-001"})
        closed = self._sign_all_and_close()
        self.assertEqual(closed["state"], "closed")
        self.assertFalse(closed["replayed"])
        self.assertEqual(sorted(closed["released_restrictions"]), ["pledge-001", "recall-001"])
        asset = self.connection.execute("SELECT * FROM title_assets WHERE asset_id='bat-001'").fetchone()
        self.assertEqual(asset["owner_party_id"], "buyer-co")
        restrictions = self.connection.execute(
            "SELECT status,released_by_transfer_id FROM restrictions ORDER BY restriction_id"
        ).fetchall()
        for row in restrictions:
            self.assertEqual(row["status"], "released")
            self.assertEqual(row["released_by_transfer_id"], "t-001")
        facts = self.connection.execute(
            "SELECT * FROM rights_facts WHERE asset_id='bat-001' ORDER BY seq"
        ).fetchall()
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[1]["source_kind"], "transfer")
        self.assertEqual(facts[1]["prev_fact_id"], facts[0]["fact_id"])
        locks = self.connection.execute("SELECT count(*) FROM asset_transfer_locks").fetchone()[0]
        self.assertEqual(locks, 0)

    def test_initiate_replay_and_conflicting_reuse(self) -> None:
        first = self._initiate()
        second = self._initiate()
        self.assertEqual(first, second)
        count = self.connection.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(Conflict):
            self._initiate(items=[{"asset_id": "bat-001", "target_location": "异地仓库"}])
        with self.assertRaises(Conflict):
            self._initiate(key="another-key")
        count = self.connection.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(count, 1)

    def test_competing_transfers_have_deterministic_winner(self) -> None:
        self._initiate("t-001", "k1")
        with self.assertRaises(Conflict) as ctx:
            self._initiate("t-002", "k2")
        self.assertIn("t-001", str(ctx.exception))
        self.service.cancel_transfer("fin", "t-001", "买方退出")
        winner = self._initiate("t-002", "k2")
        self.assertEqual(winner["state"], "collecting")

    def test_basis_shift_invalidates_pending_signatures(self) -> None:
        self._pledge()
        self._initiate()
        self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        self.service.sign_consent(
            "fin2", "t-001", {"consent_type": "financier_release", "restriction_id": "pledge-001"}
        )
        self._recall()
        with self.assertRaises(InvalidState):
            self.service.close_transfer("fin", "t-001")
        status = self.service.transfer_status("aud", "t-001")
        self.assertEqual(status["basis_revision"], 2)
        superseded = [c for c in status["consents"] if c["status"] == "superseded"]
        self.assertEqual(len(superseded), 2)
        required_scopes = {c["scope"] for c in status["required_consents"]}
        self.assertIn("restriction:recall-001", required_scopes)
        closed = self._sign_all_and_close()
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["basis_revision"], 2)
        self.assertEqual(sorted(closed["released_restrictions"]), ["pledge-001", "recall-001"])

    def test_direct_resolve_also_shifts_basis(self) -> None:
        self._pledge()
        self._initiate()
        self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        self.service.resolve_restriction("fin", "pledge-001", 1, "贷款已清偿")
        with self.assertRaises(InvalidState):
            self.service.close_transfer("fin", "t-001")
        status = self.service.transfer_status("aud", "t-001")
        self.assertEqual(status["basis_revision"], 2)
        self.assertEqual([c["scope"] for c in status["required_consents"]], ["buyer"])
        closed = self._sign_all_and_close()
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["released_restrictions"], [])

    def test_failed_close_keeps_consents_and_ownership(self) -> None:
        self._pledge()
        self._initiate()
        self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        with self.assertRaises(InvalidState) as ctx:
            self.service.close_transfer("fin", "t-001")
        self.assertIn("restriction:pledge-001", str(ctx.exception))
        status = self.service.transfer_status("aud", "t-001")
        self.assertEqual(status["state"], "collecting")
        signed = [c for c in status["consents"] if c["status"] == "signed"]
        self.assertEqual(len(signed), 1)
        self.assertEqual(signed[0]["scope"], "buyer")
        rights = self.service.asset_rights("fin", "bat-001")
        self.assertEqual(rights["owner_party_id"], "seller-co")
        pledge = self.connection.execute(
            "SELECT status FROM restrictions WHERE restriction_id='pledge-001'"
        ).fetchone()
        self.assertEqual(pledge["status"], "active")

    def test_close_replay_does_not_duplicate_facts(self) -> None:
        self._initiate()
        first = self._sign_all_and_close()
        second = self.service.close_transfer("fin", "t-001")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["facts"], second["facts"])
        count = self.connection.execute(
            "SELECT count(*) FROM rights_facts WHERE asset_id='bat-001'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_reversal_appends_reverse_fact_and_keeps_history(self) -> None:
        self._pledge()
        self._initiate()
        self._sign_all_and_close()
        self.clock.advance(hours=1)
        reversed_result = self.service.reverse_transfer("fin", "t-001", "rev-001", "双方协议退回")
        self.assertEqual(reversed_result["state"], "reversed")
        self.assertFalse(reversed_result["replayed"])
        rights = self.service.asset_rights("fin", "bat-001")
        self.assertEqual(rights["owner_party_id"], "seller-co")
        chain = self.service.asset_chain("aud", "bat-001")
        self.assertEqual([f["source_kind"] for f in chain["facts"]], ["genesis", "transfer", "reversal"])
        self.assertEqual(chain["facts"][1]["owner_party_id"], "buyer-co")
        self.assertTrue(chain["facts"][2]["is_current"])
        replay = self.service.reverse_transfer("fin", "t-001", "rev-001", "双方协议退回")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.service.reverse_transfer("fin", "t-001", "rev-002", "重复退回")
        count = self.connection.execute(
            "SELECT count(*) FROM rights_facts WHERE asset_id='bat-001'"
        ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_reversal_rejected_after_rights_moved_on(self) -> None:
        self._initiate()
        self._sign_all_and_close("t-001")
        self.service.initiate_transfer(
            "fin",
            {
                "transfer_id": "t-002",
                "seller_party_id": "buyer-co",
                "buyer_party_id": "third-co",
                "items": [{"asset_id": "bat-001"}],
                "idempotency_key": "t-002-init",
            },
        )
        self._sign_all_and_close("t-002")
        with self.assertRaises(Conflict):
            self.service.reverse_transfer("fin", "t-001", "rev-001", "迟到的退回")

    def test_cancel_releases_lock_and_keeps_consents(self) -> None:
        self._initiate()
        self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        cancelled = self.service.cancel_transfer("fin", "t-001", "交易取消")
        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaises(InvalidState):
            self.service.cancel_transfer("fin", "t-001", "再次撤销")
        status = self.service.transfer_status("aud", "t-001")
        self.assertEqual(len(status["consents"]), 1)
        self.assertEqual(status["cancel_reason"], "交易取消")
        blockers = self.service.asset_blockers("ops", "bat-001")
        self.assertTrue(blockers["transferable"])
        self._initiate("t-002", "k2")

    def test_as_of_point_in_time_queries(self) -> None:
        self._initiate()
        self.clock.advance(hours=2)
        self._sign_all_and_close()
        before = self.service.asset_rights("ops", "bat-001", as_of="2026-09-24T09:00:00Z")
        self.assertEqual(before["owner_party_id"], "seller-co")
        after = self.service.asset_rights("fin", "bat-001", as_of="2026-09-24T10:00:01Z")
        self.assertEqual(after["owner_party_id"], "buyer-co")
        with self.assertRaises(NotFound):
            self.service.asset_rights("aud", "bat-001", as_of="2026-09-24T07:00:00Z")

    def test_blockers_report_reasons(self) -> None:
        self._pledge()
        self._initiate()
        blockers = self.service.asset_blockers("ops", "bat-001")
        self.assertFalse(blockers["transferable"])
        self.assertEqual(blockers["locked_by_transfer_id"], "t-001")
        self.assertEqual(len(blockers["active_restrictions"]), 1)
        self.assertEqual(blockers["active_restrictions"][0]["kind"], "pledge")
        scopes = {c["scope"] for c in blockers["required_consents"]}
        self.assertEqual(scopes, {"buyer", "restriction:pledge-001"})

    def test_consent_replay_and_unknown_scope(self) -> None:
        self._initiate()
        first = self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        second = self.service.sign_consent("buy", "t-001", {"consent_type": "buyer_accept"})
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        count = self.connection.execute("SELECT count(*) FROM consents").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(ValidationFailed):
            self.service.sign_consent(
                "fin2", "t-001", {"consent_type": "financier_release", "restriction_id": "pledge-999"}
            )

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.initiate_transfer(
                "buy",
                {
                    "transfer_id": "t-x",
                    "seller_party_id": "seller-co",
                    "buyer_party_id": "buyer-co",
                    "items": [{"asset_id": "bat-001"}],
                    "idempotency_key": "k-x",
                },
            )
        with self.assertRaises(Forbidden):
            self.service.impose_restriction(
                "ops",
                {
                    "restriction_id": "pledge-x",
                    "asset_id": "bat-001",
                    "kind": "pledge",
                    "holder_party_id": "bank-co",
                    "reason": "越权质押",
                },
            )
        with self.assertRaises(Forbidden):
            self.service.impose_restriction(
                "fin",
                {"restriction_id": "recall-x", "asset_id": "bat-001", "kind": "recall", "reason": "越权召回"},
            )
        self._pledge()
        self._initiate()
        with self.assertRaises(Forbidden):
            self.service.sign_consent("fin", "t-001", {"consent_type": "buyer_accept"})
        with self.assertRaises(Forbidden):
            self.service.sign_consent(
                "fin", "t-001", {"consent_type": "financier_release", "restriction_id": "pledge-001"}
            )
        with self.assertRaises(Forbidden):
            self.service.asset_rights("reg", "bat-001")
        for user_id in ("fin", "ops", "aud"):
            rights = self.service.asset_rights(user_id, "bat-001")
            self.assertEqual(rights["owner_party_id"], "seller-co")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("fin")

    def test_seller_must_own_asset(self) -> None:
        with self.assertRaises(Conflict):
            self._initiate(seller_party_id="third-co")
        with self.assertRaises(NotFound):
            self._initiate(buyer_party_id="ghost-co")
        with self.assertRaises(ValidationFailed):
            self._initiate(items=[])
        with self.assertRaises(ValidationFailed):
            self._initiate(items=[{"asset_id": "bat-001"}, {"asset_id": "bat-001"}])

    def test_audit_chain_valid(self) -> None:
        self._pledge()
        self._initiate()
        self._sign_all_and_close()
        result = self.service.audit_chain("aud")
        self.assertTrue(result["valid"])
        self.assertGreater(result["events"], 0)


class TitleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TitleService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "reg"):
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode())

    def _get(self, path: str, actor: str = "aud"):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "x"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_transfer_flow_over_http(self) -> None:
        for user_id, role in (
            ("reg", "registrar"),
            ("fin", "finance"),
            ("buy", "buyer"),
            ("aud", "auditor"),
        ):
            response = self._post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
            self.assertEqual(response.status, 201)
        for party_id in ("seller-co", "buyer-co", "field-ops"):
            response = self._post("/parties", {"party_id": party_id, "name": party_id})
            self.assertEqual(response.status, 201)
        response = self._post(
            "/assets",
            {
                "asset_id": "bat-001",
                "description": "电池簇",
                "owner_party_id": "seller-co",
                "custodian_party_id": "field-ops",
                "operator_party_id": "field-ops",
                "location": "A区",
            },
        )
        self.assertEqual(response.status, 201)
        response = self._post(
            "/transfers",
            {
                "transfer_id": "t-001",
                "seller_party_id": "seller-co",
                "buyer_party_id": "buyer-co",
                "items": [{"asset_id": "bat-001"}],
                "idempotency_key": "k1",
            },
            actor="fin",
        )
        self.assertEqual(response.status, 201)
        response = self._post("/transfers/t-001/consents", {"consent_type": "buyer_accept"}, actor="buy")
        self.assertEqual(response.status, 201)
        response = self._post("/transfers/t-001/close", {}, actor="fin")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "closed")
        response = self._get("/assets/bat-001/rights")
        self.assertEqual(response.body["owner_party_id"], "buyer-co")
        response = self._get("/assets/bat-001/chain")
        self.assertEqual(len(response.body["facts"]), 2)
        response = self._get("/audit/chain")
        self.assertTrue(response.body["valid"])


class TitleAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["initiate_replay_identical"])
        self.assertEqual(result["owner_after_close"], "buyer-co")
        self.assertEqual(result["owner_before_close"], "seller-co")
        self.assertEqual(
            [fact["source_kind"] for fact in result["chain_facts"]],
            ["genesis", "transfer", "reversal"],
        )
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
