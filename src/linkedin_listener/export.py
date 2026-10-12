"""CSV export and person-level deduplication."""
from __future__ import annotations

import csv
import re
from urllib.parse import urlsplit

PEOPLE_COLS = ["person_name", "profile_url", "headline", "engagement_types",
               "reaction_types", "source_post_urls", "source_keywords", "post_count"]


def canonical(url: str) -> str:
    """Strip tracking params/fragments so the same post never lands twice."""
    return (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")


def dedupe(rows: list[dict], key_field: str = "profile_url") -> tuple[list[dict], int]:
    """Dedupe by key_field (default: author profile URL -> people view).
    Rows with an empty key fall back to the author name. Returns
    (unique rows, dropped count)."""
    seen, out = set(), []
    for r in rows:
        key = canonical(r.get(key_field, "")) or f"name:{r.get('author', '')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, len(rows) - len(out)


def _person_key(url: str) -> str:
    parsed = urlsplit(canonical(url))
    if parsed.hostname not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/in/[^/]+", path, re.IGNORECASE):
        return ""
    return f"https://www.linkedin.com{path}".casefold()


def _as_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.split(";") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part) for part in value if str(part).strip()]
    return [str(value)]


def merge_people(rows: list[dict]) -> tuple[list[dict], int, int]:
    """Clean person events and merge by LinkedIn profile URL.

    Returns (people, duplicate_events_removed, invalid_events_dropped). Each
    person retains every engagement type, source post and keyword.
    """
    merged: dict[str, dict] = {}
    valid_events = 0
    invalid_events = 0
    for row in rows:
        name = _clean_person_name(row.get("person_name") or row.get("author"))
        profile = canonical(row.get("profile_url", ""))
        key = _person_key(profile)
        if not name or not key:
            invalid_events += 1
            continue
        valid_events += 1
        person = merged.get(key)
        if person is None:
            person = {
                "person_name": name,
                "profile_url": key,
                "headline": _clean(row.get("headline")),
                "_engagement_types": set(),
                "_reaction_types": set(),
                "_source_post_urls": [],
                "_source_keywords": [],
            }
            merged[key] = person
        elif not person["headline"] and row.get("headline"):
            person["headline"] = _clean(row.get("headline"))

        event_type = row.get("engagement_type") or row.get("engagement_types") or "posted"
        person["_engagement_types"].update(
            value.strip().lower() for value in _as_values(event_type) if value.strip()
        )
        person["_reaction_types"].update(
            cleaned for value in _as_values(row.get("reaction_type")
                                            or row.get("reaction_types"))
            if (cleaned := _clean(value))
        )
        post_urls = (row.get("source_post_urls") or row.get("source_post_url")
                     or row.get("post_url"))
        for value in _as_values(post_urls):
            url = canonical(value)
            if url and url not in person["_source_post_urls"]:
                person["_source_post_urls"].append(url)
        keywords = row.get("source_keywords") or row.get("source_keyword") or row.get("keyword")
        for value in _as_values(keywords):
            value = _clean(value)
            if value and value not in person["_source_keywords"]:
                person["_source_keywords"].append(value)

    engagement_order = {"posted": 0, "commented": 1, "reacted": 2}
    out = []
    for person in merged.values():
        types = sorted(person.pop("_engagement_types"),
                       key=lambda value: (engagement_order.get(value, 10), value))
        reactions = sorted(person.pop("_reaction_types"), key=str.casefold)
        posts = person.pop("_source_post_urls")
        keywords = person.pop("_source_keywords")
        person.update({
            "engagement_types": "; ".join(types),
            "reaction_types": "; ".join(reactions),
            "source_post_urls": "; ".join(posts),
            "source_keywords": "; ".join(keywords),
            "post_count": len(posts),
        })
        out.append(person)
    return out, valid_events - len(out), invalid_events


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_person_name(value) -> str:
    """Remove LinkedIn profile badges accidentally concatenated into a name."""
    name = str(value or "").replace("\u00a0", " ")
    for pattern in (
        r"\bExecutive\s+Top\s+Voice\s+Profile\b",
        r"\bTop\s+Voice(?:\s+Profile)?\b",
        r"\b(?:Verified|Premium)\s+Profile\b",
        r"\bOpen\s+to\s+work\b",
        r"(?<!\w)(?:1st|2nd|3rd)(?:\+)?(?!\w)",
    ):
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[•·,]", " ", name)
    words = _clean(name).split()
    for width in range(len(words) // 2, 0, -1):
        if words[:width] == words[width:2 * width] and 2 * width == len(words):
            words = words[:width]
            break
    return " ".join(words)


def write_people(rows: list[dict], path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=PEOPLE_COLS)
        w.writeheader()
        for r in rows:
            values = dict(r)
            values.setdefault("person_name", r.get("author", ""))
            values.setdefault("engagement_types", r.get("engagement_type", "posted"))
            values.setdefault("reaction_types", r.get("reaction_type", ""))
            values.setdefault("source_post_urls", r.get("post_url", ""))
            values.setdefault("source_keywords", r.get("keyword", ""))
            values.setdefault("post_count", 1 if r.get("post_url") else 0)
            w.writerow({k: _clean(values.get(k)) for k in PEOPLE_COLS})
    return len(rows)


def verify_people_csv(path: str) -> int:
    """Re-read the final CSV and reject blank or duplicate person keys."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != PEOPLE_COLS:
            raise ValueError("final people CSV has an unexpected column schema")
        rows = list(reader)
    keys = []
    for row in rows:
        key = _person_key(row.get("profile_url", ""))
        if not row.get("person_name", "").strip() or not key:
            raise ValueError("final people CSV contains a blank or malformed person")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError("final people CSV contains duplicate LinkedIn profile URLs")
    return len(rows)


def write_comments(rows: list[dict], path: str) -> int:
    cols = ["post_url", "author_name", "comment_author", "comment_date", "comment_text"]
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            for c in r.get("comments") or []:
                w.writerow({
                    "post_url": r.get("post_url", ""),
                    "author_name": r.get("author_name", ""),
                    "comment_author": _clean(c.get("author")),
                    "comment_date": _clean(c.get("date")),
                    "comment_text": _clean(c.get("text")),
                })
                n += 1
    return n
