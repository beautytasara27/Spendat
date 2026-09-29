"""Versioned, human-readable policy configuration.

Policies live in YAML (see ``policies/default.yaml``). This module validates them into
an immutable, typed object; the engine never reads files itself, which keeps it pure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VendorRules(_Strict):
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


class CategoryRules(_Strict):
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


class Caps(_Strict):
    per_transaction_minor: int = Field(gt=0)
    per_category_minor: dict[str, int] = Field(default_factory=dict)
    daily_minor: int = Field(gt=0)
    weekly_minor: int = Field(gt=0)


class Velocity(_Strict):
    max_purchases: int = Field(gt=0)
    window_minutes: int = Field(gt=0)


class Anomaly(_Strict):
    price_deviation_pct: float = Field(gt=0)
    min_history: int = Field(default=3, ge=1)
    history_days: int = Field(default=30, ge=1)
    velocity: Velocity


class Approval(_Strict):
    timeout_minutes: int = Field(default=60, gt=0)


class Policy(_Strict):
    version: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[a-z]{3}$")
    vendors: VendorRules
    categories: CategoryRules
    caps: Caps
    anomaly: Anomaly
    approval: Approval = Approval()
    content_hash: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> Policy:
        overlap_v = self.vendors.allow & self.vendors.deny
        overlap_c = self.categories.allow & self.categories.deny
        if overlap_v:
            raise ValueError(f"vendors in both allow and deny: {sorted(overlap_v)}")
        if overlap_c:
            raise ValueError(f"categories in both allow and deny: {sorted(overlap_c)}")
        if self.caps.daily_minor > self.caps.weekly_minor:
            raise ValueError("daily cap cannot exceed weekly cap")
        return self

    @property
    def full_version(self) -> str:
        return f"{self.version}#{self.content_hash[:12]}" if self.content_hash else self.version


def load_policy(path: str | Path) -> Policy:
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"policy file {path} must contain a mapping")
    data["content_hash"] = hashlib.sha256(raw).hexdigest()
    return Policy.model_validate(data)
