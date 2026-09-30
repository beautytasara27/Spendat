"""Manage the store list from the UI: add, enable/disable, trust level, remove.

Two files are edited, both human-readable and comment-preserving (ruamel round-trip):

* the vendors file (``config/vendors.yaml``): which stores exist and how to reach them;
* the policy file (``policies/*.yaml``): each store's trust level, as the policy's
  ``vendors.allow`` / ``vendors.deny`` lists:

    auto    -> in ``allow``: in-policy purchases complete without asking you
    ask     -> in neither: every purchase from it comes to you for approval
    blocked -> in ``deny``: purchases are denied outright

Trust is a policy change, so the policy's content hash (recorded on every decision)
changes with it, and every edit is written to the audit log. Secrets are never written
here: a store's API key is referenced by secret name only.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from spenddat.models import RuntimeMode

TRUST_LEVELS = ("auto", "ask", "blocked")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
SANDBOX_ONLY_TYPES = {"reference", "stub"}


class StoreConfigError(ValueError):
    pass


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"store_{s}"
    return s[:40]


class StoreConfig:
    def __init__(self, vendors_file: str | Path, policy_file: str | Path, mode: RuntimeMode):
        self.vendors_file = Path(vendors_file)
        self.policy_file = Path(policy_file)
        self.mode = mode

    # ------------------------------------------------------------------ io

    def _load(self, path: Path) -> CommentedMap:
        data = _yaml().load(path.read_text(encoding="utf-8")) if path.exists() else None
        return data if data is not None else CommentedMap()

    @staticmethod
    def _dump(data: Any) -> str:
        buf = io.StringIO()
        _yaml().dump(data, buf)
        return buf.getvalue()

    def snapshot(self) -> dict[Path, str | None]:
        return {
            p: (p.read_text(encoding="utf-8") if p.exists() else None)
            for p in (self.vendors_file, self.policy_file)
        }

    def restore(self, snap: dict[Path, str | None]) -> None:
        for p, text in snap.items():
            if text is None:
                p.unlink(missing_ok=True)
            else:
                self._write(p, text)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _vendors(self) -> tuple[CommentedMap, CommentedSeq]:
        doc = self._load(self.vendors_file)
        if "vendors" not in doc or doc["vendors"] is None:
            doc["vendors"] = CommentedSeq()
        return doc, doc["vendors"]

    def _policy_lists(self) -> tuple[CommentedMap, list[str], list[str]]:
        doc = self._load(self.policy_file)
        v = doc.setdefault("vendors", CommentedMap())
        for k in ("allow", "deny"):
            if v.get(k) is None:
                seq = CommentedSeq()
                seq.fa.set_flow_style()
                v[k] = seq
        return doc, v["allow"], v["deny"]

    # ------------------------------------------------------------------ queries

    def trust_of(self, vid: str) -> str:
        _, allow, deny = self._policy_lists()
        if vid in deny:
            return "blocked"
        if vid in allow:
            return "auto"
        return "ask"

    def list(self) -> list[dict[str, Any]]:
        _, vendors = self._vendors()
        _, allow, deny = self._policy_lists()
        out = []
        for v in vendors:
            vid = str(v.get("id"))
            section = v.get(self.mode.value) or {}
            out.append(
                {
                    "id": vid,
                    "name": str(v.get("name") or vid.replace("_", " ").title()),
                    "type": str(v.get("type")),
                    "enabled": bool(v.get("enabled", True)),
                    "trust": "blocked" if vid in deny else "auto" if vid in allow else "ask",
                    "configured_for_mode": bool(section),
                    "base_url": section.get("base_url"),
                    "feed_url": section.get("feed_url"),
                    "catalog": section.get("catalog"),
                    "api_key_secret": section.get("api_key_secret"),
                    "notes": str(v.get("notes") or ""),
                }
            )
        return out

    def get(self, vid: str) -> dict[str, Any]:
        for s in self.list():
            if s["id"] == vid:
                return s
        raise StoreConfigError(f"no store with id '{vid}'")

    # ------------------------------------------------------------------ mutations

    def set_enabled(self, vid: str, enabled: bool) -> None:
        doc, vendors = self._vendors()
        entry = next((v for v in vendors if v.get("id") == vid), None)
        if entry is None:
            raise StoreConfigError(f"no store with id '{vid}'")
        entry["enabled"] = bool(enabled)
        self._write(self.vendors_file, self._dump(doc))

    def set_trust(self, vid: str, trust: str) -> None:
        if trust not in TRUST_LEVELS:
            raise StoreConfigError(f"trust must be one of {TRUST_LEVELS}")
        self.get(vid)  # must exist
        doc, allow, deny = self._policy_lists()
        for seq in (allow, deny):
            while vid in seq:
                seq.remove(vid)
        if trust == "auto":
            allow.append(vid)
        elif trust == "blocked":
            deny.append(vid)
        self._write(self.policy_file, self._dump(doc))

    def add(self, spec: dict[str, Any]) -> str:
        name = str(spec.get("name") or "").strip()
        if not 2 <= len(name) <= 60:
            raise StoreConfigError("give the store a name (2-60 characters)")
        vid = str(spec.get("id") or slugify(name)).strip()
        if not ID_RE.match(vid):
            raise StoreConfigError("store id must be lowercase letters, digits and _")
        vtype = str(spec.get("type") or "acp")
        if vtype not in ("acp", "reference"):
            raise StoreConfigError("store type must be 'acp' (agent checkout) or 'reference'")
        if vtype in SANDBOX_ONLY_TYPES and self.mode is RuntimeMode.LIVE:
            raise StoreConfigError("demo stores are only available in sandbox mode")
        doc, vendors = self._vendors()
        if any(v.get("id") == vid for v in vendors):
            raise StoreConfigError(f"a store with id '{vid}' already exists")

        section = CommentedMap()
        if vtype == "reference":
            section["catalog"] = str(spec.get("catalog") or "everyday")
        else:
            for key in ("base_url", "feed_url"):
                url = str(spec.get(key) or "").strip()
                if not re.match(r"^https?://[^\s]+$", url):
                    raise StoreConfigError(f"{key.replace('_', ' ')} must be a URL")
                if self.mode is RuntimeMode.LIVE and not url.startswith("https://"):
                    raise StoreConfigError(f"{key.replace('_', ' ')} must use https in live mode")
                section[key] = url
            profile = str(spec.get("seller_profile") or "").strip()
            if not re.match(r"^profile_[A-Za-z0-9_]+$", profile):
                raise StoreConfigError(
                    "seller profile must be the store's Stripe profile id (profile_...)"
                )
            section["seller_profile"] = profile
            section["api_key_secret"] = str(spec.get("api_key_secret") or f"{vid}_api_key")
            section["api_version"] = str(spec.get("api_version") or "2025-09-29")

        entry = CommentedMap()
        entry["id"] = vid
        entry["name"] = name
        entry["type"] = vtype
        entry["enabled"] = True
        entry[self.mode.value] = section
        vendors.append(entry)
        self._write(self.vendors_file, self._dump(doc))
        self.set_trust(vid, str(spec.get("trust") or "ask"))
        return vid

    def remove(self, vid: str) -> None:
        doc, vendors = self._vendors()
        idx = next((i for i, v in enumerate(vendors) if v.get("id") == vid), None)
        if idx is None:
            raise StoreConfigError(f"no store with id '{vid}'")
        del vendors[idx]
        self._write(self.vendors_file, self._dump(doc))
        pdoc, allow, _deny = self._policy_lists()
        while vid in allow:
            allow.remove(vid)
        # keep a removed store blocked if it was blocked; otherwise drop it from policy
        self._write(self.policy_file, self._dump(pdoc))

    def secret_env_name(self, vid: str) -> str | None:
        s = self.get(vid)
        if not s["api_key_secret"]:
            return None
        return f"SPENDDAT_{self.mode.value.upper()}_{str(s['api_key_secret']).upper()}"
