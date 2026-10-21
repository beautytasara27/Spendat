"""Browser-based shopping: a real Chrome window driven through WebMCP agent tools."""

from __future__ import annotations

import os
import threading

from spenddat.browser.session import BrowserConfig, BrowserSession

_shared: BrowserSession | None = None
_lock = threading.Lock()


def config_from_env(env: dict[str, str] | None = None) -> BrowserConfig:
    e = env if env is not None else os.environ
    channel = e.get("SPENDDAT_BROWSER_CHANNEL", "chrome").strip().lower()
    cfg = BrowserConfig(
        profile_dir=e.get("SPENDDAT_BROWSER_PROFILE", "data/browser-profile"),
        channel=None if channel in ("", "chromium", "bundled") else channel,
        headless=e.get("SPENDDAT_BROWSER_HEADLESS", "0") == "1",
        executable_path=e.get("SPENDDAT_BROWSER_EXECUTABLE") or None,
    )
    key_file = e.get("SPENDDAT_WBA_KEY_FILE", "data/wba/key.pem")
    directory = e.get("SPENDDAT_WBA_DIRECTORY_URL")
    if directory and os.path.exists(key_file):
        from spenddat.browser.webbotauth import WebBotAuthSigner

        signer = WebBotAuthSigner.from_files(key_file, directory)
        cfg.on_context = signer.install
    return cfg


def shared_session() -> BrowserSession:
    """One browser for the whole app (one Chrome profile, one Shop Pay sign-in)."""
    global _shared
    with _lock:
        if _shared is None:
            _shared = BrowserSession(config_from_env())
        return _shared


__all__ = ["BrowserConfig", "BrowserSession", "config_from_env", "shared_session"]
