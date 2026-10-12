"""The listener session: one persistent Chrome profile, logged in once.

The profile directory IS the session: the human signs in once with --login,
LinkedIn issues its cookies directly into the profile, and every later run
reuses the same fingerprint. Cookie exports and fresh contexts are exactly
what get sessions invalidated; this module makes both impossible.

After login, the profile's live li_at/JSESSIONID can be read by the RSC
fetcher for same-origin browser-context replay.
"""
from __future__ import annotations

import time
from typing import Self

_LANDING = "https://www.linkedin.com/feed/"
_LOGIN = "https://www.linkedin.com/login"

_ME_JS = """async () => {
  const jsid = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1] || '';
  const r = await fetch('/voyager/api/me', { headers: { accept: 'application/vnd.linkedin.normalized+json+2.1', 'csrf-token': jsid, 'x-restli-protocol-version': '2.0.0' }, credentials: 'include' });
  return r.status;
}"""


class SessionNotLoggedIn(RuntimeError):
    pass


class LinkedInSession:
    """Persistent-context Chrome holding one live LinkedIn session."""

    def __init__(self, profile_dir: str, headless: bool = False):
        self.profile_dir = profile_dir
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._page = None

    def __enter__(self) -> Self:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chrome",
            headless=self.headless,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        try:
            self._ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                        origin="https://www.linkedin.com")
        except Exception:
            pass
        try:
            self._page.goto(_LANDING, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    @property
    def page(self):
        return self._page

    def session_status(self) -> int:
        """200 when the profile holds a live LinkedIn session."""
        try:
            return self._page.evaluate(_ME_JS)
        except Exception:
            return 0

    def verify_session(self) -> None:
        if self.session_status() != 200:
            raise SessionNotLoggedIn(
                f"no live session in {self.profile_dir}. "
                "Run: linkedin-listener --login"
            )

    def login(self, timeout: int = 600) -> None:
        """Open the login page and wait for the human to sign in once."""
        self._page.goto(_LOGIN, wait_until="domcontentloaded", timeout=60000)
        print(f"sign in inside the seat Chrome (profile: {self.profile_dir}). "
              f"Waiting up to {timeout // 60} min...", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                if self._page.evaluate(_ME_JS) == 200:
                    print("session established and saved to the profile.", flush=True)
                    return
            except Exception:
                continue
        raise SessionNotLoggedIn("timed out waiting for login")

    def cookies(self) -> dict:
        """The live session cookies needed for same-origin HTTP replay."""
        vals = {c["name"]: c["value"] for c in self._ctx.cookies("https://www.linkedin.com")
                if c["name"] in ("li_at", "JSESSIONID")}
        if "li_at" not in vals:
            raise SessionNotLoggedIn("no li_at in the session profile")
        return vals
