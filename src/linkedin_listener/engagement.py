"""Parsers and request builders for LinkedIn comment and reaction data."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .rsc import _walk as _walk_flight
from .rsc import parse_flight

_COMMENT_PREFIX = "CommentComponentReference_"
_COMMENT_URN = re.compile(r"urn:li:comment:\(activity:\d+,\d+\)")
_REACTED = " reacted with "
_PROFILE_PATH = re.compile(r"^/in/[^/]+/?$", re.IGNORECASE)
_COMPANY_PATH = re.compile(r"^/company/[^/]+/?$", re.IGNORECASE)
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {"br", "div", "li", "p", "section"}


def build_reactors_payload(activity_id: str, page_token: str | None = None,
                           page_size: int = 10) -> dict:
    """Build the captured reactorsListByTypeScreen pagination body."""
    operation = "com.linkedin.sdui.impl.social.components.reactorsListByTypeScreen"
    payload = {
        "threadUrn": {
            "threadUrnActivityThreadUrn": {
                "activityUrn": {"activityId": str(activity_id)},
            },
        },
        "pageSize": page_size,
    }
    if page_token:
        payload["pageToken"] = page_token

    current_actor_id = f"identitySwitcherActorContext-urn:li:activity:{activity_id}"
    request_metadata = {
        "$type": "proto.sdui.common.RequestMetadata",
        "currentActor": {
            "$type": "proto.sdui.bindings.core.Bindable",
            "key": {
                "key": {"value": {"$case": "id", "id": current_actor_id}},
                "namespace": "",
            },
            "content": {"$case": "stringBinding", "stringBinding": {}},
        },
    }

    def requested_args(include_screen: bool = False) -> dict:
        result = {
            "$type": "proto.sdui.actions.requests.RequestedArguments",
            "requestedStateKeys": [],
            "payload": dict(payload),
            "requestMetadata": request_metadata,
        }
        if include_screen:
            result.update({
                "states": [],
                "screenId": "com.linkedin.sdui.flagshipnav.feed.social.ReactorsList",
                "knownTemplateIds": [],
            })
        return result

    return {
        "pagerId": operation,
        "clientArguments": requested_args(include_screen=True),
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": operation,
            "trigger": {
                "$case": "itemDistanceTrigger",
                "itemDistanceTrigger": {
                    "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                    "preloadDistance": 2,
                    "preloadLength": 250,
                },
            },
            "retryCount": 2,
            "requestedArguments": requested_args(),
        },
    }


def extract_next_page_token(flight: str) -> str | None:
    """Extract the opaque reactors cursor from an RSC flight response."""
    patterns = (
        r'"pageToken"\s*:\s*"([^"\\]+)"',
        r'pageToken\\":\\"([^"\\]+)',
        r"pageToken&quot;:&quot;([^&]+)&quot;",
    )
    for pattern in patterns:
        match = re.search(pattern, flight)
        if match and match.group(1):
            return match.group(1)
    return None


def _profile_url(value: str) -> tuple[str, str]:
    url = urljoin("https://www.linkedin.com", value or "")
    parsed = urlsplit(url)
    if parsed.hostname not in {"linkedin.com", "www.linkedin.com"}:
        return "", ""
    path = parsed.path.rstrip("/")
    if _PROFILE_PATH.fullmatch(path):
        return f"https://www.linkedin.com{path}", "person"
    if _COMPANY_PATH.fullmatch(path):
        return f"https://www.linkedin.com{path}", "company"
    return "", ""


def _clean_lines(values: list[str]) -> list[str]:
    return [re.sub(r"\s+", " ", value).strip() for value in values if value.strip()]


def _name_and_headline(anchor_text: str) -> tuple[str, str]:
    lines = _clean_lines(anchor_text.splitlines())
    if not lines:
        return "", ""

    def name_candidate(line: str) -> str:
        candidate = re.split(r"\s+(?:Verified|Premium Profile)\b", line, maxsplit=1,
                             flags=re.IGNORECASE)[0]
        candidate = re.sub(r"\s+\d+(?:st|nd|rd|th)$", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*[•·]\s*\d*(?:st|nd|rd|th)?$", "", candidate,
                           flags=re.IGNORECASE)
        return candidate.strip()

    ignored = {"author", "follow", "following", "verified", "premium profile"}
    name = ""
    name_line = -1
    for index, line in enumerate(lines):
        candidate = name_candidate(line)
        if candidate and candidate.casefold() not in ignored and not candidate.startswith("•"):
            name, name_line = candidate, index
            break

    headline = ""
    for line in lines[name_line + 1:]:
        candidate = name_candidate(line)
        if not candidate or candidate.casefold() in ignored or candidate == name:
            continue
        if re.fullmatch(r"\d+(?:st|nd|rd|th)?", candidate, re.IGNORECASE):
            continue
        headline = candidate
        break
    return name, headline


class _CommentCardParser(HTMLParser):
    """Collect rendered comment-card roots from LinkedIn's post HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._active: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if self._active:
            for card in self._active:
                if tag not in _VOID_TAGS:
                    card["depth"] += 1
                if tag in _BLOCK_TAGS:
                    card["text"].append("\n")
                    if card["open_anchor"] is not None:
                        card["anchors"][card["open_anchor"]]["text"].append("\n")
                if tag == "a":
                    card["anchors"].append({
                        "href": attrs_dict.get("href") or "",
                        "text": [],
                    })
                    card["open_anchor"] = len(card["anchors"]) - 1

        component_key = attrs_dict.get("componentkey") or attrs_dict.get("componentKey") or ""
        if component_key.startswith(_COMMENT_PREFIX):
            self._active.append({
                "component_key": component_key,
                "depth": 1,
                "text": [],
                "anchors": [],
                "open_anchor": None,
            })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        for card in self._active:
            card["text"].append(data)
            if card["open_anchor"] is not None:
                card["anchors"][card["open_anchor"]]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._active or tag in _VOID_TAGS:
            return
        for card in self._active:
            if tag == "a":
                card["open_anchor"] = None
            if tag in _BLOCK_TAGS:
                card["text"].append("\n")
                if card["open_anchor"] is not None:
                    card["anchors"][card["open_anchor"]]["text"].append("\n")
            card["depth"] -= 1

        finished = [card for card in self._active if card["depth"] <= 0]
        self._active = [card for card in self._active if card["depth"] > 0]
        for card in finished:
            self.cards.append(card)

    def close(self) -> None:
        super().close()
        self.cards.extend(self._active)
        self._active.clear()


