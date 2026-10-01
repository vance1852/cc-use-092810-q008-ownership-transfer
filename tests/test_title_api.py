from __future__ import annotations

import json
import sqlite3
import unittest

from battery_title.api import JsonApplication
from battery_title.service import TitleService


class TitleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TitleService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "seller"):
        return self.app.handle(
            "POST", path,
            {"x-actor-id": actor, "content-type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )

    def _get(self, path: str, actor: str = "auditor"):
        return self.app.handle("GET", path, {"x-actor-id": actor})

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("POST", "/parties", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_settlement_blocked_payload_exposes_reasons(self) -> None:
        self._post("/users", {"user_id": "seller", "display_name": "卖方", "role": "seller"})
        self._post("/users", {"user_id": "buyer", "display_name": "买方", "role": "buyer"})
        self._post("/users", {"user_id": "finance", "display_name": "财务", "role": "finance"})
        for pid, kind in (("seller-co", "company"), ("buyer-co", "company"),
                          ("site-a", "site"), ("site-b", "site"), ("om-co", "company")):
            self._post("/parties", {"party_id": pid, "name": pid, "kind": kind})
        self._post("/assets", {
            "asset_id": "batt-1", "name": "电池",
            "owner_party": "seller-co", "custodian_party": "site-a",
            "operator_party": "om-co", "custodial_location": "A 仓"})
        title = self._get("/assets/batt-1/title", "finance").body
        deal = {
            "transfer_id": "deal-1", "seller_party": "seller-co", "buyer_party": "buyer-co",
            "idempotency_key": "k1", "basis_version": "v1", "note": "n",
            "assets": [{"asset_id": "batt-1", "revision": title["basis_revision"]}],
            "claims": [
                {"kind": "owner", "current_party": "seller-co", "next_party": "buyer-co"},
                {"kind": "custodian", "current_party": "site-a", "next_party": "site-b"},
                {"kind": "operator", "current_party": "om-co", "next_party": "buyer-co"}],
            "custodial_location": "B 仓", "restrictions": [], "conditions": []}
        self.assertEqual(self._post("/transfers", deal).status, 201)
        blocked = self._post("/transfers/deal-1/settle", {})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "settlement_blocked")
        self.assertIn("awaiting_consent:@acceptance", blocked.body["error"]["details"])
        self.assertEqual(self._post(
            "/transfers/deal-1/consents",
            {"condition_id": "@acceptance", "outcome": "cleared", "idempotency_key": "a"},
            actor="buyer").status, 201)
        settled = self._post("/transfers/deal-1/settle", {})
        self.assertEqual(settled.status, 200)
        self.assertEqual(settled.body["state"], "settled")

    def test_route_not_found(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "seller"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
