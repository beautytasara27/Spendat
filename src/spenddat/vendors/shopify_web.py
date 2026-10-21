"""Shopify stores, through the agent tools Shopify publishes in the browser (WebMCP).

Every Shopify online store exposes storefront tools (``search_catalog``, ``get_product``,
``update_cart``, ``proceed_to_checkout``, ...) and, on eligible checkouts, checkout tools
(``get_checkout``, ``update_checkout``, ``complete_checkout``) to AI agents running in
the shopper's browser. Shopify explicitly permits this; this adapter uses only those
tools - no scraping, no clicking.

Rules Shopify sets, and how Spenddat follows them:

* **The buyer must see the order and total and give permission before every purchase.**
  This adapter declares ``requires_buyer_confirmation``, so the policy engine always
  escalates Shopify purchases to you (they can't be set to Auto-buy), and the checkout
  total is re-checked against what you approved right before ``complete_checkout``.
  Shopify may additionally show a review step on the page - you approve it there.
* **Payment** uses a card saved in your Shop Pay account (sign in once with
  ``spenddat browser login``). Spenddat never sees the card; it only picks the saved
  instrument by id. No Stripe token is involved.
* **Agent identity** - Shopify asks agents to sign requests with Web Bot Auth; see
  ``spenddat.browser.webbotauth``.

The checkout tools are documented by Shopify; the storefront tools' argument formats are
not published, so arguments are built from each tool's own ``inputSchema`` at runtime
(``spenddat browser inspect <store>`` saves those schemas for review).
"""

from __future__ import annotations

import contextlib
import re
import time
from typing import Any
from urllib.parse import urlparse

from spenddat.browser.session import (
    BrowserSession,
    BrowserUnavailable,
    ToolCallError,
    call_tool,
    list_tools,
    wait_for_tool,
)
from spenddat.models import Address, Candidate, OrderStatus, Receipt, ResolvedItem
from spenddat.observability import get_logger, log
from spenddat.vendors.acp import _tokens
from spenddat.vendors.base import PaymentHandle, VendorAdapter, VendorError, VendorOutcomeUnknown

_log = get_logger("vendors.shopify_web")

# ---------------------------------------------------------------------------- result parsing


def unwrap(res: Any) -> Any:
    """Accept MCP-style ``{"content":[{"type":"text","text":"..."}]}`` wrappers too."""
    import json

    if isinstance(res, dict) and isinstance(res.get("content"), list) and len(res) <= 3:
        texts = [c.get("text") for c in res["content"] if isinstance(c, dict) and c.get("text")]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except ValueError:
                return texts[0]
    if isinstance(res, dict) and "structuredContent" in res:
        return res["structuredContent"]
    return res


def tool_errors(res: Any) -> list[tuple[str, str]]:
    """UCP errors: ``ucp.status == "error"`` with messages[{type:"error",code,content}]."""
    if not isinstance(res, dict):
        return []
    out = []
    status = (res.get("ucp") or {}).get("status")
    for m in res.get("messages") or res.get("errors") or []:
        if isinstance(m, dict) and (m.get("type") == "error" or status == "error"):
            out.append((str(m.get("code") or "error"), str(m.get("content") or m.get("message"))))
    if status == "error" and not out:
        out.append(("error", str(res.get("message") or "tool returned an error")))
    if res.get("isError"):
        out.append(("error", str(res.get("message") or res)))
    return out


