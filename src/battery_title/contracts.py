"""权属流转领域输入契约。

一笔转让（transfer）由卖方发起，发起时冻结：
- 资产清单 assets；
- 各方权利声明 claims（谁将成为所有者、保管人、运维责任人）；
- 保管位置 custodial_location；
- 资产上的关联限制 restrictions（质押、召回等）以及解押/质量确认的前置条件 conditions。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 角色轴：法律所有、现场保管、运维责任可以分属不同主体。
CLAIM_KINDS = ("owner", "custodian", "operator")
# 关联限制类型。
RESTRICTION_KINDS = ("pledge", "recall", "lien", "lock", "other")
# 前置条件参与方。
CONDITION_PARTIES = ("buyer", "financier", "quality", "seller")
# 条件确认给出的处置意见：放行 / 拒绝（召回确认不合格等）。
OUTCOMES = ("cleared", "rejected")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return identifier(value, field)


@dataclass(frozen=True, slots=True)
class AssetItem:
    asset_id: str
    revision: int  # 资产依据版本（证据/台账版本），变化即作废旧签
    evidence_sha256: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "AssetItem":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        revision = raw.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValidationFailed(f"{field}.revision 必须是正整数")
        digest = raw.get("evidence_sha256")
        if digest is not None:
            digest = required_text(digest, f"{field}.evidence_sha256", 64).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValidationFailed(f"{field}.evidence_sha256 必须是 64 位十六进制摘要")
        return cls(identifier(raw.get("asset_id"), f"{field}.asset_id"), revision, digest)


@dataclass(frozen=True, slots=True)
class Claim:
    kind: str  # owner / custodian / operator
    current_party: str | None  # 发起时的当前权利人（事实核对用，可为空表示未知）
    next_party: str  # 交割生效后的权利人

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "Claim":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        kind = required_text(raw.get("kind"), f"{field}.kind", 16)
        if kind not in CLAIM_KINDS:
            raise ValidationFailed(f"{field}.kind 必须是 {', '.join(CLAIM_KINDS)} 之一")
        current = optional_identifier(raw.get("current_party"), f"{field}.current_party")
        next_party = identifier(raw.get("next_party"), f"{field}.next_party")
        # 允许某一权利轴在交割后保持不变（如仅售所有权、保管不转移）。
        return cls(kind, current, next_party)


@dataclass(frozen=True, slots=True)
class Restriction:
    restriction_id: str
    kind: str  # pledge / recall / lien / lock / other
    holder_party: str | None  # 限制权利人，如融资方
    status: str  # active / released（发起时的现状）
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "Restriction":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        kind = required_text(raw.get("kind"), f"{field}.kind", 16)
        if kind not in RESTRICTION_KINDS:
            raise ValidationFailed(f"{field}.kind 必须是 {', '.join(RESTRICTION_KINDS)} 之一")
        status = required_text(raw.get("status", "active"), f"{field}.status", 16)
        if status not in ("active", "released"):
            raise ValidationFailed(f"{field}.status 必须是 active 或 released")
        return cls(
            identifier(raw.get("restriction_id"), f"{field}.restriction_id"),
            kind,
            optional_identifier(raw.get("holder_party"), f"{field}.holder_party"),
            status,
            required_text(raw.get("note", ""), f"{field}.note", 512),
        )


@dataclass(frozen=True, slots=True)
class Condition:
    """交割前置条件：某参与方必须就某限制给出确认。"""

    condition_id: str
    party: str  # buyer / financier / quality / seller
    kind: str  # acceptance（买方接受）/ release（解押）/ quality_clearance（质量或召回确认）
    restriction_id: str | None  # 关联的限制；买方接受可以不关联
    required: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "Condition":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        party = required_text(raw.get("party"), f"{field}.party", 16)
        if party not in CONDITION_PARTIES:
            raise ValidationFailed(f"{field}.party 必须是 {', '.join(CONDITION_PARTIES)} 之一")
        kind = required_text(raw.get("kind"), f"{field}.kind", 32)
        if kind not in ("acceptance", "release", "quality_clearance"):
            raise ValidationFailed(f"{field}.kind 必须是 acceptance、release 或 quality_clearance")
        if kind == "acceptance" and party != "buyer":
            raise ValidationFailed("acceptance 条件只能由 buyer 完成")
        if kind == "acceptance":
            raise ValidationFailed("买方接受由系统自动汇集，无需在 conditions 中声明")
        if kind == "release" and party != "financier":
            raise ValidationFailed("release 条件只能由 financier 完成")
        if kind == "quality_clearance" and party != "quality":
            raise ValidationFailed("quality_clearance 条件只能由 quality 完成")
        restriction_ref = optional_identifier(raw.get("restriction_id"), f"{field}.restriction_id")
        if restriction_ref is None:
            raise ValidationFailed(f"{field}.restriction_id 对 {kind} 为必填")
        return cls(
            identifier(raw.get("condition_id"), f"{field}.condition_id"),
            party,
            kind,
            restriction_ref,
            bool(raw.get("required", True)),
        )


@dataclass(frozen=True, slots=True)
class TransferProposal:
    transfer_id: str
    seller_party: str
    buyer_party: str
    assets: tuple[AssetItem, ...]
    claims: tuple[Claim, ...]
    custodial_location: str
    restrictions: tuple[Restriction, ...]
    conditions: tuple[Condition, ...]
    basis_version: str  # 卖方发起所依据的外部台账/规则版本
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransferProposal":
        assets_raw = raw.get("assets")
        if not isinstance(assets_raw, list) or not assets_raw:
            raise ValidationFailed("assets 必须是非空数组")
        assets = tuple(AssetItem.from_dict(item, "assets") for item in assets_raw)
        asset_ids = [item.asset_id for item in assets]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValidationFailed("资产清单中存在重复资产")

        claims_raw = raw.get("claims")
        if not isinstance(claims_raw, list) or not claims_raw:
            raise ValidationFailed("claims 必须是非空数组")
        claims = tuple(Claim.from_dict(item, "claims") for item in claims_raw)
        if len({claim.kind for claim in claims}) != len(claims):
            raise ValidationFailed("同一权利轴不能声明两次")
        if not any(claim.kind == "owner" for claim in claims):
            raise ValidationFailed("权利声明必须包含 owner 权利轴")

        restrictions_raw = raw.get("restrictions", [])
        if not isinstance(restrictions_raw, list):
            raise ValidationFailed("restrictions 必须是数组")
        restrictions = tuple(
            Restriction.from_dict(item, "restrictions") for item in restrictions_raw
        )
        restriction_ids = {item.restriction_id for item in restrictions}
        if len(restriction_ids) != len(restrictions):
            raise ValidationFailed("关联限制编号重复")

        conditions_raw = raw.get("conditions", [])
        if not isinstance(conditions_raw, list):
            raise ValidationFailed("conditions 必须是数组")
        conditions = tuple(
            Condition.from_dict(item, "conditions") for item in conditions_raw
        )
        condition_ids = [item.condition_id for item in conditions]
        if len(set(condition_ids)) != len(condition_ids):
            raise ValidationFailed("前置条件编号重复")
        # 条件引用的限制是否真实生效，由服务层对照台账冻结快照核对。

        seller = identifier(raw.get("seller_party"), "seller_party")
        buyer = identifier(raw.get("buyer_party"), "buyer_party")
        if seller == buyer:
            raise ValidationFailed("买卖双方不能是同一主体")
        if not any(claim.kind == "owner" and claim.next_party == buyer for claim in claims):
            raise ValidationFailed("owner 权利轴的受让方必须是买方")
        return cls(
            transfer_id=identifier(raw.get("transfer_id"), "transfer_id"),
            seller_party=seller,
            buyer_party=buyer,
            assets=assets,
            claims=claims,
            custodial_location=required_text(raw.get("custodial_location"), "custodial_location", 256),
            restrictions=restrictions,
            conditions=conditions,
            basis_version=required_text(raw.get("basis_version"), "basis_version", 64),
            note=required_text(raw.get("note", ""), "note", 512),
        )
