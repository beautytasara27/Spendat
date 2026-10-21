"""A real Chrome window that Spenddat drives through WebMCP tools.

WebMCP is how websites expose "tools" to AI agents running in the browser. Shopify
registers its storefront and checkout tools on ``document.modelContext``; the agent
lists them with ``getTools()`` and calls them with ``executeTool(tool, jsonString)``.
Spenddat never clicks around or scrapes the page: it only calls the tools the store
publishes for agents.

Design:

* One worker thread owns Playwright (its sync API isn't thread-safe; the web UI is
  multi-threaded). Everything else submits jobs to it with ``BrowserSession.run``.
* A *persistent* Chrome profile (``data/browser-profile``) keeps your Shop Pay sign-in
  between runs. Spenddat never reads cookies or passwords from it.
* WebMCP is still behind flags in Chrome (origin trial until it ships), so Chrome is
  launched with ``--enable-features=WebMCPTesting --enable-blink-features=WebMCP``.
* The window is visible: Shopify may ask you to review an order on the page, and you
  can always see what the agent is doing.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WEBMCP_ARGS = ["--enable-features=WebMCPTesting", "--enable-blink-features=WebMCP"]


class BrowserUnavailable(RuntimeError):
    """Chrome/Playwright can't be started, or the page has no WebMCP support."""


class ToolCallError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool


@dataclass
class BrowserConfig:
    profile_dir: str = "data/browser-profile"
    channel: str | None = "chrome"  # "chrome", "msedge", or None for bundled Chromium
    headless: bool = False
    executable_path: str | None = None
    extra_args: list[str] = field(default_factory=list)
    nav_timeout_ms: int = 45_000
    # Optional: called with the BrowserContext right after launch (Web Bot Auth signing).
    on_context: Callable[[Any], None] | None = None


# JavaScript run inside the page. Arguments are always passed as a JSON *string*
# (Chrome 153 rejects objects with "Failed to parse input arguments").
_JS_LIST = """
async () => {
  const mc = document.modelContext;
  if (!mc || typeof mc.getTools !== 'function') return null;
  const tools = await mc.getTools();
  return tools
    .filter(t => (!t.window || t.window === window) && (!t.origin || t.origin === location.origin))
    .map(t => ({
      name: t.name,
      description: t.description || '',
      inputSchema: typeof t.inputSchema === 'string' ? t.inputSchema
                   : JSON.stringify(t.inputSchema || {}),
    }));
}
"""

_JS_CALL = """
async ([name, args]) => {
  const mc = document.modelContext;
  if (!mc) return {__error: 'no_webmcp'};
  const tools = await mc.getTools();
  const tool = tools.find(t => t.name === name
      && (!t.window || t.window === window) && (!t.origin || t.origin === location.origin));
  if (!tool) return {__error: 'no_such_tool', available: tools.map(t => t.name)};
  try {
    const raw = await mc.executeTool(tool, args);
    return {__raw: raw};
  } catch (e) {
    return {__error: 'exception', message: String(e && e.message || e)};
  }
}
"""


