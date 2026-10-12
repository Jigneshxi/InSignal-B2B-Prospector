"""Direct calls to LinkedIn's content-search endpoints.

The logged-in web app is Server-Driven UI: keyword search runs through

    POST /flagship-web/rsc-action/actions/pagination
        ?sduiid=com.linkedin.sdui.search.contentSearchResults

with a JSON proto-action body (the full facet schema: keywords, sortBy,
datePosted, postedBy, contentType, author filters, ...) and answers with a
React Server Components flight stream. This module builds that request,
parses the flight, and extracts post references.

The production listener sends this action from inside the persistent browser
seat, which supplies the authenticated browser context. Plain-HTTP replay
helpers remain available for endpoint probes and offline-compatible workflows.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from importlib import resources
from urllib.parse import quote

BASE = "https://www.linkedin.com"
CARRIER = BASE + "/flagship-web/rsc-action/actions/pagination"
INITIAL_URL = BASE + "/flagship-web/search/results/content/"
OP_SEARCH = "com.linkedin.sdui.search.contentSearchResults"
SEARCH_SCREEN = "com.linkedin.sdui.flagshipnav.search.SearchResultsContent"
SEARCH_PAGE_URL = (BASE + "/search/results/content/"
                   "?keywords={keywords}&origin=FACETED_SEARCH")
SEARCH_FACETS = (
    "sortBy", "postedBy", "datePosted", "contentType", "fromMember",
    "fromOrganization", "mentionsMember", "mentionsOrganization",
    "authorCompany", "authorIndustry", "authorJobTitle",
)

_LINE = re.compile(r"^([0-9A-Za-z]+):(.*)$", re.MULTILINE)
_ACTIVITY = re.compile(r"urn:li:(?:activity|ugcPost):(\d{15,25})")


# ---------------------------------------------------------------- payload ----

def build_search_payload(keywords: str, start: int = 0, count: int = 3,
                         search_id: str | None = None,
                         cluster_start_position: int | None = None, **facets) -> dict:
    """The contentSearchResults request body.

    Facet schema as captured: sortBy, postedBy, datePosted, contentType,
    fromMember, mentionsOrganization, mentionsMember, fromOrganization,
    authorCompany, authorIndustry, authorJobTitle. Values are lists; recognized
    facet kwargs replace the corresponding default values."""
    payload = {
        "startIndex": start,
        "keywords": keywords,
        "count": count,
        "sortBy": [],
        "postedBy": [],
        "datePosted": [],
        "contentType": [],
        "fromMember": [],
        "mentionsOrganization": [],
        "mentionsMember": [],
        "fromOrganization": [],
        "authorCompany": [],
        "authorIndustry": [],
        "authorJobTitle": [],
        "spellCheckEnabled": True,
        "clusterStartPosition": (max(0, start - 1) if cluster_start_position is None
                                 else cluster_start_position),
        "searchId": search_id or str(uuid.uuid4()),
    }
    for k, v in facets.items():
        if k in payload and v is not None:
            payload[k] = v if isinstance(v, list) else [v]

    def args(payload_obj):
        return {"$type": "proto.sdui.actions.requests.RequestedArguments",
                "requestedStateKeys": [], "payload": payload_obj,
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"}}

    client_args = args(payload)
    client_args.update({
        "states": [],
        "screenId": SEARCH_SCREEN,
        "knownTemplateIds": [],
    })
    return {
        "pagerId": OP_SEARCH,
        "clientArguments": client_args,
        "paginationRequest": {
            "$type": "proto.sdui.actions.requests.PaginationRequest",
            "pagerId": OP_SEARCH,
            "trigger": {"$case": "itemDistanceTrigger",
                        "itemDistanceTrigger": {"$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                                                "preloadDistance": 3, "preloadLength": 1500}},
            "retryCount": 2,
            "requestedArguments": args(dict(payload)),
        },
    }


_PAGE_POST_JS = """async ({path, body}) => {
  const jsid = (document.cookie.match(/JSESSIONID=\\\"?([^;\\\"]+)/) || [])[1] || '';
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'accept': 'application/json, text/plain, */*',
      'content-type': 'application/json',
      'csrf-token': jsid,
      'x-restli-protocol-version': '2.0.0',
      'x-li-lang': 'en_US'
    },
    body: JSON.stringify(body)
  });
  return {status: response.status, text: await response.text()};
}"""


def post_rsc_action(page, carrier: str, operation: str, body: dict) -> dict:
    """POST an RSC action from the authenticated LinkedIn page context.

    Keeping the request inside the persistent seat supplies LinkedIn's ambient
    browser/session context. Returns {status, text} without interpreting flight.
    """
    if carrier not in {"pagination", "server-request"}:
        raise ValueError(f"unsupported RSC carrier: {carrier}")
    path = f"/flagship-web/rsc-action/actions/{carrier}?sduiid={operation}"
    return page.evaluate(_PAGE_POST_JS, {"path": path, "body": body})


# ------------------------------------------------------------------ flight ----

def parse_flight(text: str) -> list:
    """Parse an RSC flight stream into its JSON chunks (best effort)."""
    chunks = []
    for m in _LINE.finditer(text):
        v = m.group(2).strip()
        if not v or v[0] not in "{[":
            continue
        try:
            chunks.append(json.loads(v))
        except (ValueError, TypeError):
            continue
    return chunks


def _walk(obj):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def extract_post_refs(text: str) -> list[dict]:
    """Collect unique post references (activity/ugcPost URNs) from a flight
    stream, in first-seen order. Author metadata is enriched from parsed JSON
    chunks when the surrounding entity offered it."""
    author_by_id: dict[str, str] = {}
    author_keys = ("authorName", "author", "actorName", "fullName")
    for chunk in parse_flight(text):
        for o in _walk(chunk):
            if not isinstance(o, dict):
                continue
            vals = list(o.values())
            author = next((o[ak] for ak in author_keys
                           if isinstance(o.get(ak), str) and o.get(ak)), "")
            if not author:
                continue
            for v in vals:
                if isinstance(v, str):
                    m = _ACTIVITY.search(v)
                    if m and m.group(1) not in author_by_id:
                        author_by_id[m.group(1)] = author

    out, seen = [], set()
    for m in _ACTIVITY.finditer(text):
        nid = m.group(1)
        if nid in seen:
            continue
        seen.add(nid)
        out.append({"native_id": nid, "urn": m.group(0), "author": author_by_id.get(nid, "")})
    return out


def flight_post_urls(text: str) -> list[str]:
    """Convenience: canonical /feed/update/ permalinks for every post in a flight."""
    seen, out = set(), []
    for m in _ACTIVITY.finditer(text):
        nid = m.group(1)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(f"{BASE}/feed/update/urn:li:activity:{nid}/")
    return out


# ------------------------------------------------------------------ replay ----

def build_initial_action(keywords: str) -> dict:
    """The NavigateToScreen action that opens the content-search screen for a
    keyword (template captured from the live app; keyword + cache id swapped)."""
    s = (resources.files("linkedin_listener")
         .joinpath("data/initial_search_action.json").read_text("utf-8"))
    plus = quote(keywords).replace("%20", "+")
    s = s.replace("{keywords}", keywords).replace("{keywords_plus}", plus)
    s = s.replace("{cache_id}", str(uuid.uuid4()))
    return json.loads(s)


def replay_initial_search(session, cookies: dict, keywords: str):
    """Plain-HTTP replay of the initial content search. Returns (status, flight)."""
    action = build_initial_action(keywords)
    jsid = (cookies.get("JSESSIONID", "") or "").strip('"')
    headers = {
        "accept": "application/json, text/plain, */*",
        "csrf-token": jsid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "content-type": "application/json",
    }
    r = session.post(INITIAL_URL, json=action, cookies=cookies, headers=headers, timeout=60)
    return r.status_code, (r.text if r.status_code == 200 else "")


def replay_search(session, cookies: dict, keywords: str, start: int = 0,
                  count: int = 10, **facets):
    """Plain-HTTP replay of the contentSearchResults operation.

    session is a curl_cffi-compatible session (TLS impersonation). Returns
    (status, flight_text)."""
    payload = build_search_payload(keywords, start=start, count=count, **facets)
    jsid = (cookies.get("JSESSIONID", "") or "").strip('"')
    headers = {
        "accept": "application/json, text/plain, */*",
        "csrf-token": jsid,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "content-type": "application/json",
    }
    url = f"{CARRIER}?sduiid={OP_SEARCH}"
    r = session.post(url, json=payload, cookies=cookies, headers=headers, timeout=30)
    return r.status_code, (r.text if r.status_code == 200 else "")


def capture_search_via_page(page, keywords: str) -> str:
    """Fallback: drive the session's own page to the search URL and capture the
    flight response the page fires. Returns the flight text ('' on failure)."""
    captured = {}

    def on_resp(r):
        if "rsc-action" in r.url and r.status == 200 and "flight" not in captured:
            try:
                body = r.text()
                if body and len(body) > 5000:
                    captured["flight"] = body
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(SEARCH_PAGE_URL.format(keywords=keywords),
                  wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        for _ in range(2):
            page.mouse.wheel(0, 2400)
            time.sleep(2)
    except Exception:
        pass
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
    return captured.get("flight", "")
