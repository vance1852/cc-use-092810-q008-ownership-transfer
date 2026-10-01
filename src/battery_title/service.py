"""权属流转领域用例。

流程：
1. 卖方发起转让（propose）：原子冻结资产清单、权利声明、保管位置、关联限制
   与前置条件，并对同一资产上的竞争提案按 (created_at, transfer_id) 确定唯一领跑者。
2. 买方接受、融资方解押、质量/召回确认分别以带依据版本的同意书汇集；任一资产
   依据版本变化都会使已给出的同意失效。
3. 全部前置条件满足且本提案是竞争领跑者时，settle 在单事务内原子写权属事实；
   条件不满足时交割被阻断，已完成的同意全部保留，权属不发生任何变化。
4. 交割失败只置状态；撤销（settle 前）保留全部历史；退回（settle 后）写与
   title_changed 对称的 title_reversed 反向事实，不删除任何记录。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .contracts import CLAIM_KINDS, TransferProposal
from .errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    SettlementBlocked,
    ValidationFailed,
)
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "seller": {
        "catalog.write", "restriction.write",
        "transfer.propose", "transfer.settle", "transfer.cancel", "transfer.reverse",
    },
    "financier": {"restriction.write", "consent.give"},
    "buyer": {"consent.give", "transfer.settle"},
    "quality": {"consent.give", "asset.revise", "restriction.write"},
    "finance": {"title.read"},
    "ops": {"title.read"},
    "auditor": {"title.read", "audit.read"},
}

PARTY_KINDS = {"company", "site", "financier", "vendor", "other"}
RESTRICTION_KINDS = {"pledge", "recall", "lien", "lock", "other"}
# 每类限制能够解除它的确认类型：质押/留置/锁定靠融资方解押，召回靠质量确认。
CLEARANCE_KINDS: Mapping[str, set[str]] = {
    "pledge": {"release"},
    "lien": {"release"},
    "lock": {"release"},
    "recall": {"quality_clearance"},
    "other": {"release", "quality_clearance"},
}
ACCEPTANCE_CONDITION_ID = "@acceptance"


def _covers(restriction_kind: str, condition_kind: str) -> bool:
    return condition_kind in CLEARANCE_KINDS.get(restriction_kind, set())


class TitleService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础辅助 -----

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
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM title_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(row["response_json"])

    def _save_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO title_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    def _append_event(
        self,
        asset_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        basis_revision: int,
        actor_id: str,
        transfer_id: str | None = None,
        reversed_event_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO title_events(asset_id,event_type,payload_json,basis_revision,transfer_id,"
            "reversed_event_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                asset_id, event_type, canonical_json(payload), basis_revision, transfer_id,
                reversed_event_id, actor_id, self._now(),
            ),
        )
        return int(cursor.lastrowid)

    # ----- 目录与资产登记 -----

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

    def register_party(self, actor_id: str, party_id: str, name: str, kind: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if kind not in PARTY_KINDS:
            raise ValidationFailed(f"kind 必须是 {', '.join(sorted(PARTY_KINDS))} 之一")
        if not party_id.strip() or not name.strip():
            raise ValidationFailed("主体编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO parties(party_id,name,kind,created_at) VALUES(?,?,?,?)",
                    (party_id.strip(), name.strip(), kind, self._now()),
                )
                self._audit("party", party_id.strip(), "party.registered", actor_id, {"name": name, "kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("主体编号已经存在") from exc
        return {"party_id": party_id.strip(), "kind": kind}

    def _party(self, party_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM parties WHERE party_id=?", (party_id,)).fetchone()
        if row is None:
            raise NotFound(f"权属主体不存在: {party_id}")
        return row

    def register_asset(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        asset_id = str(raw.get("asset_id", "")).strip()
        name = str(raw.get("name", "")).strip()
        location = str(raw.get("custodial_location", "")).strip()
        if not asset_id or not name or not location:
            raise ValidationFailed("asset_id、name、custodial_location 不能为空")
        claims = {}
        for kind in CLAIM_KINDS:
            party = raw.get(f"{kind}_party")
            if party is None:
                raise ValidationFailed(f"缺少初始 {kind}_party")
            claims[kind] = str(party).strip()
        evidence = raw.get("evidence_sha256")
        if evidence is not None and (not isinstance(evidence, str) or len(evidence) != 64):
            raise ValidationFailed("evidence_sha256 必须是 64 位摘要")
        with transaction(self.connection, immediate=True):
            for party_id in set(claims.values()):  # 校验初始三权主体存在
                self._party(party_id)
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO title_assets(asset_id,name,current_revision,evidence_sha256,created_at,updated_at) "
                    "VALUES(?,?,1,?,?,?)",
                    (asset_id, name, evidence, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("资产编号已经存在") from exc
            self._append_event(
                asset_id, "enrolled",
                {"name": name, "claims": claims, "custodial_location": location},
                1, actor_id,
            )
            self._audit("asset", asset_id, "asset.registered", actor_id, {"claims": claims})
        return {"asset_id": asset_id, "revision": 1, **claims}

    def revise_asset(
        self, actor_id: str, asset_id: str, evidence_sha256: str | None, note: str
    ) -> dict[str, Any]:
        """外部台账/证据升版。升版使引用旧版本的未完成签署全部失效。"""
        self._require(actor_id, "asset.revise")
        if len(note.strip()) == 0:
            raise ValidationFailed("升版原因不能为空")
        with transaction(self.connection, immediate=True):
            asset = self.connection.execute(
                "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if asset is None:
                raise NotFound("资产不存在")
            new_revision = int(asset["current_revision"]) + 1
            self.connection.execute(
                "UPDATE title_assets SET current_revision=?,evidence_sha256=?,updated_at=? WHERE asset_id=?",
                (new_revision, evidence_sha256, self._now(), asset_id),
            )
            self._append_event(
                asset_id, "basis_revised",
                {"from_revision": asset["current_revision"], "to_revision": new_revision,
                 "evidence_sha256": evidence_sha256, "note": note},
                new_revision, actor_id,
            )
            self._audit("asset", asset_id, "asset.revised", actor_id,
                        {"to_revision": new_revision, "note": note})
        return {"asset_id": asset_id, "revision": new_revision}

    # ----- 限制（质押/召回/留置/锁定） -----

    def put_restriction(
        self, actor_id: str, asset_id: str, restriction_id: str, kind: str,
        holder_party: str | None, note: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "restriction.write")
        if kind not in RESTRICTION_KINDS:
            raise ValidationFailed(f"kind 必须是 {', '.join(sorted(RESTRICTION_KINDS))} 之一")
        with transaction(self.connection, immediate=True):
            asset = self.connection.execute(
                "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if asset is None:
                raise NotFound("资产不存在")
            if holder_party is not None:
                self._party(holder_party)
            current = self._fold(asset_id)
            if restriction_id in current["restrictions"]:
                raise Conflict("该限制已处于生效状态")
            new_revision = int(asset["current_revision"]) + 1
            payload = {"restriction_id": restriction_id, "kind": kind,
                       "holder_party": holder_party, "note": note}
            self.connection.execute(
                "UPDATE title_assets SET current_revision=?,updated_at=? WHERE asset_id=?",
                (new_revision, self._now(), asset_id),
            )
            self._append_event(asset_id, "restriction_put_on", payload, new_revision, actor_id)
            self._audit("asset", asset_id, "restriction.put_on", actor_id, payload)
        return {"asset_id": asset_id, "restriction_id": restriction_id, "revision": new_revision}

    def release_restriction(
        self, actor_id: str, asset_id: str, restriction_id: str, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "restriction.write")
        with transaction(self.connection, immediate=True):
            asset = self.connection.execute(
                "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if asset is None:
                raise NotFound("资产不存在")
            current = self._fold(asset_id)
            if restriction_id not in current["restrictions"]:
                raise InvalidState("该限制当前未生效，不能解除")
            new_revision = int(asset["current_revision"]) + 1
            payload = {"restriction_id": restriction_id, "note": note}
            self.connection.execute(
                "UPDATE title_assets SET current_revision=?,updated_at=? WHERE asset_id=?",
                (new_revision, self._now(), asset_id),
            )
            self._append_event(asset_id, "restriction_released", payload, new_revision, actor_id)
            self._audit("asset", asset_id, "restriction.released", actor_id, payload)
        return {"asset_id": asset_id, "restriction_id": restriction_id, "revision": new_revision}

    # ----- 转让发起 -----

    def propose_transfer(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "transfer.propose")
        proposal = TransferProposal.from_dict(raw)
        idempotency_key = str(raw.get("idempotency_key", "")).strip()
        if not idempotency_key:
            raise ValidationFailed("idempotency_key 不能为空")
        request_digest = content_digest(raw)
        cached = self._idempotent("transfer.propose", idempotency_key, request_digest)
        if cached is not None:
            return cached
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT transfer_id FROM transfers WHERE transfer_id=?", (proposal.transfer_id,)
            ).fetchone()
            if existing is not None:
                raise Conflict("转让编号已经存在，重试须使用相同幂等键")
            self._party(proposal.seller_party)
            self._party(proposal.buyer_party)

            frozen_assets: list[dict[str, Any]] = []
            revisions: dict[str, int] = {}
            restrictions: dict[str, dict[str, Any]] = {}
            for item in proposal.assets:
                asset = self.connection.execute(
                    "SELECT * FROM title_assets WHERE asset_id=?", (item.asset_id,)
                ).fetchone()
                if asset is None:
                    raise NotFound(f"资产不存在: {item.asset_id}")
                current = self._fold(item.asset_id)
                if current["owner"] != proposal.seller_party:
                    raise InvalidState(
                        f"卖方 {proposal.seller_party} 不是资产 {item.asset_id} 的当前所有人"
                    )
                for claim in proposal.claims:
                    if claim.current_party is not None and current[claim.kind] != claim.current_party:
                        raise InvalidState(
                            f"资产 {item.asset_id} 的 {claim.kind} 当前权利人为 "
                            f"{current[claim.kind]}，与声明 {claim.current_party} 不符"
                        )
                if item.revision != int(asset["current_revision"]):
                    raise Conflict(
                        f"资产 {item.asset_id} 依据版本已变化：期望 {item.revision}，"
                        f"当前 {asset['current_revision']}"
                    )
                revisions[item.asset_id] = item.revision
                frozen_assets.append({
                    "asset_id": item.asset_id,
                    "revision": item.revision,
                    "evidence_sha256": item.evidence_sha256 or asset["evidence_sha256"],
                })
                for rid, restriction in current["restrictions"].items():
                    entry = restrictions.setdefault(rid, {
                        "restriction_id": rid,
                        "kind": restriction["kind"],
                        "holder_party": restriction["holder_party"],
                        "note": restriction.get("note", ""),
                        "asset_ids": [],
                    })
                    entry["asset_ids"].append(item.asset_id)

            # 卖方声明的限制必须与台账现状一致；系统冻结的是完整快照。
            for declared in proposal.restrictions:
                if declared.status == "active" and declared.restriction_id not in restrictions:
                    raise InvalidState(
                        f"限制 {declared.restriction_id} 在台账上并非生效状态，不能冻结为关联限制"
                    )

            for claim in proposal.claims:
                self._party(claim.next_party)

            conditions = [
                {"condition_id": ACCEPTANCE_CONDITION_ID, "party": "buyer",
                 "kind": "acceptance", "restriction_id": None, "required": True},
            ]
            for condition in proposal.conditions:
                if condition.restriction_id is not None and condition.restriction_id not in restrictions:
                    raise InvalidState(
                        f"条件 {condition.condition_id} 引用的限制当前未生效: "
                        f"{condition.restriction_id}"
                    )
                conditions.append({
                    "condition_id": condition.condition_id,
                    "party": condition.party,
                    "kind": condition.kind,
                    "restriction_id": condition.restriction_id,
                    "required": condition.required,
                })

            frozen = {
                "transfer_id": proposal.transfer_id,
                "seller_party": proposal.seller_party,
                "buyer_party": proposal.buyer_party,
                "assets": frozen_assets,
                "claims": [
                    {"kind": claim.kind, "current_party": claim.current_party,
                     "next_party": claim.next_party}
                    for claim in proposal.claims
                ],
                "custodial_location": proposal.custodial_location,
                "restrictions": sorted(restrictions.values(), key=lambda item: item["restriction_id"]),
                "conditions": conditions,
                "basis_version": proposal.basis_version,
                "note": proposal.note,
            }
            now = self._now()
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,seller_party,buyer_party,proposal_json,"
                "proposal_sha256,basis_version,assets_revision_json,state,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal.transfer_id, proposal.seller_party, proposal.buyer_party,
                    canonical_json(frozen), content_digest(frozen), proposal.basis_version,
                    canonical_json(revisions), "proposed", actor_id, now, now,
                ),
            )
            response = {
                "transfer_id": proposal.transfer_id,
                "state": "proposed",
                "frozen_sha256": content_digest(frozen),
                "assets": frozen_assets,
                "restrictions": frozen["restrictions"],
                "conditions": conditions,
            }
            self._save_idempotent("transfer.propose", idempotency_key, request_digest, response)
            self._audit("transfer", proposal.transfer_id, "transfer.proposed", actor_id,
                        {"assets": list(revisions), "sha256": response["frozen_sha256"]})
        return response

    # ----- 同意/确认 -----

    def _transfer(self, transfer_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise NotFound("转让不存在")
        return row

    def give_consent(
        self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "consent.give")
        user = self._user(actor_id)
        condition_id = str(raw.get("condition_id", "")).strip()
        outcome = str(raw.get("outcome", "")).strip()
        idempotency_key = str(raw.get("idempotency_key", "")).strip()
        note = str(raw.get("note", ""))
        if not condition_id or not idempotency_key:
            raise ValidationFailed("condition_id 与 idempotency_key 不能为空")
        if outcome not in {"cleared", "rejected"}:
            raise ValidationFailed("outcome 必须是 cleared 或 rejected")
        request_digest = content_digest({"transfer_id": transfer_id, **dict(raw)})
        cached = self._idempotent(f"consent:{transfer_id}", idempotency_key, request_digest)
        if cached is not None:
            return {**cached, "replayed": True}
        with transaction(self.connection, immediate=True):
            transfer = self._transfer(transfer_id)
            if transfer["state"] != "proposed":
                raise InvalidState(f"转让处于 {transfer['state']} 状态，不能再补充同意")
            proposal = json.loads(transfer["proposal_json"])
            condition = next(
                (item for item in proposal["conditions"] if item["condition_id"] == condition_id),
                None,
            )
            if condition is None:
                raise NotFound(f"前置条件不存在: {condition_id}")
            if user["role"] != condition["party"]:
                raise Forbidden(f"只有 {condition['party']} 角色可以完成条件 {condition_id}")
            # 同键重试：直接回读既有同意，绝不生成第二次签署。
            existing = self.connection.execute(
                "SELECT consent_id,basis_key FROM transfer_consents "
                "WHERE transfer_id=? AND idempotency_key=?",
                (transfer_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return {
                    "consent_id": existing["consent_id"], "transfer_id": transfer_id,
                    "condition_id": condition_id, "outcome": outcome,
                    "basis_key": existing["basis_key"], "replayed": True,
                }
            revisions = {item["asset_id"]: item["revision"] for item in proposal["assets"]}
            current_revisions = self._current_revisions(revisions)
            stale = sorted(
                asset_id for asset_id, revision in revisions.items()
                if current_revisions.get(asset_id) != revision
            )
            if stale:
                # 依据版本已变化：该提案的未完成签署不再可能生效，禁止再追加签署。
                raise InvalidState(
                    f"资产依据版本已变化（{', '.join(stale)}），请重新发起转让", stale
                )
            basis_key = canonical_json(current_revisions)
            try:
                cursor = self.connection.execute(
                    "INSERT INTO transfer_consents(transfer_id,condition_id,party_role,kind,outcome,"
                    "basis_key,note,idempotency_key,given_by,given_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        transfer_id, condition_id, condition["party"], condition["kind"], outcome,
                        basis_key, note[:512], idempotency_key, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该条件已由其他签署完成，重试不会生成第二次同意") from exc
            consent_id = int(cursor.lastrowid)
            if outcome == "rejected":
                self.connection.execute(
                    "UPDATE transfers SET state='failed',block_reason=?,updated_at=?,ended_at=? "
                    "WHERE transfer_id=? AND state='proposed'",
                    (f"rejected_outcome:{condition_id}", self._now(), self._now(), transfer_id),
                )
            response = {
                "consent_id": consent_id, "transfer_id": transfer_id,
                "condition_id": condition_id, "outcome": outcome, "basis_key": basis_key,
            }
            self._save_idempotent(f"consent:{transfer_id}", idempotency_key, request_digest, response)
            self._audit("transfer", transfer_id, "consent.given", actor_id,
                        {"condition_id": condition_id, "outcome": outcome})
            if outcome == "rejected":
                self._audit("transfer", transfer_id, "transfer.failed", actor_id,
                            {"reason": f"rejected_outcome:{condition_id}"})
        return response

    # ----- 竞争交易裁决 -----

    def _proposed_competitors(self, transfer_id: str, asset_ids: set[str]) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM transfers WHERE state='proposed' ORDER BY created_at,transfer_id"
        ).fetchall()
        result = []
        for row in rows:
            proposal = json.loads(row["proposal_json"])
            if {item["asset_id"] for item in proposal["assets"]} & asset_ids:
                result.append(row)
        return result

    # ----- 交割前评估（被阻断原因） -----

    def _current_revisions(self, asset_ids: Mapping[str, int]) -> dict[str, int]:
        result = {}
        for asset_id in asset_ids:
            row = self.connection.execute(
                "SELECT current_revision FROM title_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            result[asset_id] = None if row is None else int(row["current_revision"])
        return result

    def _evaluation(self, transfer: sqlite3.Row) -> dict[str, Any]:
        proposal = json.loads(transfer["proposal_json"])
        frozen_revisions = {item["asset_id"]: item["revision"] for item in proposal["assets"]}
        current_revisions = self._current_revisions(frozen_revisions)
        consents = self.connection.execute(
            "SELECT * FROM transfer_consents WHERE transfer_id=? ORDER BY consent_id",
            (transfer["transfer_id"],),
        ).fetchall()

        frozen_basis_key = canonical_json(frozen_revisions)
        reasons: list[str] = []

        if transfer["state"] != "proposed":
            return {"ready": False, "state": transfer["state"],
                    "reasons": [transfer["block_reason"] or f"terminal:{transfer['state']}"],
                    "stale_assets": [], "consents": [dict(row) for row in consents]}

        stale_assets = sorted(
            asset_id for asset_id, revision in frozen_revisions.items()
            if current_revisions.get(asset_id) != revision
        )
        if stale_assets:
            reasons.extend(f"stale_basis:{asset_id}" for asset_id in stale_assets)

        # 有效同意：给出时与当前的依据版本都必须等于冻结版本。
        valid_cleared: dict[str, sqlite3.Row] = {}
        rejected: list[str] = []
        for row in consents:
            if row["outcome"] == "rejected":
                rejected.append(row["condition_id"])
            valid = (
                row["basis_key"] == frozen_basis_key
                and canonical_json(current_revisions) == frozen_basis_key
                and row["outcome"] == "cleared"
            )
            if valid:
                valid_cleared[row["condition_id"]] = row
        reasons.extend(f"rejected_outcome:{condition_id}" for condition_id in rejected)

        for condition in proposal["conditions"]:
            if not condition["required"]:
                continue
            if condition["condition_id"] not in valid_cleared:
                reasons.append(f"awaiting_consent:{condition['condition_id']}")

        # 每一条生效中的关联限制都必须被相容的有效确认覆盖。
        for restriction in proposal["restrictions"]:
            rid = restriction["restriction_id"]
            covered = False
            for condition in proposal["conditions"]:
                if condition["restriction_id"] != rid:
                    continue
                consent = valid_cleared.get(condition["condition_id"])
                if consent is None:
                    continue
                if _covers(restriction["kind"], condition["kind"]):
                    covered = True
                    break
            if not covered:
                reasons.append(f"unresolved_restriction:{rid}:{restriction['kind']}")

        asset_ids = set(frozen_revisions)
        leader = None
        competitors = self._proposed_competitors(transfer["transfer_id"], asset_ids)
        if competitors:
            leader = competitors[0]["transfer_id"]
            if leader != transfer["transfer_id"]:
                reasons.append(f"contest_leader:{leader}")

        return {
            "ready": not reasons,
            "state": "proposed",
            "reasons": reasons,
            "stale_assets": stale_assets,
            "leader": leader,
            "consents": [dict(row) for row in consents],
        }

    def settle_transfer(self, actor_id: str, transfer_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "transfer.settle")
        # 阻断信息在事务内收集，事务提交后于事务外抛出，确保 failed 事实落库不被回滚。
        blocked: SettlementBlocked | None = None
        result: dict[str, Any] | None = None
        with transaction(self.connection, immediate=True):
            transfer = self._transfer(transfer_id)
            if transfer["state"] == "settled":
                # 交割幂等由状态机保证：重试只回读既有事实，绝不写第二次转让。
                event_ids = [row["event_id"] for row in self.connection.execute(
                    "SELECT event_id FROM title_events WHERE transfer_id=? AND event_type='title_changed' "
                    "ORDER BY event_id", (transfer_id,)
                ).fetchall()]
                result = {"transfer_id": transfer_id, "state": "settled",
                          "event_ids": event_ids, "settled_at": transfer["settled_at"], "replayed": True}
            elif transfer["state"] == "failed":
                reasons = [item for item in (transfer["block_reason"] or "").split(";") if item]
                blocked = SettlementBlocked("交割已失败：依据版本变化或存在拒绝确认", reasons)
            elif transfer["state"] in {"cancelled", "reversed"}:
                raise InvalidState(
                    f"转让已终结（{transfer['state']}），不能再次交割；已完成的同意保留在案",
                )
            else:
                proposal = json.loads(transfer["proposal_json"])
                evaluation = self._evaluation(transfer)

                # 终局失败：拒绝意见、依据版本变化。已完成的同意保留，权属不变。
                fatal = [reason for reason in evaluation["reasons"]
                         if reason.startswith(("rejected_outcome:", "stale_basis:"))]
                if fatal:
                    self.connection.execute(
                        "UPDATE transfers SET state='failed',block_reason=?,updated_at=?,ended_at=? "
                        "WHERE transfer_id=? AND state='proposed'",
                        (";".join(fatal), self._now(), self._now(), transfer_id),
                    )
                    self._audit("transfer", transfer_id, "transfer.failed", actor_id, {"reasons": fatal})
                    blocked = SettlementBlocked("交割失败：依据版本变化或存在拒绝确认", fatal)
                else:
                    competitors = self._proposed_competitors(
                        transfer_id, {item["asset_id"] for item in proposal["assets"]}
                    )
                    leader = competitors[0]["transfer_id"] if competitors else transfer_id
                    if leader != transfer_id:
                        # 可重试阻断：领跑提案尚未终结，竞争赢家已确定但需等待。
                        blocked = SettlementBlocked(
                            f"存在领先竞争交易 {leader}，本提案在其终结前不能交割",
                            evaluation["reasons"],
                        )
                    elif evaluation["reasons"]:
                        # 可重试阻断：同意未齐或限制未解。保留全部同意，权属不变。
                        blocked = SettlementBlocked("交割前置条件尚未满足", evaluation["reasons"])
                    else:
                        result = self._apply_settlement(actor_id, transfer, proposal)
        if blocked is not None:
            raise blocked
        assert result is not None
        return result

    def _apply_settlement(
        self, actor_id: str, transfer: sqlite3.Row, proposal: Mapping[str, Any]
    ) -> dict[str, Any]:
        """在已持有写事务、且全部前置条件满足时，原子写入权属事实。"""
        transfer_id = transfer["transfer_id"]
        claims_by_kind = {item["kind"]: item for item in proposal["claims"]}
        conditions = proposal["conditions"]
        consents = self.connection.execute(
            "SELECT * FROM transfer_consents WHERE transfer_id=? AND outcome='cleared'",
            (transfer_id,),
        ).fetchall()
        restriction_kinds = {
            item["restriction_id"]: item["kind"] for item in proposal["restrictions"]
        }
        cleared_condition_ids = {row["condition_id"] for row in consents}
        # 只有被相容确认覆盖的限制才随交割解除（质押要解押、召回到质量确认）。
        released_restriction_ids = {
            condition["restriction_id"]
            for condition in conditions
            if condition["restriction_id"] in restriction_kinds
            and condition["condition_id"] in cleared_condition_ids
            and _covers(restriction_kinds[condition["restriction_id"]], condition["kind"])
        }

        settled_event_ids: list[int] = []
        now = self._now()
        for frozen_asset in proposal["assets"]:
            asset_id = frozen_asset["asset_id"]
            current = self._fold(asset_id)
            changes = {}
            for kind, claim in claims_by_kind.items():
                changes[kind] = {"from": current[kind], "to": claim["next_party"]}
            released = [
                restriction for restriction in proposal["restrictions"]
                if asset_id in restriction["asset_ids"]
                and restriction["restriction_id"] in released_restriction_ids
            ]
            new_revision = int(current["revision"]) + 1
            payload = {
                "transfer_id": transfer_id,
                "basis_version": proposal["basis_version"],
                "changes": changes,
                "location": {"from": current["location"], "to": proposal["custodial_location"]},
                "released_restrictions": released,
            }
            event_id = self._append_event(
                asset_id, "title_changed", payload, new_revision, actor_id,
                transfer_id=transfer_id,
            )
            settled_event_ids.append(event_id)
            self.connection.execute(
                "UPDATE title_assets SET current_revision=?,updated_at=? WHERE asset_id=?",
                (new_revision, now, asset_id),
            )

        self.connection.execute(
            "UPDATE transfers SET state='settled',settled_event_id=?,updated_at=?,settled_at=? "
            "WHERE transfer_id=?",
            (settled_event_ids[0], now, now, transfer_id),
        )
        # 竞争交易在赢家原子生效的同一事务内确定落败。
        competitors_after = self._proposed_competitors(
            transfer_id, {item["asset_id"] for item in proposal["assets"]}
        )
        for loser in competitors_after:
            if loser["transfer_id"] == transfer_id:
                continue
            self.connection.execute(
                "UPDATE transfers SET state='failed',block_reason=?,updated_at=?,ended_at=? "
                "WHERE transfer_id=? AND state='proposed'",
                (f"lost_contest:{transfer_id}", now, now, loser["transfer_id"]),
            )
            self._audit("transfer", loser["transfer_id"], "transfer.failed", actor_id,
                        {"reason": f"lost_contest:{transfer_id}"})

        response = {
            "transfer_id": transfer_id,
            "state": "settled",
            "event_ids": settled_event_ids,
            "settled_at": now,
            "replayed": False,
        }
        self._audit("transfer", transfer_id, "transfer.settled", actor_id,
                    {"event_ids": settled_event_ids})
        return response

    # ----- 撤销（交割前）与退回（交割后） -----

    def cancel_transfer(self, actor_id: str, transfer_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "transfer.cancel")
        if not note.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            transfer = self._transfer(transfer_id)
            if transfer["state"] != "proposed":
                raise InvalidState(f"转让处于 {transfer['state']} 状态，不能撤销")
            self.connection.execute(
                "UPDATE transfers SET state='cancelled',block_reason=?,updated_at=?,ended_at=? "
                "WHERE transfer_id=? AND state='proposed'",
                # 权属从未改变，撤销不写权属反向事实；同意与提案全部留存。
                (f"cancelled:{note[:200]}", self._now(), self._now(), transfer_id),
            )
            self._audit("transfer", transfer_id, "transfer.cancelled", actor_id, {"note": note})
        return {"transfer_id": transfer_id, "state": "cancelled"}

    def reverse_transfer(self, actor_id: str, transfer_id: str, note: str) -> dict[str, Any]:
        """交割后退回：为每个资产生成 title_reversed 反向事实，历史保留不删。"""
        self._require(actor_id, "transfer.reverse")
        if not note.strip():
            raise ValidationFailed("退回原因不能为空")
        with transaction(self.connection, immediate=True):
            transfer = self._transfer(transfer_id)
            if transfer["state"] != "settled":
                raise InvalidState(f"只有已交割的转让可以退回，当前状态 {transfer['state']}")
            proposal = json.loads(transfer["proposal_json"])
            now = self._now()
            reversed_event_ids: list[int] = []
            for frozen_asset in proposal["assets"]:
                asset_id = frozen_asset["asset_id"]
                original = self.connection.execute(
                    "SELECT * FROM title_events WHERE transfer_id=? AND asset_id=? "
                    "AND event_type='title_changed' ORDER BY event_id LIMIT 1",
                    (transfer_id, asset_id),
                ).fetchone()
                if original is None:  # 防御性：理论不可能
                    raise InvalidState(f"资产 {asset_id} 缺少交割事实，无法退回")
                original_payload = json.loads(original["payload_json"])
                current = self._fold(asset_id)
                inverse_changes = {
                    kind: {"from": change["to"], "to": change["from"]}
                    for kind, change in original_payload["changes"].items()
                }
                new_revision = int(current["revision"]) + 1
                payload = {
                    "transfer_id": transfer_id,
                    "reason": note,
                    "changes": inverse_changes,
                    "location": {"from": original_payload["location"]["to"],
                                 "to": original_payload["location"]["from"]},
                    # 交割时解除的质押/召回等限制随退回重新生效。
                    "restored_restrictions": original_payload["released_restrictions"],
                }
                event_id = self._append_event(
                    asset_id, "title_reversed", payload, new_revision, actor_id,
                    transfer_id=transfer_id, reversed_event_id=original["event_id"],
                )
                reversed_event_ids.append(event_id)
                self.connection.execute(
                    "UPDATE title_assets SET current_revision=?,updated_at=? WHERE asset_id=?",
                    (new_revision, now, asset_id),
                )
            self.connection.execute(
                "UPDATE transfers SET state='reversed',updated_at=?,ended_at=? WHERE transfer_id=?",
                (now, now, transfer_id),
            )
            self._audit("transfer", transfer_id, "transfer.reversed", actor_id,
                        {"event_ids": reversed_event_ids, "reason": note})
        return {"transfer_id": transfer_id, "state": "reversed", "event_ids": reversed_event_ids}

    # ----- 查询：时点快照、权利链、转让详情 -----

    def _fold(self, asset_id: str, as_of: str | None = None) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM title_events WHERE asset_id=? ORDER BY event_id", (asset_id,)
        ).fetchall()
        state: dict[str, Any] = {
            "owner": None, "custodian": None, "operator": None,
            "location": None, "restrictions": {}, "revision": 0,
            "enrolled": False, "last_event": None, "last_transfer_id": None,
        }
        for row in rows:
            if as_of is not None and row["created_at"] > as_of:
                break
            payload = json.loads(row["payload_json"])
            state["revision"] = max(state["revision"], int(row["basis_revision"]))
            state["last_event"] = {"event_id": row["event_id"], "created_at": row["created_at"]}
            if row["event_type"] == "enrolled":
                for kind in CLAIM_KINDS:
                    state[kind] = payload["claims"][kind]
                state["location"] = payload["custodial_location"]
                state["enrolled"] = True
            elif row["event_type"] == "title_changed":
                state["last_transfer_id"] = payload["transfer_id"]
                for kind, change in payload["changes"].items():
                    state[kind] = change["to"]
                state["location"] = payload["location"]["to"]
                for restriction in payload["released_restrictions"]:
                    state["restrictions"].pop(restriction["restriction_id"], None)
            elif row["event_type"] == "title_reversed":
                state["last_transfer_id"] = payload["transfer_id"]
                for kind, change in payload["changes"].items():
                    state[kind] = change["to"]
                state["location"] = payload["location"]["to"]
                for restriction in payload["restored_restrictions"]:
                    state["restrictions"][restriction["restriction_id"]] = restriction
            elif row["event_type"] == "restriction_put_on":
                state["restrictions"][payload["restriction_id"]] = payload
            elif row["event_type"] == "restriction_released":
                state["restrictions"].pop(payload["restriction_id"], None)
            # basis_revised 只推进 revision，不改变三权。
        return state

    def title_at(self, actor_id: str, asset_id: str, as_of: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "title.read")
        asset = self.connection.execute(
            "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if asset is None:
            raise NotFound("资产不存在")
        as_of_text = None
        if as_of:
            as_of_text = utc_text(parse_utc(as_of, "as_of"))
        state = self._fold(asset_id, as_of_text)
        if not state["enrolled"]:
            raise NotFound("该时点之前资产尚未入账")
        return {
            "asset_id": asset_id,
            "as_of": as_of_text or self._now(),
            "owner": state["owner"],
            "custodian": state["custodian"],
            "operator": state["operator"],
            "custodial_location": state["location"],
            "basis_revision": state["revision"],
            "last_transfer_id": state["last_transfer_id"],
            "effective_at": state["last_event"],
            "restrictions": sorted(
                ({"restriction_id": rid, **{k: v for k, v in item.items() if k != "restriction_id"}}
                 for rid, item in state["restrictions"].items()),
                key=lambda item: item["restriction_id"],
            ),
        }

    def rights_chain(self, actor_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "title.read")
        asset = self.connection.execute(
            "SELECT * FROM title_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if asset is None:
            raise NotFound("资产不存在")
        rows = self.connection.execute(
            "SELECT event_id,event_type,payload_json,basis_revision,transfer_id,"
            "reversed_event_id,created_by,created_at FROM title_events "
            "WHERE asset_id=? ORDER BY event_id",
            (asset_id,),
        ).fetchall()
        return {
            "asset_id": asset_id,
            "current_revision": asset["current_revision"],
            "chain": [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "basis_revision": row["basis_revision"],
                    "transfer_id": row["transfer_id"],
                    "reversed_event_id": row["reversed_event_id"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ],
        }

    def transfer_detail(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "title.read")
        transfer = self._transfer(transfer_id)
        proposal = json.loads(transfer["proposal_json"])
        consents = self.connection.execute(
            "SELECT consent_id,condition_id,party_role,kind,outcome,basis_key,note,"
            "given_by,given_at FROM transfer_consents WHERE transfer_id=? ORDER BY consent_id",
            (transfer_id,),
        ).fetchall()
        frozen_basis_key = canonical_json(
            {item["asset_id"]: item["revision"] for item in proposal["assets"]}
        )
        current_basis_key = canonical_json(
            self._current_revisions({item["asset_id"]: item["revision"] for item in proposal["assets"]})
        )
        detail = {
            "transfer_id": transfer_id,
            "state": transfer["state"],
            "block_reason": transfer["block_reason"],
            "basis_version": transfer["basis_version"],
            "seller_party": transfer["seller_party"],
            "buyer_party": transfer["buyer_party"],
            "frozen_sha256": transfer["proposal_sha256"],
            "created_at": transfer["created_at"],
            "settled_at": transfer["settled_at"],
            "ended_at": transfer["ended_at"],
            "proposal": proposal,
            "consents": [
                {
                    **dict(row),
                    "valid_for_settlement": (
                        row["outcome"] == "cleared"
                        and row["basis_key"] == frozen_basis_key
                        and current_basis_key == frozen_basis_key
                    ),
                }
                for row in consents
            ],
        }
        if transfer["state"] == "proposed":
            detail["evaluation"] = {
                key: value for key, value in self._evaluation(transfer).items()
                if key != "consents"
            }
        return detail

    # ----- 审计哈希链 -----

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