def money_minor(v: Any) -> int | None:
    """Shopify money comes as decimal strings/{amount,currencyCode} (dollars) or as
    integer cents (Liquid/AJAX and UCP). Ints are treated as minor units."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        for k in ("amount", "value", "price"):
            if k in v:
                return money_minor(v[k])
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v * 100)
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", str(v).replace(",", ""))
    if not m:
        return None
    whole, _, frac = m.group(1).replace(",", ".").partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _opt(d: dict, names: tuple[str, ...]) -> str | None:
    for so in d.get("selectedOptions") or d.get("selected_options") or []:
        if isinstance(so, dict) and str(so.get("name", "")).lower() in names:
            return str(so.get("value"))
    opts = d.get("options")
    if isinstance(opts, dict):
        for k, v in opts.items():
            if k.lower() in names:
                return str(v)
    return None


def _variant_fields(v: dict) -> tuple[str | None, str | None]:
    size = _opt(v, ("size", "shoe size", "us size"))
    color = _opt(v, ("color", "colour"))
    if (size is None or color is None) and isinstance(v.get("title"), str) and " / " in v["title"]:
        for part in v["title"].split(" / "):
            p = part.strip()
            if size is None and re.fullmatch(r"(\d{1,2}(\.5)?|xxs|xs|s|m|l|xl|xxl|xxxl)", p, re.I):
                size = p
            elif color is None and not re.search(r"\d", p):
                color = p
    return size, color


def _variants(p: dict) -> list[dict]:
    vs = p.get("variants")
    if isinstance(vs, dict):  # Storefront API connection: {"nodes": [...]} / {"edges": [...]}
        vs = vs.get("nodes") or [e.get("node") for e in vs.get("edges", []) if isinstance(e, dict)]
    return [v for v in (vs or []) if isinstance(v, dict)]


def find_products(obj: Any, out: list[dict] | None = None) -> list[dict]:
    """Walk any JSON and collect product-like objects (title + price or variants)."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        is_product = (
            isinstance(obj.get("title"), str)
            and (
                _variants(obj)
                or any(k in obj for k in ("price", "price_range", "priceRange", "min_price"))
            )
            and not obj.get("selectedOptions")
        )
        if is_product:
            out.append(obj)
            return out
        for v in obj.values():
            find_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_products(v, out)
    return out


def _product_price(p: dict) -> int | None:
    for k in ("price", "min_price", "price_min"):
        if k in p:
            return money_minor(p[k])
    pr = p.get("price_range") or p.get("priceRange") or {}
    if isinstance(pr, dict):
        return money_minor(_first(pr, "min", "minVariantPrice", "min_variant_price"))
    return None


def _available(d: dict) -> bool:
    v = _first(d, "available", "availableForSale", "available_for_sale", "in_stock")
    if v is None:
        av = d.get("availability")
        return av is None or str(av).lower() in ("in_stock", "instock", "available", "true")
    return bool(v)


def products_to_candidates(
    vendor: str, query: str, products: list[dict], currency: str = "usd"
) -> list[Candidate]:
    q = _tokens(query)
    out: list[Candidate] = []
    for p in products:
        title = str(p.get("title"))
        text = (
            f"{title} {p.get('description', '')} {p.get('product_type', '')} {p.get('vendor', '')}"
        )
        score = round(len(q & _tokens(text)) / len(q), 3) if q else 0.0
        pid = str(_first(p, "id", "product_id", "handle") or title)
        url = _first(p, "url", "onlineStoreUrl", "online_store_url")
        variants = _variants(p)
        if not variants:
            price = _product_price(p)
            vid = _first(p, "variant_id", "default_variant_id")
            if price is None:
                continue
            out.append(
                Candidate(
                    vendor=vendor,
                    sku=str(vid or pid),
                    title=title,
                    unit_price_minor=price,
                    currency=currency,
                    in_stock=_available(p),
                    url=url,
                    score=score,
                    group_id=pid,
                )
            )
            continue
        for v in variants:
            vid = _first(v, "id", "variant_id")
            price = money_minor(_first(v, "price", "priceV2", "amount"))
            if vid is None or price is None:
                continue
            size, color = _variant_fields(v)
            out.append(
                Candidate(
                    vendor=vendor,
                    sku=str(vid),
                    title=title,
                    unit_price_minor=price,
                    currency=currency,
                    in_stock=_available(v),
                    url=url,
                    score=score,
                    group_id=pid,
                    size=size,
                    color=color,
                )
            )
    return out


# ---------------------------------------------------------------------------- schema mapping

_QUERY_NAMES = ("query", "q", "search", "search_query", "searchquery", "term", "keywords", "text")
_VARIANT_NAMES = (
    "variant_id",
    "variantid",
    "merchandise_id",
    "merchandiseid",
    "product_variant_id",
    "productvariantid",
    "id",
)
_QTY_NAMES = ("quantity", "qty", "count")
_PRODUCT_NAMES = ("product_id", "productid", "id", "handle", "product_handle", "product")


def _props(schema: dict) -> dict[str, dict]:
    return schema.get("properties") or {}


def _match(props: dict[str, dict], names: tuple[str, ...]) -> str | None:
    norm = {k.lower().replace("-", "_"): k for k in props}
    for n in names:
        if n in norm:
            return norm[n]
    return None


class SchemaMismatch(VendorError):
    pass


