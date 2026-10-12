"""Parse the server-rendered content-search response into post cards.

The initial content-search POST returns fully server-rendered HTML: every
result card carries the author (control-menu aria-label), their profile URL,
headline, the post text (expandable-text-box), a relative time token, and the
post's activity/ugcPost URN. This is the same structure the DOM harvest used,
parsed server-side: one request per search, zero per-post visits for the
people list.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .rsc import _ACTIVITY, BASE

_MENU = re.compile(r'aria-label="Open control menu for post by ([^"]+)"')
_PROFILE = re.compile(r'href="(https://www\.linkedin\.com/(?:in|company)/[A-Za-z0-9\-_%]+)/?"')
_TEXTBOX = re.compile(r'data-testid="expandable-text-box"')
_TIME = re.compile(r"\b(?:Just now|\d{1,2}\s*(?:m|h|d|w|mo)\b)\b", re.IGNORECASE)
_REL = re.compile(r"^(\d{1,2})\s*(m|h|d|w|mo)$", re.IGNORECASE)
_HEADLINE = re.compile(r"<p[^>]*><span>((?:(?!</span>).){10,300})</span>", re.DOTALL)


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _relative_to_utc(token: str, now: datetime) -> datetime | None:
    m = _REL.match(token.strip())
    if not m:
        return now  # "Just now"
    n, unit = int(m.group(1)), m.group(2).lower()
    deltas = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}
    return now - timedelta(seconds=n * deltas[unit])


def parse_ssr_results(html: str, now: datetime | None = None) -> list[dict]:
    """Extract post cards from the SSR search response.

    Returns rows: {author, author_type, profile_url, headline, post_text,
    native_id, urn, post_url, time_str, posted_at(UTC datetime)}. Cards without
    an extractable URN are still returned (urn='' ; the date then comes from
    the relative token)."""
    now = now or datetime.now(UTC)
    menus = list(_MENU.finditer(html))
    rows = []
    prev_menu_start = 0
    for i, m in enumerate(menus):
        author = m.group(1).strip()
        # this card's header (avatar + profile anchor) sits between the previous
        # card's menu and this card's menu; the profile anchor is the LAST one
        # in that zone (closest to this card's menu button)
        header_zone = html[prev_menu_start:m.start()]
        seg_start = m.end()
        seg_end = menus[i + 1].start() if i + 1 < len(menus) else len(html)
        seg = html[seg_start:seg_end]

        # profile: the anchor closest BEFORE this card's menu button
        profile_url, author_type = "", ""
        pm = None
        for pm in _PROFILE.finditer(header_zone):
            pass  # keep the last match in the header zone
        if pm:
            profile_url = pm.group(1)
            author_type = "company" if "/company/" in profile_url else "person"

        # headline: the first <p><span> block after the menu button
        hm = _HEADLINE.search(seg[:4000])
        headline = _strip_tags(hm.group(1)) if hm else ""

        # post text: the expandable-text-box block, tags stripped
        tb = _TEXTBOX.search(seg)
        text = ""
        if tb:
            text = _strip_tags(seg[tb.end():tb.end() + 6000])[:5000]

        # the card's own meta row (name • time) lives in the header, BEFORE the
        # menu button -- the time token must come from there, not from the body
        tm = _TIME.search(header_zone[-1500:])
        time_str = tm.group(0) if tm else ""

        um = _ACTIVITY.search(seg)
        native_id, urn = (um.group(1), um.group(0)) if um else ("", "")
        posted_at = _relative_to_utc(time_str, now) if time_str else None

        rows.append({
            "author": author, "author_type": author_type,
            "profile_url": profile_url, "headline": headline,
            "post_text": text, "native_id": native_id, "urn": urn,
            "post_url": (f"{BASE}/feed/update/urn:li:activity:{native_id}/"
                         if native_id else ""),
            "time_str": time_str, "posted_at": posted_at,
        })
        prev_menu_start = m.start()
    return rows


def filter_days(rows: list[dict], days: int, now: datetime | None = None) -> list[dict]:
    """Keep rows posted within the last `days` days (urn timestamp first, then
    the relative token). Rows without any date signal are kept only when the
    urn carries a parseable timestamp."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    kept = []
    for r in rows:
        dt = None
        if r.get("native_id"):
            try:
                ms = int(r["native_id"]) >> 22
                dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
            except (ValueError, OSError):
                dt = None
        if dt is None:
            dt = r.get("posted_at")
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        r["posted_at_dt"] = dt
        if dt is not None and dt >= cutoff:
            kept.append(r)
    return kept


def search_url(keywords: str) -> str:
    return (f"{BASE}/flagship-web/search/results/content/"
            f"?keywords={quote(keywords)}&origin=FACETED_SEARCH")
