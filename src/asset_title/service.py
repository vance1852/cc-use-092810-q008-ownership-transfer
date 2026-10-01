"""所有权流转的事务用例：冻结依据、多方签署、原子交割与反向事实。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest
from .models import (
    AssetRegistration,
    ConsentInput,
    RestrictionInput,
    TransferInitiation,
    identifier,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "registrar": {"party.write", "asset.write"},
    "finance": {"transfer.write", "pledge.write", "consent.financier", "chain.read"},
    "buyer": {"consent.buyer", "chain.read"},
    "operations": {"recall.write", "consent.quality", "chain.read"},
    "auditor": {"chain.read", "audit.read"},
}

CONSENT_PERMISSIONS = {
    "buyer_accept": "consent.buyer",
    "financier_release": "consent.financier",
    "quality_confirm": "consent.quality",
}

RESTRICTION_WRITE_PERMISSIONS = {
    "pledge": "pledge.write",
    "recall": "recall.write",
}


class TitleService:
    """在单个 SQLite 连接上提供所有权流转的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM title_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM title_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO title_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _party(self, party_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM title_parties WHERE party_id=?", (party_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"参与方不存在: {party_id}")
        return row

    def _asset(self, asset_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"资产不存在: {asset_id}")
        return row

    def _transfer(self, transfer_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise NotFound("转让不存在")
        return row

    def _latest_fact(self, asset_id: str) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM rights_facts WHERE asset_id=? ORDER BY seq DESC LIMIT 1", (asset_id,)
        ).fetchone()

    def _active_restrictions(self, asset_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM restrictions WHERE asset_id=? AND status='active' ORDER BY restriction_id",
            (asset_id,),
        ).fetchall()

    def _restriction_snapshot(self, asset_id: str) -> list[dict[str, Any]]:
        return [
            {
                "restriction_id": row["restriction_id"],
                "kind": row["kind"],
                "holder_party_id": row["holder_party_id"],
                "reason": row["reason"],
                "revision": row["revision"],
            }
            for row in self._active_restrictions(asset_id)
        ]

    def _transfer_items(self, transfer_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM transfer_items WHERE transfer_id=? ORDER BY asset_id", (transfer_id,)
        ).fetchall()

    @staticmethod
    def _required_scopes(items: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
        required: dict[str, dict[str, Any]] = {
            "buyer": {"consent_type": "buyer_accept", "restriction_id": None}
        }
        for item in items:
            for restriction in json.loads(item["frozen_restrictions_json"]):
                scope = f"restriction:{restriction['restriction_id']}"
                consent_type = (
                    "financier_release" if restriction["kind"] == "pledge" else "quality_confirm"
                )
                required[scope] = {
                    "consent_type": consent_type,
                    "restriction_id": restriction["restriction_id"],
                }
        return required

    @staticmethod
    def _required_list(required: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"scope": scope, **required[scope]}
            for scope in sorted(required, key=lambda scope: (scope != "buyer", scope))
        ]

    def _repair_basis_locked(self, transfer: sqlite3.Row, actor_id: str) -> sqlite3.Row:
        """在持有写事务时检查依据版本；漂移则推进 basis_revision 并使未完成签署失效。"""
        if transfer["state"] != "collecting":
            return transfer
        items = self._transfer_items(transfer["transfer_id"])
        drifted: list[tuple[sqlite3.Row, sqlite3.Row]] = []
        for item in items:
            asset = self._asset(item["asset_id"])
            if (
                asset["rights_revision"] != item["base_rights_revision"]
                or asset["restriction_revision"] != item["base_restriction_revision"]
            ):
                drifted.append((item, asset))
        if not drifted:
            return transfer
        transfer_id = transfer["transfer_id"]
        new_basis = transfer["basis_revision"] + 1
        for item, asset in drifted:
            fact = self._latest_fact(item["asset_id"])
            self.connection.execute(
                "UPDATE transfer_items SET base_fact_id=?,base_rights_revision=?,base_restriction_revision=?,"
                "frozen_owner_party_id=?,frozen_custodian_party_id=?,frozen_operator_party_id=?,"
                "frozen_location=?,frozen_restrictions_json=? WHERE transfer_id=? AND asset_id=?",
                (
                    fact["fact_id"],
                    asset["rights_revision"],
                    asset["restriction_revision"],
                    asset["owner_party_id"],
                    asset["custodian_party_id"],
                    asset["operator_party_id"],
                    asset["location"],
                    canonical_json(self._restriction_snapshot(item["asset_id"])),
                    transfer_id,
                    item["asset_id"],
                ),
            )
        self.connection.execute(
            "UPDATE transfers SET basis_revision=? WHERE transfer_id=?", (new_basis, transfer_id)
        )
        superseded = self.connection.execute(
            "UPDATE consents SET status='superseded' WHERE transfer_id=? AND status='signed' AND basis_revision<?",
            (transfer_id, new_basis),
        ).rowcount
        self._audit(
            "transfer",
            transfer_id,
            "transfer.basis_shifted",
            actor_id,
            {
                "from_basis_revision": transfer["basis_revision"],
                "to_basis_revision": new_basis,
                "drifted_assets": [item["asset_id"] for item, _ in drifted],
                "superseded_consents": superseded,
            },
        )
        return self._transfer(transfer_id)

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO title_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_party(self, actor_id: str, party_id: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        if not party_id.strip() or not name.strip():
            raise ValidationFailed("参与方编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO title_parties(party_id,name,created_at) VALUES(?,?,?)",
                    (party_id.strip(), name.strip(), self._now()),
                )
                self._audit("party", party_id.strip(), "party.registered", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("参与方已经存在") from exc
        return {"party_id": party_id.strip(), "name": name.strip()}

    def register_asset(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "asset.write")
        asset = AssetRegistration.from_dict(raw)
        for party_id in (asset.owner_party_id, asset.custodian_party_id, asset.operator_party_id):
            self._party(party_id)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO title_assets(asset_id,description,owner_party_id,custodian_party_id,"
                    "operator_party_id,location,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        asset.asset_id,
                        asset.description,
                        asset.owner_party_id,
                        asset.custodian_party_id,
                        asset.operator_party_id,
                        asset.location,
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO rights_facts(asset_id,seq,owner_party_id,custodian_party_id,operator_party_id,"
                    "location,source_kind,source_id,prev_fact_id,effective_at,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        asset.asset_id,
                        1,
                        asset.owner_party_id,
                        asset.custodian_party_id,
                        asset.operator_party_id,
                        asset.location,
                        "genesis",
                        asset.asset_id,
                        None,
                        now,
                        now,
                    ),
                )
                self._audit(
                    "asset",
                    asset.asset_id,
                    "asset.registered",
                    actor_id,
                    {"owner_party_id": asset.owner_party_id, "location": asset.location},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("资产编号已经存在") from exc
        return {
            "asset_id": asset.asset_id,
            "owner_party_id": asset.owner_party_id,
            "custodian_party_id": asset.custodian_party_id,
            "operator_party_id": asset.operator_party_id,
            "location": asset.location,
            "rights_revision": 1,
        }

    def impose_restriction(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        restriction = RestrictionInput.from_dict(raw)
        self._require(actor_id, RESTRICTION_WRITE_PERMISSIONS[restriction.kind])
        self._asset(restriction.asset_id)
        if restriction.holder_party_id is not None:
            self._party(restriction.holder_party_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO restrictions(restriction_id,asset_id,kind,holder_party_id,reason,"
                    "imposed_by,imposed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        restriction.restriction_id,
                        restriction.asset_id,
                        restriction.kind,
                        restriction.holder_party_id,
                        restriction.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE title_assets SET restriction_revision=restriction_revision+1 WHERE asset_id=?",
                    (restriction.asset_id,),
                )
                self._audit(
                    "restriction",
                    restriction.restriction_id,
                    "restriction.imposed",
                    actor_id,
                    {"asset_id": restriction.asset_id, "kind": restriction.kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("限制编号已经存在") from exc
        return {
            "restriction_id": restriction.restriction_id,
            "asset_id": restriction.asset_id,
            "kind": restriction.kind,
            "status": "active",
            "revision": 1,
        }

    def resolve_restriction(
        self, actor_id: str, restriction_id: str, expected_revision: int, note: str = ""
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM restrictions WHERE restriction_id=?", (restriction_id,)
        ).fetchone()
        if row is None:
            raise NotFound("限制不存在")
        self._require(actor_id, RESTRICTION_WRITE_PERMISSIONS[row["kind"]])
        if row["status"] != "active":
            raise InvalidState("限制已经解除")
        new_status = "released" if row["kind"] == "pledge" else "lifted"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE restrictions SET status=?,resolved_by=?,resolved_at=?,revision=revision+1 "
                "WHERE restriction_id=? AND status='active' AND revision=?",
                (new_status, actor_id, self._now(), restriction_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("限制状态或版本已变化")
            self.connection.execute(
                "UPDATE title_assets SET restriction_revision=restriction_revision+1 WHERE asset_id=?",
                (row["asset_id"],),
            )
            self._audit(
                "restriction",
                restriction_id,
                "restriction.resolved",
                actor_id,
                {"asset_id": row["asset_id"], "status": new_status, "note": note},
            )
        return {"restriction_id": restriction_id, "status": new_status, "revision": expected_revision + 1}

    def _idempotent_response(
        self, scope: str, key: str, request_digest: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM title_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def initiate_transfer(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        initiation = TransferInitiation.from_dict(raw)
        request_digest = digest(raw)
        stored = self._idempotent_response("transfer-initiate", initiation.idempotency_key, request_digest)
        if stored is not None:
            return stored
        self._party(initiation.seller_party_id)
        self._party(initiation.buyer_party_id)
        frozen_items: list[dict[str, Any]] = []
        for item in initiation.items:
            asset = self._asset(item.asset_id)
            if asset["owner_party_id"] != initiation.seller_party_id:
                raise Conflict(f"卖方不是资产 {item.asset_id} 的当前所有者")
            lock = self.connection.execute(
                "SELECT l.transfer_id FROM asset_transfer_locks l JOIN transfers t ON t.transfer_id=l.transfer_id "
                "WHERE l.asset_id=? AND t.state='collecting'",
                (item.asset_id,),
            ).fetchone()
            if lock is not None:
                raise Conflict(f"资产 {item.asset_id} 已被转让 {lock['transfer_id']} 锁定")
            base_fact = self._latest_fact(item.asset_id)
            target_custodian = item.target_custodian_party_id or asset["custodian_party_id"]
            target_operator = item.target_operator_party_id or asset["operator_party_id"]
            target_location = item.target_location or asset["location"]
            for party_id in (target_custodian, target_operator):
                self._party(party_id)
            frozen_items.append(
                {
                    "asset_id": item.asset_id,
                    "base_fact_id": base_fact["fact_id"],
                    "base_rights_revision": asset["rights_revision"],
                    "base_restriction_revision": asset["restriction_revision"],
                    "frozen_owner_party_id": asset["owner_party_id"],
                    "frozen_custodian_party_id": asset["custodian_party_id"],
                    "frozen_operator_party_id": asset["operator_party_id"],
                    "frozen_location": asset["location"],
                    "frozen_restrictions": self._restriction_snapshot(item.asset_id),
                    "target_owner_party_id": initiation.buyer_party_id,
                    "target_custodian_party_id": target_custodian,
                    "target_operator_party_id": target_operator,
                    "target_location": target_location,
                }
            )
        required: dict[str, dict[str, Any]] = {
            "buyer": {"consent_type": "buyer_accept", "restriction_id": None}
        }
        for frozen in frozen_items:
            for restriction in frozen["frozen_restrictions"]:
                scope = f"restriction:{restriction['restriction_id']}"
                required[scope] = {
                    "consent_type": "financier_release"
                    if restriction["kind"] == "pledge"
                    else "quality_confirm",
                    "restriction_id": restriction["restriction_id"],
                }
        created_at = self._now()
        response = {
            "transfer_id": initiation.transfer_id,
            "state": "collecting",
            "basis_revision": 1,
            "seller_party_id": initiation.seller_party_id,
            "buyer_party_id": initiation.buyer_party_id,
            "items": [frozen["asset_id"] for frozen in frozen_items],
            "required_consents": self._required_list(required),
            "created_at": created_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO transfers(transfer_id,seller_party_id,buyer_party_id,state,basis_revision,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,'collecting',1,?,?,?)",
                    (
                        initiation.transfer_id,
                        initiation.seller_party_id,
                        initiation.buyer_party_id,
                        initiation.idempotency_key,
                        actor_id,
                        created_at,
                    ),
                )
                for frozen in frozen_items:
                    self.connection.execute(
                        "INSERT INTO transfer_items(transfer_id,asset_id,base_fact_id,base_rights_revision,"
                        "base_restriction_revision,frozen_owner_party_id,frozen_custodian_party_id,"
                        "frozen_operator_party_id,frozen_location,frozen_restrictions_json,"
                        "target_owner_party_id,target_custodian_party_id,target_operator_party_id,"
                        "target_location) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            initiation.transfer_id,
                            frozen["asset_id"],
                            frozen["base_fact_id"],
                            frozen["base_rights_revision"],
                            frozen["base_restriction_revision"],
                            frozen["frozen_owner_party_id"],
                            frozen["frozen_custodian_party_id"],
                            frozen["frozen_operator_party_id"],
                            frozen["frozen_location"],
                            canonical_json(frozen["frozen_restrictions"]),
                            frozen["target_owner_party_id"],
                            frozen["target_custodian_party_id"],
                            frozen["target_operator_party_id"],
                            frozen["target_location"],
                        ),
                    )
                    self.connection.execute(
                        "INSERT INTO asset_transfer_locks(asset_id,transfer_id) VALUES(?,?)",
                        (frozen["asset_id"], initiation.transfer_id),
                    )
                self.connection.execute(
                    "INSERT INTO title_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('transfer-initiate',?,?,?,?)",
                    (initiation.idempotency_key, request_digest, canonical_json(response), created_at),
                )
                self._audit(
                    "transfer",
                    initiation.transfer_id,
                    "transfer.initiated",
                    actor_id,
                    {
                        "seller_party_id": initiation.seller_party_id,
                        "buyer_party_id": initiation.buyer_party_id,
                        "assets": response["items"],
                        "request_sha256": request_digest,
                    },
                )
        except sqlite3.IntegrityError as exc:
            if self.connection.execute(
                "SELECT 1 FROM transfers WHERE transfer_id=?", (initiation.transfer_id,)
            ).fetchone():
                raise Conflict("转让编号已经存在") from exc
            for item in initiation.items:
                lock = self.connection.execute(
                    "SELECT transfer_id FROM asset_transfer_locks WHERE asset_id=?", (item.asset_id,)
                ).fetchone()
                if lock is not None:
                    raise Conflict(f"资产 {item.asset_id} 已被转让 {lock['transfer_id']} 锁定") from exc
            raise Conflict("转让发起与现有记录冲突") from exc
        return response

    def sign_consent(self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        consent = ConsentInput.from_dict(raw)
        self._require(actor_id, CONSENT_PERMISSIONS[consent.consent_type])
        transfer = self._transfer(transfer_id)
        if transfer["state"] != "collecting":
            raise InvalidState("转让不在签署汇集状态")
        if transfer["created_by"] == actor_id:
            raise Forbidden("发起人不能为自己的转让签署")
        result: dict[str, Any] | None = None
        replay: sqlite3.Row | None = None
        failure: ValidationFailed | InvalidState | None = None
        with transaction(self.connection, immediate=True):
            current = self._repair_basis_locked(self._transfer(transfer_id), actor_id)
            if current["state"] != "collecting":
                failure = InvalidState("转让不在签署汇集状态")
            else:
                required = self._required_scopes(self._transfer_items(transfer_id))
                expectation = required.get(consent.scope)
                if expectation is None:
                    failure = ValidationFailed("该签署不在当前必需签署清单中")
                elif expectation["consent_type"] != consent.consent_type:
                    failure = ValidationFailed("签署类型与必需签署清单不匹配")
                else:
                    replay = self.connection.execute(
                        "SELECT * FROM consents WHERE transfer_id=? AND scope=? AND basis_revision=? AND status='signed'",
                        (transfer_id, consent.scope, current["basis_revision"]),
                    ).fetchone()
                    if replay is None:
                        self.connection.execute(
                            "INSERT INTO consents(transfer_id,scope,consent_type,basis_revision,status,actor_id,"
                            "note,signed_at) VALUES(?,?,?,?,'signed',?,?,?)",
                            (
                                transfer_id,
                                consent.scope,
                                consent.consent_type,
                                current["basis_revision"],
                                actor_id,
                                consent.note,
                                self._now(),
                            ),
                        )
                        self._audit(
                            "transfer",
                            transfer_id,
                            "transfer.consent_signed",
                            actor_id,
                            {
                                "scope": consent.scope,
                                "consent_type": consent.consent_type,
                                "basis_revision": current["basis_revision"],
                            },
                        )
                        result = {
                            "transfer_id": transfer_id,
                            "scope": consent.scope,
                            "consent_type": consent.consent_type,
                            "basis_revision": current["basis_revision"],
                            "status": "signed",
                            "replayed": False,
                        }
        if failure is not None:
            raise failure
        if replay is not None:
            return {
                "transfer_id": transfer_id,
                "scope": replay["scope"],
                "consent_type": replay["consent_type"],
                "basis_revision": replay["basis_revision"],
                "status": "signed",
                "replayed": True,
            }
        return result

    def _apply_closing(
        self, transfer: sqlite3.Row, items: list[sqlite3.Row], actor_id: str
    ) -> dict[str, Any]:
        """在持有写事务且依据版本一致时原子应用交割效果。"""
        transfer_id = transfer["transfer_id"]
        now = self._now()
        facts: list[dict[str, Any]] = []
        for item in items:
            asset = self._asset(item["asset_id"])
            if asset["rights_revision"] != item["base_rights_revision"]:
                raise InvalidState(f"资产 {item['asset_id']} 的权利版本已变化")
            base_fact = self.connection.execute(
                "SELECT * FROM rights_facts WHERE fact_id=?", (item["base_fact_id"],)
            ).fetchone()
            cursor = self.connection.execute(
                "INSERT INTO rights_facts(asset_id,seq,owner_party_id,custodian_party_id,operator_party_id,"
                "location,source_kind,source_id,prev_fact_id,effective_at,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["asset_id"],
                    base_fact["seq"] + 1,
                    item["target_owner_party_id"],
                    item["target_custodian_party_id"],
                    item["target_operator_party_id"],
                    item["target_location"],
                    "transfer",
                    transfer_id,
                    item["base_fact_id"],
                    now,
                    now,
                ),
            )
            updated = self.connection.execute(
                "UPDATE title_assets SET owner_party_id=?,custodian_party_id=?,operator_party_id=?,"
                "location=?,rights_revision=rights_revision+1 WHERE asset_id=? AND rights_revision=?",
                (
                    item["target_owner_party_id"],
                    item["target_custodian_party_id"],
                    item["target_operator_party_id"],
                    item["target_location"],
                    item["asset_id"],
                    item["base_rights_revision"],
                ),
            )
            if updated.rowcount != 1:
                raise InvalidState(f"资产 {item['asset_id']} 的权利版本已变化")
            facts.append(
                {
                    "asset_id": item["asset_id"],
                    "fact_id": int(cursor.lastrowid),
                    "seq": base_fact["seq"] + 1,
                    "owner_party_id": item["target_owner_party_id"],
                    "custodian_party_id": item["target_custodian_party_id"],
                    "operator_party_id": item["target_operator_party_id"],
                    "location": item["target_location"],
                }
            )
        released: list[str] = []
        consent_rows = self.connection.execute(
            "SELECT * FROM consents WHERE transfer_id=? AND basis_revision=? AND status='signed' "
            "AND consent_type IN ('financier_release','quality_confirm') ORDER BY consent_id",
            (transfer_id, transfer["basis_revision"]),
        ).fetchall()
        for consent_row in consent_rows:
            restriction_id = consent_row["scope"].split(":", 1)[1]
            restriction = self.connection.execute(
                "SELECT * FROM restrictions WHERE restriction_id=?", (restriction_id,)
            ).fetchone()
            updated = self.connection.execute(
                "UPDATE restrictions SET status='released',resolved_by=?,resolved_at=?,"
                "released_by_transfer_id=?,revision=revision+1 "
                "WHERE restriction_id=? AND status='active'",
                (actor_id, now, transfer_id, restriction_id),
            )
            if restriction is None or updated.rowcount != 1:
                raise InvalidState(f"限制 {restriction_id} 状态已变化")
            self.connection.execute(
                "UPDATE title_assets SET restriction_revision=restriction_revision+1 WHERE asset_id=?",
                (restriction["asset_id"],),
            )
            released.append(restriction_id)
        self.connection.execute(
            "DELETE FROM asset_transfer_locks WHERE transfer_id=?", (transfer_id,)
        )
        updated = self.connection.execute(
            "UPDATE transfers SET state='closed',closed_at=? WHERE transfer_id=? AND state='collecting'",
            (now, transfer_id),
        )
        if updated.rowcount != 1:
            raise InvalidState("转让状态已变化")
        self._audit(
            "transfer",
            transfer_id,
            "transfer.closed",
            actor_id,
            {
                "basis_revision": transfer["basis_revision"],
                "facts": facts,
                "released_restrictions": released,
            },
        )
        return {
            "transfer_id": transfer_id,
            "state": "closed",
            "basis_revision": transfer["basis_revision"],
            "closed_at": now,
            "facts": facts,
            "released_restrictions": released,
            "replayed": False,
        }

    def _close_response(self, transfer_id: str, *, replayed: bool) -> dict[str, Any]:
        transfer = self._transfer(transfer_id)
        facts = self.connection.execute(
            "SELECT * FROM rights_facts WHERE source_kind='transfer' AND source_id=? ORDER BY asset_id,seq",
            (transfer_id,),
        ).fetchall()
        released = self.connection.execute(
            "SELECT restriction_id FROM restrictions WHERE released_by_transfer_id=? ORDER BY restriction_id",
            (transfer_id,),
        ).fetchall()
        return {
            "transfer_id": transfer_id,
            "state": "closed",
            "basis_revision": transfer["basis_revision"],
            "closed_at": transfer["closed_at"],
            "facts": [
                {
                    "asset_id": fact["asset_id"],
                    "fact_id": fact["fact_id"],
                    "seq": fact["seq"],
                    "owner_party_id": fact["owner_party_id"],
                    "custodian_party_id": fact["custodian_party_id"],
                    "operator_party_id": fact["operator_party_id"],
                    "location": fact["location"],
                }
                for fact in facts
            ],
            "released_restrictions": [row["restriction_id"] for row in released],
            "replayed": replayed,
        }

    def close_transfer(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        transfer = self._transfer(transfer_id)
        if transfer["state"] == "closed":
            return self._close_response(transfer_id, replayed=True)
        if transfer["state"] == "cancelled":
            raise InvalidState("转让已撤销，不能交割")
        outcome: dict[str, Any] | None = None
        replay = False
        failure: InvalidState | None = None
        with transaction(self.connection, immediate=True):
            fresh = self._transfer(transfer_id)
            if fresh["state"] == "closed":
                replay = True
            elif fresh["state"] == "cancelled":
                failure = InvalidState("转让已撤销，不能交割")
            else:
                repaired = self._repair_basis_locked(fresh, actor_id)
                if repaired["basis_revision"] != fresh["basis_revision"]:
                    failure = InvalidState("依据版本已变化，未完成签署已失效，请重新汇集签署")
                else:
                    items = self._transfer_items(transfer_id)
                    required = self._required_scopes(items)
                    signed = {
                        row["scope"]
                        for row in self.connection.execute(
                            "SELECT scope FROM consents WHERE transfer_id=? AND basis_revision=? AND status='signed'",
                            (transfer_id, fresh["basis_revision"]),
                        ).fetchall()
                    }
                    missing = [scope for scope in required if scope not in signed]
                    if missing:
                        failure = InvalidState("缺少必要签署: " + ", ".join(sorted(missing)))
                    else:
                        outcome = self._apply_closing(fresh, items, actor_id)
        if failure is not None:
            raise failure
        if replay:
            return self._close_response(transfer_id, replayed=True)
        return outcome

    def cancel_transfer(self, actor_id: str, transfer_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        reason_text = required_text(reason, "reason", 512)
        transfer = self._transfer(transfer_id)
        if transfer["state"] == "cancelled":
            raise InvalidState("转让已经撤销")
        if transfer["state"] == "closed":
            raise InvalidState("转让已交割，只能退回不能撤销")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE transfers SET state='cancelled',cancelled_at=?,cancel_reason=? "
                "WHERE transfer_id=? AND state='collecting'",
                (self._now(), reason_text, transfer_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("转让状态已变化")
            self.connection.execute(
                "DELETE FROM asset_transfer_locks WHERE transfer_id=?", (transfer_id,)
            )
            self._audit(
                "transfer", transfer_id, "transfer.cancelled", actor_id, {"reason": reason_text}
            )
        return {"transfer_id": transfer_id, "state": "cancelled", "cancel_reason": reason_text}

    def reverse_transfer(
        self, actor_id: str, transfer_id: str, reversal_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        reversal_id = identifier(reversal_id, "reversal_id")
        reason_text = required_text(reason, "reason", 512)
        transfer = self._transfer(transfer_id)
        if transfer["state"] != "closed":
            raise InvalidState("只有已交割的转让可以退回")
        existing = self.connection.execute(
            "SELECT * FROM reversals WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if existing is not None:
            if existing["reversal_id"] == reversal_id:
                return self._reversal_response(existing["reversal_id"], transfer_id, replayed=True)
            raise Conflict("该转让已经退回")
        if self.connection.execute(
            "SELECT 1 FROM reversals WHERE reversal_id=?", (reversal_id,)
        ).fetchone():
            raise Conflict("退回编号已经存在")
        closing_facts = self.connection.execute(
            "SELECT * FROM rights_facts WHERE source_kind='transfer' AND source_id=? ORDER BY asset_id,seq",
            (transfer_id,),
        ).fetchall()
        for fact in closing_facts:
            asset = self._asset(fact["asset_id"])
            if asset["rights_revision"] != fact["seq"]:
                raise Conflict(f"资产 {fact['asset_id']} 的权利已再次变动，不能退回")
        now = self._now()
        new_facts: list[dict[str, Any]] = []
        try:
            with transaction(self.connection, immediate=True):
                for fact in closing_facts:
                    asset = self._asset(fact["asset_id"])
                    if asset["rights_revision"] != fact["seq"]:
                        raise Conflict(f"资产 {fact['asset_id']} 的权利已再次变动，不能退回")
                    previous = self.connection.execute(
                        "SELECT * FROM rights_facts WHERE fact_id=?", (fact["prev_fact_id"],)
                    ).fetchone()
                    cursor = self.connection.execute(
                        "INSERT INTO rights_facts(asset_id,seq,owner_party_id,custodian_party_id,"
                        "operator_party_id,location,source_kind,source_id,prev_fact_id,effective_at,"
                        "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            fact["asset_id"],
                            fact["seq"] + 1,
                            previous["owner_party_id"],
                            previous["custodian_party_id"],
                            previous["operator_party_id"],
                            previous["location"],
                            "reversal",
                            reversal_id,
                            fact["fact_id"],
                            now,
                            now,
                        ),
                    )
                    updated = self.connection.execute(
                        "UPDATE title_assets SET owner_party_id=?,custodian_party_id=?,operator_party_id=?,"
                        "location=?,rights_revision=rights_revision+1 WHERE asset_id=? AND rights_revision=?",
                        (
                            previous["owner_party_id"],
                            previous["custodian_party_id"],
                            previous["operator_party_id"],
                            previous["location"],
                            fact["asset_id"],
                            fact["seq"],
                        ),
                    )
                    if updated.rowcount != 1:
                        raise Conflict(f"资产 {fact['asset_id']} 的权利已再次变动，不能退回")
                    new_facts.append(
                        {
                            "asset_id": fact["asset_id"],
                            "fact_id": int(cursor.lastrowid),
                            "seq": fact["seq"] + 1,
                            "owner_party_id": previous["owner_party_id"],
                            "custodian_party_id": previous["custodian_party_id"],
                            "operator_party_id": previous["operator_party_id"],
                            "location": previous["location"],
                        }
                    )
                self.connection.execute(
                    "INSERT INTO reversals(reversal_id,transfer_id,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (reversal_id, transfer_id, reason_text, actor_id, now),
                )
                self._audit(
                    "transfer",
                    transfer_id,
                    "transfer.reversed",
                    actor_id,
                    {"reversal_id": reversal_id, "reason": reason_text, "facts": new_facts},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("退回与现有记录冲突") from exc
        return {
            "reversal_id": reversal_id,
            "transfer_id": transfer_id,
            "state": "reversed",
            "facts": new_facts,
            "replayed": False,
        }

    def _reversal_response(
        self, reversal_id: str, transfer_id: str, *, replayed: bool
    ) -> dict[str, Any]:
        facts = self.connection.execute(
            "SELECT * FROM rights_facts WHERE source_kind='reversal' AND source_id=? ORDER BY asset_id,seq",
            (reversal_id,),
        ).fetchall()
        return {
            "reversal_id": reversal_id,
            "transfer_id": transfer_id,
            "state": "reversed",
            "facts": [
                {
                    "asset_id": fact["asset_id"],
                    "fact_id": fact["fact_id"],
                    "seq": fact["seq"],
                    "owner_party_id": fact["owner_party_id"],
                    "custodian_party_id": fact["custodian_party_id"],
                    "operator_party_id": fact["operator_party_id"],
                    "location": fact["location"],
                }
                for fact in facts
            ],
            "replayed": replayed,
        }

    def transfer_status(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        transfer = self._transfer(transfer_id)
        items = self._transfer_items(transfer_id)
        required = self._required_scopes(items)
        consent_rows = self.connection.execute(
            "SELECT * FROM consents WHERE transfer_id=? ORDER BY consent_id", (transfer_id,)
        ).fetchall()
        signed_valid = {
            row["scope"]
            for row in consent_rows
            if row["status"] == "signed" and row["basis_revision"] == transfer["basis_revision"]
        }
        item_dicts: list[dict[str, Any]] = []
        basis_stale = False
        for item in items:
            asset = self._asset(item["asset_id"])
            drifted = (
                asset["rights_revision"] != item["base_rights_revision"]
                or asset["restriction_revision"] != item["base_restriction_revision"]
            )
            basis_stale = basis_stale or drifted
            item_dicts.append(
                {
                    "asset_id": item["asset_id"],
                    "frozen": {
                        "owner_party_id": item["frozen_owner_party_id"],
                        "custodian_party_id": item["frozen_custodian_party_id"],
                        "operator_party_id": item["frozen_operator_party_id"],
                        "location": item["frozen_location"],
                        "rights_revision": item["base_rights_revision"],
                        "restriction_revision": item["base_restriction_revision"],
                        "restrictions": json.loads(item["frozen_restrictions_json"]),
                    },
                    "target": {
                        "owner_party_id": item["target_owner_party_id"],
                        "custodian_party_id": item["target_custodian_party_id"],
                        "operator_party_id": item["target_operator_party_id"],
                        "location": item["target_location"],
                    },
                    "current_rights_revision": asset["rights_revision"],
                    "current_restriction_revision": asset["restriction_revision"],
                    "drifted": drifted,
                }
            )
        reversal = self.connection.execute(
            "SELECT * FROM reversals WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        return {
            "transfer_id": transfer_id,
            "state": transfer["state"],
            "basis_revision": transfer["basis_revision"],
            "basis_stale": basis_stale if transfer["state"] == "collecting" else False,
            "seller_party_id": transfer["seller_party_id"],
            "buyer_party_id": transfer["buyer_party_id"],
            "created_by": transfer["created_by"],
            "created_at": transfer["created_at"],
            "closed_at": transfer["closed_at"],
            "cancelled_at": transfer["cancelled_at"],
            "cancel_reason": transfer["cancel_reason"],
            "items": item_dicts,
            "required_consents": self._required_list(required),
            "missing_consents": sorted(scope for scope in required if scope not in signed_valid)
            if transfer["state"] == "collecting"
            else [],
            "consents": [
                {
                    "scope": row["scope"],
                    "consent_type": row["consent_type"],
                    "basis_revision": row["basis_revision"],
                    "status": row["status"],
                    "actor_id": row["actor_id"],
                    "note": row["note"],
                    "signed_at": row["signed_at"],
                }
                for row in consent_rows
            ],
            "reversal": None
            if reversal is None
            else {
                "reversal_id": reversal["reversal_id"],
                "reason": reversal["reason"],
                "created_by": reversal["created_by"],
                "created_at": reversal["created_at"],
            },
        }

    def asset_rights(self, actor_id: str, asset_id: str, as_of: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        self._asset(asset_id)
        if as_of is None:
            fact = self._latest_fact(asset_id)
            moment = self._now()
        else:
            try:
                moment = utc_text(parse_utc(as_of, "as_of"))
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
            fact = self.connection.execute(
                "SELECT * FROM rights_facts WHERE asset_id=? AND effective_at<=? "
                "ORDER BY effective_at DESC,seq DESC LIMIT 1",
                (asset_id, moment),
            ).fetchone()
            if fact is None:
                raise NotFound("该时点之前没有权利记录")
        return {
            "asset_id": asset_id,
            "as_of": moment,
            "fact_id": fact["fact_id"],
            "seq": fact["seq"],
            "owner_party_id": fact["owner_party_id"],
            "custodian_party_id": fact["custodian_party_id"],
            "operator_party_id": fact["operator_party_id"],
            "location": fact["location"],
            "source_kind": fact["source_kind"],
            "source_id": fact["source_id"],
            "effective_at": fact["effective_at"],
        }

    def asset_blockers(self, actor_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        self._asset(asset_id)
        restrictions = self._active_restrictions(asset_id)
        lock = self.connection.execute(
            "SELECT l.transfer_id,t.state FROM asset_transfer_locks l "
            "JOIN transfers t ON t.transfer_id=l.transfer_id WHERE l.asset_id=?",
            (asset_id,),
        ).fetchone()
        required = [
            {
                "scope": f"restriction:{row['restriction_id']}",
                "consent_type": "financier_release" if row["kind"] == "pledge" else "quality_confirm",
                "restriction_id": row["restriction_id"],
            }
            for row in restrictions
        ]
        return {
            "asset_id": asset_id,
            "transferable": lock is None,
            "locked_by_transfer_id": None if lock is None else lock["transfer_id"],
            "active_restrictions": [
                {
                    "restriction_id": row["restriction_id"],
                    "kind": row["kind"],
                    "holder_party_id": row["holder_party_id"],
                    "reason": row["reason"],
                    "imposed_at": row["imposed_at"],
                    "revision": row["revision"],
                }
                for row in restrictions
            ],
            "required_consents": [{"scope": "buyer", "consent_type": "buyer_accept", "restriction_id": None}]
            + required,
        }

    def asset_chain(self, actor_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        asset = self._asset(asset_id)
        facts = self.connection.execute(
            "SELECT * FROM rights_facts WHERE asset_id=? ORDER BY seq", (asset_id,)
        ).fetchall()
        restrictions = self.connection.execute(
            "SELECT * FROM restrictions WHERE asset_id=? ORDER BY imposed_at,restriction_id",
            (asset_id,),
        ).fetchall()
        return {
            "asset_id": asset_id,
            "current": {
                "owner_party_id": asset["owner_party_id"],
                "custodian_party_id": asset["custodian_party_id"],
                "operator_party_id": asset["operator_party_id"],
                "location": asset["location"],
                "rights_revision": asset["rights_revision"],
                "restriction_revision": asset["restriction_revision"],
            },
            "facts": [
                {
                    "fact_id": fact["fact_id"],
                    "seq": fact["seq"],
                    "owner_party_id": fact["owner_party_id"],
                    "custodian_party_id": fact["custodian_party_id"],
                    "operator_party_id": fact["operator_party_id"],
                    "location": fact["location"],
                    "source_kind": fact["source_kind"],
                    "source_id": fact["source_id"],
                    "prev_fact_id": fact["prev_fact_id"],
                    "effective_at": fact["effective_at"],
                    "is_current": fact["seq"] == asset["rights_revision"],
                }
                for fact in facts
            ],
            "restrictions": [
                {
                    "restriction_id": row["restriction_id"],
                    "kind": row["kind"],
                    "holder_party_id": row["holder_party_id"],
                    "reason": row["reason"],
                    "status": row["status"],
                    "revision": row["revision"],
                    "imposed_by": row["imposed_by"],
                    "imposed_at": row["imposed_at"],
                    "resolved_by": row["resolved_by"],
                    "resolved_at": row["resolved_at"],
                    "released_by_transfer_id": row["released_by_transfer_id"],
                }
                for row in restrictions
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM title_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