def args_for_search(schema: dict, query: str) -> dict:
    props = _props(schema)
    key = _match(props, _QUERY_NAMES)
    if key is None:
        strings = [k for k, v in props.items() if v.get("type") == "string"]
        if len(strings) == 1:
            key = strings[0]
    if key is None:
        raise SchemaMismatch(f"can't find the search field in {sorted(props)}")
    args: dict[str, Any] = {key: query}
    lim = _match(props, ("limit", "first", "count", "max_results"))
    if lim and props[lim].get("type") in ("integer", "number"):
        args[lim] = 10
    return args


def args_for_product(schema: dict, product: dict) -> dict:
    props = _props(schema)
    key = _match(props, _PRODUCT_NAMES)
    if key is None:
        raise SchemaMismatch(f"can't find the product field in {sorted(props)}")
    want = key.lower()
    val = product.get("handle") if "handle" in want else _first(product, "id", "product_id")
    return {key: val if val is not None else product.get("handle")}


def args_for_add(schema: dict, variant_id: str, quantity: int) -> dict:
    """update_cart: find either a list of line objects or flat variant/quantity fields."""
    props = _props(schema)
    for name, spec in props.items():
        items = spec.get("items") if spec.get("type") == "array" else None
        if isinstance(items, dict):
            iprops = _props(items)
            vk, qk = _match(iprops, _VARIANT_NAMES), _match(iprops, _QTY_NAMES)
            if vk and qk and not re.search(r"remove|delete", name, re.I):
                line = {vk: variant_id, qk: quantity}
                return {name: [line]}
    vk, qk = _match(props, _VARIANT_NAMES), _match(props, _QTY_NAMES)
    if vk:
        out: dict[str, Any] = {vk: variant_id}
        if qk:
            out[qk] = quantity
        act = _match(props, ("action", "operation", "op"))
        if act:
            enum = [str(e) for e in props[act].get("enum", [])]
            out[act] = next((e for e in enum if e.lower() in ("add", "add_to_cart")), "add")
        return out
    raise SchemaMismatch(f"can't see how to add an item; update_cart fields: {sorted(props)}")


# ---------------------------------------------------------------------------- the adapter


