"""所有权流转服务的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RESTRICTION_KINDS = {"pledge", "recall"}
CONSENT_TYPES = {"buyer_accept", "financier_release", "quality_confirm"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 512) -> str:
    if value is None:
        return ""
    return required_text(value, field, maximum)


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
class AssetRegistration:
    asset_id: str
    description: str
    owner_party_id: str
    custodian_party_id: str
    operator_party_id: str
    location: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssetRegistration":
        return cls(
            asset_id=identifier(raw.get("asset_id"), "asset_id"),
            description=required_text(raw.get("description"), "description"),
            owner_party_id=identifier(raw.get("owner_party_id"), "owner_party_id"),
            custodian_party_id=identifier(raw.get("custodian_party_id"), "custodian_party_id"),
            operator_party_id=identifier(raw.get("operator_party_id"), "operator_party_id"),
            location=required_text(raw.get("location"), "location"),
        )


@dataclass(frozen=True, slots=True)
class RestrictionInput:
    restriction_id: str
    asset_id: str
    kind: str
    holder_party_id: str | None
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RestrictionInput":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in RESTRICTION_KINDS:
            raise ValidationFailed("kind 必须是 pledge 或 recall")
        holder = optional_identifier(raw.get("holder_party_id"), "holder_party_id")
        if kind == "pledge" and holder is None:
            raise ValidationFailed("质押限制必须指明持有方 holder_party_id")
        if kind == "recall" and holder is not None:
            raise ValidationFailed("召回限制不接受 holder_party_id")
        return cls(
            restriction_id=identifier(raw.get("restriction_id"), "restriction_id"),
            asset_id=identifier(raw.get("asset_id"), "asset_id"),
            kind=kind,
            holder_party_id=holder,
            reason=required_text(raw.get("reason"), "reason", 512),
        )


@dataclass(frozen=True, slots=True)
class TransferItemInput:
    asset_id: str
    target_custodian_party_id: str | None
    target_operator_party_id: str | None
    target_location: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransferItemInput":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("资产清单条目必须是对象")
        location = raw.get("target_location")
        return cls(
            asset_id=identifier(raw.get("asset_id"), "items.asset_id"),
            target_custodian_party_id=optional_identifier(
                raw.get("target_custodian_party_id"), "items.target_custodian_party_id"
            ),
            target_operator_party_id=optional_identifier(
                raw.get("target_operator_party_id"), "items.target_operator_party_id"
            ),
            target_location=None if location is None else required_text(location, "items.target_location"),
        )


@dataclass(frozen=True, slots=True)
class TransferInitiation:
    transfer_id: str
    seller_party_id: str
    buyer_party_id: str
    items: tuple[TransferItemInput, ...]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransferInitiation":
        seller = identifier(raw.get("seller_party_id"), "seller_party_id")
        buyer = identifier(raw.get("buyer_party_id"), "buyer_party_id")
        if seller == buyer:
            raise ValidationFailed("卖方和买方不能是同一参与方")
        items_raw = raw.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            raise ValidationFailed("items 必须是非空资产清单")
        items = tuple(TransferItemInput.from_dict(item) for item in items_raw)
        asset_ids = [item.asset_id for item in items]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValidationFailed("资产清单包含重复资产")
        return cls(
            transfer_id=identifier(raw.get("transfer_id"), "transfer_id"),
            seller_party_id=seller,
            buyer_party_id=buyer,
            items=items,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ConsentInput:
    consent_type: str
    restriction_id: str | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConsentInput":
        consent_type = required_text(raw.get("consent_type"), "consent_type", 32)
        if consent_type not in CONSENT_TYPES:
            raise ValidationFailed("consent_type 必须是 buyer_accept、financier_release 或 quality_confirm")
        restriction_id = optional_identifier(raw.get("restriction_id"), "restriction_id")
        if consent_type == "buyer_accept" and restriction_id is not None:
            raise ValidationFailed("买方接受不需要 restriction_id")
        if consent_type != "buyer_accept" and restriction_id is None:
            raise ValidationFailed("融资方解押和质量确认必须指明 restriction_id")
        return cls(
            consent_type=consent_type,
            restriction_id=restriction_id,
            note=optional_text(raw.get("note"), "note"),
        )

    @property
    def scope(self) -> str:
        if self.consent_type == "buyer_accept":
            return "buyer"
        return f"restriction:{self.restriction_id}"
