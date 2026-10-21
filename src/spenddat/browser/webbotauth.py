"""Web Bot Auth: cryptographically identify Spenddat's browser to stores.

Shopify asks agents to sign their browser requests with Web Bot Auth (HTTP Message
Signatures, RFC 9421, with an Ed25519 key) and verifies only keys you have registered
with Shopify. Unsigned agents may be slowed down or blocked by bot protection.

What you do once:
1. ``spenddat browser keys`` - creates ``data/wba/key.pem`` (private, never shared)
   and ``data/wba/http-message-signatures-directory`` (public key directory, JWKS).
2. Host that directory file at a public https URL you control, e.g.
   ``https://<you>.github.io/.well-known/http-message-signatures-directory``.
3. Register that URL with Shopify (see shopify.dev "Web Bot Auth" for agents).
4. Put ``SPENDDAT_WBA_DIRECTORY_URL=<that URL>`` in ``.env``.

Then every request Spenddat's browser makes to a store is signed. The signature format
follows the Web Bot Auth draft; until Shopify confirms your registration it's untested
against their verifier.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _load_crypto() -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as e:  # pragma: no cover
        raise RuntimeError('Web Bot Auth needs: pip install -e ".[browser]"') from e
    return serialization, Ed25519PrivateKey


def jwk_for(public_raw: bytes) -> dict[str, str]:
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64u(public_raw)}


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638 thumbprint, used as the signature's keyid."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}, separators=(",", ":")
    )
    return _b64u(hashlib.sha256(canonical.encode()).digest())


def init_keys(directory: str | Path = "data/wba") -> tuple[Path, Path, str]:
    """Create a key pair and the public key directory. Never overwrites an existing key."""
    serialization, Ed25519PrivateKey = _load_crypto()
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    key_file = d / "key.pem"
    if key_file.exists():
        priv = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    else:
        priv = Ed25519PrivateKey.generate()
        key_file.write_bytes(
            priv.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        with contextlib.suppress(OSError):  # Windows ignores POSIX modes
            key_file.chmod(0o600)
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk = jwk_for(pub)
    dir_file = d / "http-message-signatures-directory"
    dir_file.write_text(json.dumps({"keys": [jwk]}, indent=2), encoding="utf-8")
    return key_file, dir_file, jwk_thumbprint(jwk)


@dataclass
class WebBotAuthSigner:
    private_key: Any
    directory_url: str
    keyid: str
    ttl_s: int = 60
    hosts: tuple[str, ...] = ()  # sign only these hosts (empty = all https hosts)

    @classmethod
    def from_files(cls, key_file: str | Path, directory_url: str) -> WebBotAuthSigner:
        serialization, _ = _load_crypto()
        priv = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
        pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cls(priv, directory_url, jwk_thumbprint(jwk_for(pub)))

    def headers_for(self, url: str, now: int | None = None) -> dict[str, str]:
        authority = urlparse(url).netloc.lower()
        created = int(now if now is not None else time.time())
        agent = f'"{self.directory_url}"'
        params = (
            '("@authority" "signature-agent")'
            f";created={created};expires={created + self.ttl_s}"
            f';keyid="{self.keyid}";alg="ed25519";nonce="{_b64u(secrets.token_bytes(16))}"'
            ';tag="web-bot-auth"'
        )
        base = (
            f'"@authority": {authority}\n"signature-agent": {agent}\n"@signature-params": {params}'
        )
        sig = self.private_key.sign(base.encode())
        return {
            "Signature-Agent": agent,
            "Signature-Input": f"sig1={params}",
            "Signature": f"sig1=:{base64.b64encode(sig).decode()}:",
        }

    def install(self, context: Any) -> None:
        """Sign matching requests made by a Playwright BrowserContext."""

        def handle(route: Any) -> None:
            req = route.request
            u = urlparse(req.url)
            if u.scheme == "https" and (not self.hosts or u.hostname in self.hosts):
                route.continue_(headers={**req.headers, **self.headers_for(req.url)})
            else:
                route.continue_()

        context.route("**/*", handle)
