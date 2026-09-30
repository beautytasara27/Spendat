"""Vendor adapters and the factory that builds them from ``config/vendors.yaml``.

To add a vendor *type*, implement ``VendorAdapter`` and add one builder to
``ADAPTER_BUILDERS``. To add another merchant of an existing type, only edit the YAML.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from spenddat.models import RuntimeMode
from spenddat.secrets import SecretsProvider, require
from spenddat.vendors.acp import AcpAdapter
from spenddat.vendors.base import AdapterRegistry, VendorAdapter
from spenddat.vendors.stub import StubAdapter, StubProduct

Builder = Callable[[str, dict[str, Any], SecretsProvider, dict[str, str] | None], VendorAdapter]


def _build_acp(
    vid: str, cfg: dict[str, Any], secrets: SecretsProvider, buyer: dict[str, str] | None
) -> VendorAdapter:
    return AcpAdapter(
        vendor_id=vid,
        base_url=cfg["base_url"],
        feed_url=cfg["feed_url"],
        api_key=require(secrets, cfg["api_key_secret"]),
        seller_profile=cfg["seller_profile"],
        api_version=cfg.get("api_version", "2025-09-29"),
        buyer=buyer,
    )


def _build_stub(
    vid: str, cfg: dict[str, Any], _s: SecretsProvider, _b: dict[str, str] | None
) -> VendorAdapter:
    products = [
        StubProduct(p["sku"], p["title"], int(p["price_minor"])) for p in cfg.get("catalog", [])
    ]
    return StubAdapter(vid, products, shipping_minor=int(cfg.get("shipping_minor", 0)))


_REFERENCE_MERCHANTS: dict[str, Any] = {}


def _build_reference(
    vid: str, cfg: dict[str, Any], _s: SecretsProvider, buyer: dict[str, str] | None
) -> VendorAdapter:
    """A bundled sandbox ACP store running in-process (no network, no keys)."""
    import httpx

    from spenddat.sandbox.catalogs import CATALOGS
    from spenddat.sandbox.reference_merchant import ReferenceMerchant

    catalog = cfg.get("catalog", "everyday")
    if catalog not in CATALOGS:
        raise ValueError(f"unknown demo catalog {catalog!r}; choose from {sorted(CATALOGS)}")
    key = f"{vid}:{catalog}"
    if key not in _REFERENCE_MERCHANTS:  # keep carts alive across config reloads
        _REFERENCE_MERCHANTS[key] = ReferenceMerchant(api_key="demo_key", catalog=CATALOGS[catalog])
    base = f"https://{vid.replace('_', '-')}.demo.test"
    return AcpAdapter(
        vendor_id=vid,
        base_url=base,
        feed_url=f"{base}/feed.json",
        api_key="demo_key",
        seller_profile=f"profile_test_{vid}",
        buyer=buyer,
        client=httpx.Client(transport=_REFERENCE_MERCHANTS[key].mock_transport(base)),
    )


ADAPTER_BUILDERS: dict[str, Builder] = {
    "acp": _build_acp,
    "stub": _build_stub,
    "reference": _build_reference,
}
SANDBOX_ONLY_TYPES = {"stub", "reference"}


def build_adapter(
    spec: dict[str, Any], mode: RuntimeMode, secrets: SecretsProvider, buyer: dict[str, str] | None
) -> VendorAdapter:
    vtype = spec["type"]
    if vtype not in ADAPTER_BUILDERS:
        raise ValueError(f"unknown vendor type {vtype!r}")
    if vtype in SANDBOX_ONLY_TYPES and mode is RuntimeMode.LIVE:
        raise ValueError(f"{vtype} vendors cannot be used in live mode")
    adapter = ADAPTER_BUILDERS[vtype](spec["id"], spec[mode.value], secrets, buyer)
    adapter.display_name = spec.get("name") or None
    return adapter


__all__ = ["ADAPTER_BUILDERS", "AdapterRegistry", "VendorAdapter", "build_adapter"]