def parse_comment_cards(html: str) -> list[dict]:
    """Parse loaded comment cards into person/company author records.

    Each result carries the comment URN, author name/profile/headline and
    author_type. Company pages are returned with author_type='company' so the
    list builder can exclude them from its people-only output.
    """
    parser = _CommentCardParser()
    parser.feed(html or "")
    parser.close()
    out: list[dict] = []
    seen: set[str] = set()
    for card in parser.cards:
        component_key = card["component_key"]
        urn_match = _COMMENT_URN.search(component_key)
        comment_urn = urn_match.group(0) if urn_match else ""
        if comment_urn and comment_urn in seen:
            continue
        if comment_urn:
            seen.add(comment_urn)

        links = []
        for anchor in card["anchors"]:
            url, kind = _profile_url(anchor["href"])
            if url:
                links.append((url, kind, " ".join(anchor["text"])))
        if not links:
            continue
        profile_url, author_type, _ = links[0]
        anchor_text = next((link[2] for link in links
                            if link[0] == profile_url and link[2].strip()), "")
        name, headline = _name_and_headline(anchor_text)
        if not name:
            continue
        out.append({
            "comment_urn": comment_urn,
            "author": name,
            "profile_url": profile_url,
            "headline": headline,
            "author_type": author_type,
            "comment_text": " ".join(card["text"]),
        })
    return out


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def _parse_reactor_label(label: str) -> dict:
    name, remainder = label.split(_REACTED, 1)
    parts = remainder.split(", ", 2)
    reaction = parts[0].strip()
    relation = parts[1].strip() if len(parts) > 1 else ""
    headline = parts[2].strip() if len(parts) > 2 else ""
    return {
        "author": name.strip(),
        "reaction_type": reaction,
        "relation": relation,
        "headline": headline,
        "is_self": relation.casefold() == "you",
    }


def parse_reactors_flight(flight: str) -> list[dict]:
    """Extract reactor people and reaction labels from a paginated RSC flight."""
    out: list[dict] = []
    for chunk in parse_flight(flight or ""):
        labels = []
        for obj in _walk_flight(chunk):
            if isinstance(obj, dict):
                label = obj.get("aria-label")
                if isinstance(label, str) and _REACTED in label:
                    labels.append(label)
        if not labels:
            continue
        urls = []
        for value in _walk_strings(chunk):
            url, kind = _profile_url(value)
            if url and kind == "person" and url not in urls:
                urls.append(url)
        for index, label in enumerate(labels):
            row = _parse_reactor_label(label)
            if len(urls) == len(labels):
                row["profile_url"] = urls[index]
            elif len(urls) == 1:
                row["profile_url"] = urls[0]
            else:
                row["profile_url"] = ""
            row["author_type"] = "person" if row["profile_url"] else "unknown"
            if row["author"] and row["profile_url"]:
                out.append(row)
    return out