class ShopifyWebAdapter(VendorAdapter):
    integration = "browser"
    payment_kind = "merchant_wallet"  # Shop Pay saved card, not a Stripe token
    requires_buyer_confirmation = True

    def __init__(
        self,
        vendor_id: str,
        store_url: str,
        session: BrowserSession,
        buyer: dict[str, str] | None = None,
        escalation_timeout_s: float = 600.0,
        poll_s: float = 2.0,
        dry_run: bool = False,
    ) -> None:
        u = urlparse(store_url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise ValueError(f"not a store URL: {store_url!r}")
        self.vendor_id = vendor_id
        self.store_url = f"{u.scheme}://{u.netloc}"
        self.session = session
        self.buyer = buyer or {}
        self.escalation_timeout_s = escalation_timeout_s
        self.poll_s = poll_s
        # Sandbox mode with a real store: do everything except placing the order.
        self.dry_run = dry_run
        self._receipts: dict[str, Receipt] = {}
        self._payment_label: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers (worker)

    @property
    def _store_tab(self) -> str:
        return f"store:{self.vendor_id}"

    def _call(self, page: Any, name: str, args: dict | None = None) -> Any:
        try:
            res = unwrap(call_tool(page, name, args))
        except ToolCallError as e:
            raise VendorError(str(e)) from e
        return res

    def _schema(self, page: Any, name: str) -> dict:
        for t in list_tools(page):
            if t["name"] == name:
                return t["inputSchema"] or {}
        raise VendorError(f"{name} is not offered by {self.store_url}")

    def _open_store(self, s: BrowserSession) -> Any:
        try:
            p = s.pages.get(self._store_tab)
            if p is None or p.is_closed() or not p.url.startswith(self.store_url):
                p = s.page(self._store_tab, self.store_url)
            else:
                wait_for_tool(p, "search_catalog", 10_000)
            return p
        except BrowserUnavailable as e:
            raise VendorError(str(e)) from e
        except ToolCallError as e:
            raise VendorError(f"{self.store_url}: {e}") from e

    # ------------------------------------------------------------------ interface

    def health_check(self) -> dict[str, Any]:
        def job(s: BrowserSession) -> dict[str, Any]:
            p = self._open_store(s)
            names = sorted(t["name"] for t in list_tools(p))
            return {"ok": "search_catalog" in names, "tools": len(names)}

        return self.session.run(job, timeout=90)

    def search(self, query: str, limit: int = 5) -> list[Candidate]:
        def job(s: BrowserSession) -> list[Candidate]:
            p = self._open_store(s)
            res = self._call(
                p, "search_catalog", args_for_search(self._schema(p, "search_catalog"), query)
            )
            if tool_errors(res):
                raise VendorError(f"search failed: {tool_errors(res)}")
            products = find_products(res)
            # Search results may omit variants; fetch details for the best few.
            if products and not any(_variants(x) for x in products):
                detailed = []
                schema = self._schema(p, "get_product")
                ranked = sorted(
                    products, key=lambda x: -len(_tokens(query) & _tokens(str(x.get("title"))))
                )
                for prod in ranked[:3]:
                    try:
                        d = self._call(p, "get_product", args_for_product(schema, prod))
                        detailed += find_products(d) or [prod]
                    except VendorError:
                        detailed.append(prod)
                products = detailed
            cands = [
                c for c in products_to_candidates(self.vendor_id, query, products) if c.in_stock
            ]
            cands = [c for c in cands if c.score >= 0.5]
            cands.sort(key=lambda c: (-c.score, c.unit_price_minor))
            log(_log, 20, "search", store=self.store_url, results=len(cands))
            return cands[: max(limit, 1)]

        return self.session.run(job, timeout=120)

    def resolve(self, candidate: Candidate, quantity: int, ship_to: Address) -> ResolvedItem:
        def job(s: BrowserSession) -> ResolvedItem:
            p = self._open_store(s)
            with contextlib.suppress(VendorError):  # empty cart or tool missing: fine
                self._call(p, "cancel_cart", {})
            res = self._call(
                p,
                "update_cart",
                args_for_add(self._schema(p, "update_cart"), candidate.sku, quantity),
            )
            if tool_errors(res):
                raise VendorError(f"couldn't add to cart: {tool_errors(res)}")
            self._call(p, "proceed_to_checkout", {})
            try:
                wait_for_tool(p, "get_checkout", 30_000)
            except ToolCallError as e:
                raise VendorError(
                    "this store's checkout doesn't offer agent tools; it would need you to "
                    "finish on the page, so Spenddat won't buy here"
                ) from e
            co = self._checkout_details(p, ship_to)
            ref = str(co.get("id") or f"co_{int(time.time() * 1000)}")
            s.adopt(self._store_tab, f"checkout:{ref}")
            return self._to_item(candidate, quantity, co, ref)

        return self.session.run(job, timeout=180)

    def purchase(
        self, item: ResolvedItem, payment: PaymentHandle, ship_to: Address, idempotency_key: str
    ) -> Receipt:
        def job(s: BrowserSession) -> Receipt:
            if item.vendor_ref in self._receipts:  # idempotent
                return self._receipts[item.vendor_ref]
            p = s.pages.get(f"checkout:{item.vendor_ref}")
            if p is None or p.is_closed():
                raise VendorError("the checkout tab was closed; nothing was bought")
            co = self._get(p)
            total = _total(co, "total")
            if total is None or total > payment.max_amount_minor:
                raise VendorError(
                    f"checkout total is now {_fmt(total)}, above the "
                    f"{_fmt(payment.max_amount_minor)} you approved; not buying"
                )
            if co.get("status") not in ("ready_for_complete", "requires_escalation"):
                raise VendorError(_not_ready_reason(co))
            if self.dry_run:
                pay = self._payment_label.get(item.vendor_ref) or "your saved card"
                raise VendorError(
                    f"dry run (sandbox mode): the checkout was ready - {_fmt(total)} with "
                    f"{pay} - but the order was NOT placed. Switch to live mode to buy for real."
                )
            p.bring_to_front()
            return self._complete(p, item, co)

        return self.session.run(job, timeout=self.escalation_timeout_s + 120)

    def order_status(self, vendor_ref: str) -> tuple[OrderStatus, Receipt | None]:
        if vendor_ref in self._receipts:
            return OrderStatus.COMPLETED, self._receipts[vendor_ref]

        def job(s: BrowserSession) -> tuple[OrderStatus, Receipt | None]:
            p = s.pages.get(f"checkout:{vendor_ref}")
            if p is None or p.is_closed():
                return OrderStatus.UNKNOWN, None
            try:
                co = self._get(p)
            except VendorError:
                return OrderStatus.UNKNOWN, None
            if co.get("status") == "completed" or co.get("order"):
                r = self._receipt(co, vendor_ref)
                self._receipts[vendor_ref] = r
                return OrderStatus.COMPLETED, r
            if co.get("status") in ("ready_for_complete", "incomplete"):
                return OrderStatus.NOT_COMPLETED, None
            return OrderStatus.UNKNOWN, None

        return self.session.run(job, timeout=60)

    def cancel(self, vendor_ref: str) -> None:
        def job(s: BrowserSession) -> None:
            label = f"checkout:{vendor_ref}"
            p = s.pages.get(label)
            if p is None or p.is_closed():
                return
            try:
                self._call(p, "navigate_to_storefront", {})
                wait_for_tool(p, "cancel_cart", 15_000)
                self._call(p, "cancel_cart", {})
            except (VendorError, ToolCallError):
                log(_log, 30, "cancel_failed", vendor_ref=vendor_ref)
            finally:
                s.close_page(label)

        try:
            self.session.run(job, timeout=60)
        except Exception:
            log(_log, 30, "cancel_failed", vendor_ref=vendor_ref)

    # ------------------------------------------------------------------ checkout (worker)

    def _get(self, p: Any) -> dict:
        co = self._call(p, "get_checkout", {})
        if not isinstance(co, dict):
            raise VendorError(f"unexpected get_checkout result {co!r}")
        return co

    def _update(self, p: Any, checkout: dict) -> dict:
        res = self._call(p, "update_checkout", {"checkout": checkout})
        errs = tool_errors(res)
        if errs:
            codes = {c for c, _ in errs}
            if codes & {"update_failed", "internal_error", "checkout_busy"}:
                p.wait_for_timeout(1500)
            else:
                raise VendorError(f"checkout update refused: {errs}")
        return self._get(p)

    def _checkout_details(self, p: Any, ship_to: Address) -> dict:
        co = self._get(p)
        desired: dict[str, Any] = {}
        buyer = dict(co.get("buyer") or {})
        if self.buyer.get("email") and not buyer.get("email"):
            buyer["email"] = self.buyer["email"]
        if buyer:
            desired["buyer"] = {k: v for k, v in buyer.items() if k in ("email", "phone_number")}
        first, _, last = ship_to.name.partition(" ")
        dest = {
            "first_name": first,
            "last_name": last or first,
            "street_address": ship_to.line_one,
            "address_locality": ship_to.city,
            "address_region": ship_to.state,
            "postal_code": ship_to.postal_code,
            "address_country": ship_to.country,
        }
        if ship_to.line_two:
            dest["extended_address"] = ship_to.line_two
        desired["fulfillment"] = {"methods": [{"type": "shipping", "destinations": [dest]}]}
        instruments = [i for i in (co.get("payment") or {}).get("instruments") or [] if i.get("id")]
        chosen = next((i for i in instruments if i.get("selected")), None) or (
            instruments[0] if instruments else None
        )
        if chosen:
            desired["payment"] = {"instruments": [{"id": chosen["id"]}]}
        co = self._update(p, desired)

        # cheapest shipping option for each group
        methods = (co.get("fulfillment") or {}).get("methods") or []
        if methods:
            m0 = methods[0]
            groups_out = []
            for g in m0.get("groups") or []:
                opts = [o for o in g.get("options") or [] if o.get("id")]
                if opts:
                    best = min(
                        opts, key=lambda o: money_minor(_first(o, "total", "amount", "price")) or 0
                    )
                    groups_out.append({"id": g.get("id"), "selected_option_id": best["id"]})
            if groups_out:
                m_desired = {"type": "shipping", "destinations": [dest], "groups": groups_out}
                dest_ids = [d.get("id") for d in m0.get("destinations") or [] if d.get("id")]
                if dest_ids:
                    m_desired["selected_destination_id"] = dest_ids[0]
                desired["fulfillment"] = {"methods": [m_desired]}
                co = self._update(p, desired)

        pay = (co.get("payment") or {}).get("instruments") or []
        sel = next((i for i in pay if i.get("selected")), None)
        if sel and isinstance(sel.get("display"), dict):
            d = sel["display"]
            self._payment_label[str(co.get("id"))] = (
                f"{d.get('brand', 'card')} ending {d.get('last_digits', '????')}"
            )
        return co

    def _to_item(self, cand: Candidate, quantity: int, co: dict, ref: str) -> ResolvedItem:
        lines = co.get("line_items") or []
        if len(lines) != 1 or int(lines[0].get("quantity") or 0) != quantity:
            raise VendorError(
                f"the store's checkout doesn't match the request ({len(lines)} line(s)); not buying"
            )
        total = _total(co, "total")
        subtotal = _total(co, "subtotal") or _total(co, "items_base_amount")
        if total is None:
            raise VendorError("the checkout didn't return a total")
        unit = (subtotal // quantity) if subtotal else cand.unit_price_minor
        return ResolvedItem(
            vendor=self.vendor_id,
            sku=cand.sku,
            title=cand.title,
            quantity=quantity,
            unit_price_minor=unit,
            shipping_minor=_total(co, "fulfillment") or _total(co, "shipping") or 0,
            tax_minor=_total(co, "tax") or 0,
            total_minor=total,
            currency=str(co.get("currency") or "usd").lower(),
            vendor_ref=ref,
            merchant_payment_id=None,
        )

    def _complete(self, p: Any, item: ResolvedItem, co: dict) -> Receipt:
        ref = item.vendor_ref
        called_again = False
        res = self._call(p, "complete_checkout", {})
        deadline = time.monotonic() + self.escalation_timeout_s
        while True:
            if res is None:  # page navigated (e.g. to the Thank you page)
                p.wait_for_timeout(1000)
                res = self._safe_get(p)
            errs = tool_errors(res) if isinstance(res, dict) else []
            codes = {c for c, _ in errs}
            if codes & {"invalid_request", "rejected", "payment_instrument_unusable"}:
                raise VendorError(f"the store refused the order: {errs}")
            if "completion_failed" in codes:
                raise VendorOutcomeUnknown(f"completion failed; outcome unknown: {errs}")
            status = (res or {}).get("status") if isinstance(res, dict) else None
            if status == "completed" or (isinstance(res, dict) and res.get("order")):
                r = self._receipt(res if res.get("order") else self._get(p), ref)
                self._receipts[ref] = r
                return r
            if time.monotonic() > deadline:
                raise VendorOutcomeUnknown(
                    "waited for you to approve the order on the Shopify page; no confirmation yet"
                )
            if status == "requires_escalation" or codes & {"buyer_action_required"}:
                log(_log, 20, "awaiting_buyer_review", vendor_ref=ref)
            p.wait_for_timeout(int(self.poll_s * 1000))
            cur = self._safe_get(p)
            # After the buyer approves the review on the page, the checkout returns to
            # ready_for_complete; call complete once more (Shopify's documented flow).
            if (
                isinstance(cur, dict)
                and cur.get("status") == "ready_for_complete"
                and status in ("requires_escalation", None)
                and not called_again
                and not _has_review_message(cur)
            ):
                called_again = True
                total = _total(cur, "total")
                if total is None or total > item.total_minor:
                    raise VendorError("the total changed during review; not buying")
                res = self._call(p, "complete_checkout", {})
            else:
                res = cur

    def _safe_get(self, p: Any) -> Any:
        try:
            wait_for_tool(p, "get_checkout", 15_000)
            return self._get(p)
        except (VendorError, ToolCallError):
            return None

    def _receipt(self, co: dict, ref: str) -> Receipt:
        order = co.get("order") or {}
        return Receipt(
            vendor=self.vendor_id,
            order_id=str(order.get("label") or order.get("id") or ref),
            vendor_ref=ref,
            amount_minor=_total(co, "total") or 0,
            currency=str(co.get("currency") or "usd").lower(),
            permalink_url=order.get("permalink_url"),
        )

    def payment_label(self, vendor_ref: str) -> str | None:
        return self._payment_label.get(vendor_ref)


def _total(co: dict | None, kind: str) -> int | None:
    for t in (co or {}).get("totals") or []:
        if t.get("type") == kind:
            return money_minor(t.get("amount"))
    return None


def _fmt(minor: int | None) -> str:
    return "unknown" if minor is None else f"${minor / 100:,.2f}"


def _has_review_message(co: dict) -> bool:
    return any(
        isinstance(m, dict) and "review" in str(m.get("code", "")) + str(m.get("severity", ""))
        for m in co.get("messages") or []
    )


def _not_ready_reason(co: dict) -> str:
    msgs = [
        str(m.get("content") or m.get("code"))
        for m in co.get("messages") or []
        if isinstance(m, dict)
    ]
    pay = (co.get("payment") or {}).get("instruments") or []
    if not pay:
        return (
            "no saved Shop Pay card is available in Spenddat's browser - sign in with "
            "`spenddat browser login` and save a card in Shop Pay; nothing was bought"
        )
    return f"checkout isn't ready ({co.get('status')}): {'; '.join(msgs) or 'details missing'}"
