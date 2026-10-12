"""Reader: turn a LinkedIn post permalink into a clean record, keyless.

LinkedIn serves public post permalinks to signed-out visitors with a complete
schema.org ld+json block embedded: author (name + profile URL), the full post
text, the exact publish timestamp, reaction and comment counts, and the first
comments with their own authors and dates. One GET per post, no cookies.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_AUTHWALL = re.compile(r"authwall|/uas/login", re.IGNORECASE)


_OG_TITLE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.IGNORECASE)
_OG_AUTHOR = re.compile(r"^(.*)\s+posted on (?:the topic|LinkedIn)\s*$", re.IGNORECASE)


def _og_author_fallback(html: str) -> str:
    """og:title looks like 'Post title | Author posted on the topic | LinkedIn';
    recover the author segment when the ld+json creator block is missing."""
    m = _OG_TITLE.search(html)
    if not m:
        return ""
    for part in m.group(1).split(" | "):
        am = _OG_AUTHOR.match(part.strip())
        if am and am.group(1).strip():
            return am.group(1).strip()
    return ""


def parse_ldjson(html: str) -> dict:
    """Parse the post ld+json object out of a permalink page. {} when absent."""
    for m in _LDJSON.finditer(html):
        try:
            obj = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("@id"):
            return obj
    return {}


def _stat(obj, kind: str):
    for s in obj.get("interactionStatistic") or []:
        if kind in str(s.get("interactionType", "")):
            return s.get("userInteractionCount")
    return None


def flatten(obj: dict) -> dict:
    """Flatten the ld+json post object into listener row fields."""
    creator = obj.get("creator") or {}
    comments = [
        {"author": (c.get("creator") or {}).get("name", ""),
         "date": c.get("datePublished", ""),
         "text": (c.get("text") or "").strip()}
        for c in (obj.get("comment") or []) if isinstance(c, dict)
    ]
    return {
        "post_url": obj.get("@id", ""),
        "author_name": creator.get("name", ""),
        "author_profile": creator.get("url", ""),
        "posted_at": obj.get("datePublished", ""),
        "reactions": _stat(obj, "LikeAction"),
        "comment_count": _stat(obj, "CommentAction") or len(comments),
        "post_text": (obj.get("description") or obj.get("headline") or "").strip(),
        "comments": comments,
    }


def read_post(session, url: str, cookies: dict | None = None, log=print) -> dict | None:
    """Fetch one permalink and return a flattened record, or None with a reason.

    Accepts both /posts/<slug>-activity-<id> and /feed/update/urn:li:activity:<id>
    forms. Without cookies only public posts render (the rest authwall); pass the
    session cookies to read exactly what the logged-in account can see."""
    try:
        r = session.get(url, timeout=30, cookies=cookies or None)
    except Exception as exc:
        log(f"  fetch error {type(exc).__name__}: {url[:80]}")
        return None
    if r.status_code != 200 or _AUTHWALL.search(r.text[:4000]) or "/uas/login" in str(r.url):
        log(f"  not public (status {r.status_code}): {url[:80]}")
        return None
    obj = parse_ldjson(r.text)
    if not obj:
        log(f"  no ld+json (non-public or unsupported): {url[:80]}")
        return None
    row = flatten(obj)
    if not row["author_name"]:
        row["author_name"] = _og_author_fallback(r.text)
    if not row["post_url"]:
        row["post_url"] = url
    return row


def parse_date(row: dict):
    """Row's posted_at as an aware datetime, or None when unparseable."""
    raw = row.get("posted_at") or ""
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def utcnow() -> datetime:
    return datetime.now(UTC)