class BrowserSession:
    """Owns the browser on a dedicated thread. Use ``run(fn)`` from any thread."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._jobs: queue.Queue[tuple[Callable[[BrowserSession], Any], Future] | None] = (
            queue.Queue()
        )
        self._thread: threading.Thread | None = None
        self._pw: Any = None
        self._context: Any = None
        self.pages: dict[str, Any] = {}  # label -> Page (e.g. checkout id -> its tab)

    # ------------------------------------------------------------------ threading

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="spenddat-browser", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                break
            fn, fut = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(self))
                except BaseException as e:
                    fut.set_exception(e)
        self._shutdown()

    def run(self, fn: Callable[[BrowserSession], Any], timeout: float | None = 900) -> Any:
        self._ensure_thread()
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut.result(timeout=timeout)

    def close(self) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=15)

    # ------------------------------------------------------------------ lifecycle (worker)

    @property
    def context(self) -> Any:
        if self._context is None:
            self._launch()
        return self._context

    def _launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - depends on install
            raise BrowserUnavailable(
                'the browser feature needs Playwright: pip install -e ".[browser]"'
            ) from e
        Path(self.config.profile_dir).mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "args": [*WEBMCP_ARGS, *self.config.extra_args],
            "viewport": None if not self.config.headless else {"width": 1280, "height": 900},
        }
        if self.config.executable_path:
            kwargs["executable_path"] = self.config.executable_path
        elif self.config.channel:
            kwargs["channel"] = self.config.channel
        try:
            self._context = self._pw.chromium.launch_persistent_context(
                self.config.profile_dir, **kwargs
            )
        except Exception as e:
            self._pw.stop()
            self._pw = None
            hint = (
                "Is Google Chrome installed? Or set SPENDDAT_BROWSER_CHANNEL=chromium "
                "after running: playwright install chromium"
                if self.config.channel
                else ""
            )
            raise BrowserUnavailable(f"couldn't start the browser: {e}. {hint}") from e
        self._context.set_default_navigation_timeout(self.config.nav_timeout_ms)
        if self.config.on_context:
            self.config.on_context(self._context)

    def _shutdown(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None
            self.pages.clear()

    # ------------------------------------------------------------------ pages (worker)

    def page(self, label: str, url: str | None = None) -> Any:
        """Get (or open) the tab for ``label``; navigate to ``url`` if given."""
        p = self.pages.get(label)
        if p is None or p.is_closed():
            p = self.context.new_page()
            self.pages[label] = p
        if url:
            p.goto(url, wait_until="domcontentloaded")
            wait_for_webmcp(p)
        return p

    def adopt(self, old: str, new: str) -> None:
        if old in self.pages:
            self.pages[new] = self.pages.pop(old)

    def close_page(self, label: str) -> None:
        p = self.pages.pop(label, None)
        if p is not None and not p.is_closed():
            p.close()


# ---------------------------------------------------------------------- WebMCP calls


def wait_for_webmcp(page: Any, timeout_ms: int = 15_000) -> None:
    try:
        page.wait_for_function(
            "() => !!(document.modelContext && document.modelContext.getTools)",
            timeout=timeout_ms,
        )
    except Exception as e:
        raise BrowserUnavailable(
            "this page doesn't offer WebMCP tools. Either the browser has WebMCP turned "
            "off (needs a recent Chrome; Spenddat starts it with the WebMCP flags), or the "
            "site doesn't publish agent tools."
        ) from e


def list_tools(page: Any) -> list[dict[str, Any]]:
    tools = page.evaluate(_JS_LIST)
    if tools is None:
        raise BrowserUnavailable("document.modelContext is not available on this page")
    for t in tools:
        try:
            t["inputSchema"] = json.loads(t.get("inputSchema") or "{}")
        except ValueError:
            t["inputSchema"] = {}
    return tools


def wait_for_tool(page: Any, name: str, timeout_ms: int = 20_000) -> None:
    """Tools change as the page changes (storefront -> checkout); wait for ``name``."""
    js = """
    async (name) => {
      const mc = document.modelContext;
      if (!mc) return false;
      const tools = await mc.getTools();
      return tools.some(t => t.name === name);
    }
    """
    deadline_step = 250
    waited = 0
    while waited <= timeout_ms:
        try:
            if page.evaluate(js, name):
                return
        except Exception:  # noqa: S110 - the page is mid-navigation; try again
            pass
        page.wait_for_timeout(deadline_step)
        waited += deadline_step
    raise ToolCallError(name, "tool did not appear on the page")


def call_tool(page: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    """Call a WebMCP tool. Returns parsed JSON, the raw string if not JSON, or None
    when the page navigated away as a result of the call."""
    try:
        res = page.evaluate(_JS_CALL, [name, json.dumps(args or {})])
    except Exception as e:
        msg = str(e)
        if "navigat" in msg.lower() or "context was destroyed" in msg.lower():
            return None
        raise ToolCallError(name, msg) from e
    if not isinstance(res, dict):
        raise ToolCallError(name, f"unexpected result {res!r}")
    if "__error" in res:
        if res["__error"] == "no_such_tool":
            raise ToolCallError(name, f"not offered here (available: {res.get('available')})")
        raise ToolCallError(name, res.get("message") or res["__error"])
    raw = res.get("__raw")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw
