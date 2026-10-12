"""Command line interface.

  linkedin-listener --login                 # one-time interactive sign-in (persistent seat)
  linkedin-listener --check                 # session status
  linkedin-listener listen "GTM Engineering" --out people.csv
  linkedin-listener listen "#brightonseo" --max-age-days 7
  linkedin-listener read https://www.linkedin.com/posts/<slug>-activity-<id>/

The session lives in a persistent Chrome profile (default .li-profile). The
listener replays LinkedIn's content-search operation with its captured facets,
follows result pages, reads each post in the seat, collects commenters and
reactors, then merges people by LinkedIn profile URL into a CSV.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from curl_cffi import requests as cr

from .engagement import (
    build_reactors_payload,
    extract_next_page_token,
    parse_comment_cards,
    parse_reactors_flight,
)
from .export import merge_people, verify_people_csv, write_people
from .rsc import OP_SEARCH, SEARCH_FACETS, build_search_payload, extract_post_refs, post_rsc_action
from .session import LinkedInSession
from .ssr import filter_days, parse_ssr_results


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="linkedin-listener",
        description="Build a LinkedIn people list from keyword-post authors, "
        "commenters and reactors using the site's own search and engagement endpoints.",
    )
    p.add_argument("--login", action="store_true", help="sign in once; the session persists in the profile")
    p.add_argument("--check", action="store_true", help="print the session status and exit")
    p.add_argument("keywords", nargs="*", help="keyword(s) or #hashtags to listen for")
    p.add_argument("--out", default="people.csv", help="output people CSV (default people.csv)")
    p.add_argument("--max-age-days", type=int, default=None,
                   help="exact local age cutoff; defaults to 7 days unless --facet datePosted is supplied")
    p.add_argument("--facet", action="append", default=[], metavar="KEY=VALUE",
                   help="LinkedIn content-search facet; repeat for multiple values, e.g. --facet authorIndustry=4")
    p.add_argument("--search-page-size", type=int, default=3,
                   help="content-search results per endpoint page (default 3, matching the captured request)")
    p.add_argument("--max-search-pages", type=int, default=0,
                   help="stop after N search pages; 0 follows pagination until no new posts")
    p.add_argument("--max-posts", type=int, default=0,
                   help="maximum posts to process per keyword; 0 processes every matching post")
    p.add_argument("--max-reactor-pages", type=int, default=0,
                   help="reactor pages per post; 0 follows page tokens until exhausted")
    p.add_argument("--max-comment-pages", type=int, default=0,
                   help="maximum UI comment-load actions per post; 0 follows until no new comments")
    p.add_argument("--request-delay", type=float, default=20.0,
                   help="seconds between endpoint calls while collecting engagements")
    p.add_argument("--delay", type=float, default=20.0,
                   help="seconds between keyword searches (default 20)")
    p.add_argument("--headless", action="store_true", help="run the seat Chrome headless where possible")
    p.add_argument("--profile-dir", default=os.environ.get("LI_PROFILE_DIR", ".li-profile"),
                   help="persistent Chrome profile dir (default .li-profile)")
    return p


def _date_facet(max_age_days: int) -> str:
    """LinkedIn's own datePosted facet tokens."""
    if max_age_days <= 1:
        return "past-24h"
    if max_age_days <= 7:
        return "past-week"
    if max_age_days <= 30:
        return "past-month"
    return ""


class LinkedInThrottled(RuntimeError):
    """A live endpoint returned 429; stop making requests on this seat."""


def _parse_facet_args(values: list[str]) -> dict[str, list[str]]:
    facets: dict[str, list[str]] = {}
    for raw in values:
        key, separator, value = raw.partition("=")
        key = key.strip()
        if not separator or key not in SEARCH_FACETS:
            allowed = ", ".join(SEARCH_FACETS)
            raise ValueError(f"invalid facet {raw!r}; supported keys: {allowed}")
        facet_values = [part.strip() for part in value.split(",") if part.strip()]
        if not facet_values:
            raise ValueError(f"facet {key!r} needs at least one value")
        target = facets.setdefault(key, [])
        for item in facet_values:
            if item not in target:
                target.append(item)
    return facets


def _effective_max_age(args, facets: dict[str, list[str]]) -> int:
    if args.max_age_days is not None:
        if args.max_age_days < 0:
            raise ValueError("--max-age-days cannot be negative")
        return args.max_age_days
    return 0 if "datePosted" in facets else 7


def _search_url(keywords: str, date_facet: str) -> str:
    params = {"keywords": keywords, "origin": "FACETED_SEARCH"}
    if date_facet:
        params["datePosted"] = date_facet
    return "https://www.linkedin.com/search/results/content/?" + urlencode(params)


def _native_post_datetime(native_id: str) -> datetime | None:
    try:
        timestamp_ms = int(native_id) >> 22
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def search_post_refs(seat, keywords: str, args, facets: dict[str, list[str]],
                     max_age_days: int) -> list[dict]:
    """Search through the captured contentSearchResults RSC endpoint and follow pages."""
    page = seat.page
    query_facets = {key: list(values) for key, values in facets.items()}
    date_facet = (query_facets.get("datePosted") or
                  ([_date_facet(max_age_days)] if max_age_days > 0 else []))
    if date_facet and "datePosted" not in query_facets:
        query_facets["datePosted"] = date_facet
    landing = page.goto(_search_url(keywords, date_facet[0] if date_facet else ""),
                        wait_until="domcontentloaded", timeout=60000)
    if landing and landing.status == 429:
        raise LinkedInThrottled("content search page returned HTTP 429")
    if landing and landing.status >= 400:
        raise RuntimeError(f"content search page returned HTTP {landing.status}")
    page.wait_for_timeout(7000)

    search_id = str(uuid.uuid4())
    seen_ids: set[str] = set()
    refs_out: list[dict] = []
    start = 0
    page_no = 0
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days) if max_age_days else None
    while True:
        body = build_search_payload(
            keywords,
            start=start,
            count=args.search_page_size,
            search_id=search_id,
            cluster_start_position=max(0, start - 1),
            **query_facets,
        )
        response = post_rsc_action(page, "pagination", OP_SEARCH, body)
        status = response.get("status", 0)
        if status == 429:
            raise LinkedInThrottled("content search returned HTTP 429")
        if status != 200:
            raise RuntimeError(f"content search returned HTTP {status}")

        refs = extract_post_refs(response.get("text", ""))
        page_no += 1
        new_discovered = 0
        for ref in refs:
            native_id = ref.get("native_id", "")
            if not native_id or native_id in seen_ids:
                continue
            seen_ids.add(native_id)
            new_discovered += 1
            posted_at = _native_post_datetime(native_id)
            if cutoff and (posted_at is None or posted_at < cutoff):
                continue
            ref.update({
                "post_url": f"https://www.linkedin.com/feed/update/urn:li:activity:{native_id}/",
                "posted_at": posted_at,
                "keyword": keywords,
            })
            refs_out.append(ref)
            if args.max_posts and len(refs_out) >= args.max_posts:
                break

        print(f"  search page {page_no}: refs={len(refs)} new={new_discovered} "
              f"in-window={len(refs_out)}", file=sys.stderr)
        if args.max_posts and len(refs_out) >= args.max_posts:
            break
        if not refs or not new_discovered:
            break
        if args.max_search_pages and page_no >= args.max_search_pages:
            print(f"  reached --max-search-pages={args.max_search_pages}", file=sys.stderr)
            break
        start += args.search_page_size
        if args.request_delay > 0:
            time.sleep(args.request_delay)
    return refs_out


def _is_person_profile(url: str) -> bool:
    parsed = urlsplit(url or "")
    return (parsed.hostname in {"linkedin.com", "www.linkedin.com"}
            and parsed.path.rstrip("/").lower().startswith("/in/"))


def _select_recent_comments(page) -> int | None:
    """Use the post page's own sort control to call fetchComment correctly."""
    try:
        state = page.evaluate("""() => {
          const label = [...document.querySelectorAll('p')].find((el) => {
            const text = el.textContent.trim();
            return (text === 'Most relevant' || text === 'Most recent')
              && el.nextElementSibling?.id === 'caret-small';
          });
          if (!label || label.textContent.trim() === 'Most recent') return 'already-recent';
          label.parentElement.click();
          return 'opened';
        }""")
        if state != "opened":
            return None
        page.wait_for_timeout(300)
        options = page.get_by_text("Most recent", exact=True)
        if options.count() == 0:
            return None
        with page.expect_response(
            lambda response: "com.linkedin.sdui.feed.update.comments.fetchComment" in response.url,
            timeout=20000,
        ) as response_info:
            options.last.click(timeout=5000)
        page.wait_for_timeout(700)
        return response_info.value.status
    except Exception as exc:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            if isinstance(exc, PlaywrightTimeoutError):
                return None
        except ImportError:
            pass
        print(f"  comment-sort action skipped ({type(exc).__name__})", file=sys.stderr)
        return None


def _load_more_comments(page, max_pages: int, request_delay: float) -> int | None:
    """Click visible comment-pagination controls; return 429 if the seat is limited."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    more = re.compile(
        r"\b(?:load|show|see|view)\s+(?:more|previous|older|all)\s+comments?\b"
        r"|\b(?:see|view)\s+\d+\s+more\s+comments?\b"
        r"|\b(?:more|previous|older)\s+comments?\b",
        re.IGNORECASE,
    )
    page_no = 0
    while max_pages <= 0 or page_no < max_pages:
        buttons = page.get_by_role("button", name=more)
        if buttons.count() == 0:
            break
        before = page.locator('[componentkey^="CommentComponentReference_urn:li:comment:"]').count()
        try:
            with page.expect_response(
                lambda response: "com.linkedin.sdui.feed.update.comments.fetchComment" in response.url,
                timeout=20000,
            ) as response_info:
                buttons.first.click(timeout=5000)
            status = response_info.value.status
            if status == 429:
                return status
            if status != 200:
                print(f"  more-comments request returned HTTP {status}", file=sys.stderr)
                break
            page.wait_for_timeout(700)
            page_no += 1
            after = page.locator('[componentkey^="CommentComponentReference_urn:li:comment:"]').count()
            if after <= before:
                break
            if request_delay > 0:
                time.sleep(request_delay)
        except PlaywrightTimeoutError:
            break
        except Exception as exc:
            print(f"  more-comments action stopped ({type(exc).__name__})", file=sys.stderr)
            break
    return None


def collect_post_events(seat, ref: dict, args) -> tuple[list[dict], bool]:
    """Read one post page, its visible commenters and every reactor page."""
    page = seat.page
    activity_id = ref["native_id"]
    post_url = ref["post_url"]
    events: list[dict] = []
    try:
        navigation = page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
        if navigation and navigation.status == 429:
            return events, True
        if navigation and navigation.status >= 400:
            print(f"  post page returned HTTP {navigation.status}: {post_url}", file=sys.stderr)
            return events, False
        page.wait_for_timeout(5000)
        html = page.content()
    except Exception as exc:
        print(f"  post page failed ({type(exc).__name__}): {post_url}", file=sys.stderr)
        return events, False

    posts = parse_ssr_results(html)
    post = next((row for row in posts if row.get("native_id") == activity_id),
                posts[0] if posts else {})
    if post.get("author_type") == "person" and _is_person_profile(post.get("profile_url", "")):
        events.append({
            "person_name": post.get("author", ""),
            "profile_url": post.get("profile_url", ""),
            "headline": post.get("headline", ""),
            "engagement_type": "posted",
            "source_post_url": post_url,
            "keyword": ref.get("keyword", ""),
        })

    comment_status = _select_recent_comments(page)
    throttled = comment_status == 429
    if comment_status is not None:
        print(f"    fetchComment: HTTP {comment_status}", file=sys.stderr)
    if comment_status not in (None, 200):
        print(f"  fetchComment returned HTTP {comment_status}; using rendered comments",
              file=sys.stderr)
    if not throttled:
        more_status = _load_more_comments(page, args.max_comment_pages, args.request_delay)
        throttled = more_status == 429
    comments = parse_comment_cards(page.content())
    person_comments = [comment for comment in comments
                       if comment.get("author_type") == "person"
                       and _is_person_profile(comment.get("profile_url", ""))]
    print(f"    commenters: {len(person_comments)} rendered", file=sys.stderr)
    for comment in person_comments:
        events.append({
            "person_name": comment.get("author", ""),
            "profile_url": comment.get("profile_url", ""),
            "headline": comment.get("headline", ""),
            "engagement_type": "commented",
            "source_post_url": post_url,
            "keyword": ref.get("keyword", ""),
        })
    if throttled:
        return events, True

    operation = "com.linkedin.sdui.impl.social.components.reactorsListByTypeScreen"
    page_token = None
    seen_tokens: set[str] = set()
    page_no = 0
    while True:
        body = build_reactors_payload(activity_id, page_token=page_token)
        try:
            response = post_rsc_action(page, "pagination", operation, body)
        except Exception as exc:
            print(f"  reactors request failed ({type(exc).__name__}) for {activity_id}",
                  file=sys.stderr)
            break
        status = response.get("status", 0)
        if status == 429:
            return events, True
        if status != 200:
            print(f"  reactors request returned HTTP {status} for {activity_id}", file=sys.stderr)
            break
        page_no += 1
        reactors = parse_reactors_flight(response.get("text", ""))
        for reactor in reactors:
            if reactor.get("is_self") or not _is_person_profile(reactor.get("profile_url", "")):
                continue
            events.append({
                "person_name": reactor.get("author", ""),
                "profile_url": reactor.get("profile_url", ""),
                "headline": reactor.get("headline", ""),
                "engagement_type": "reacted",
                "reaction_type": reactor.get("reaction_type", ""),
                "source_post_url": post_url,
                "keyword": ref.get("keyword", ""),
            })
        print(f"    reactors page {page_no}: {len(reactors)} parsed", file=sys.stderr)
        page_token = extract_next_page_token(response.get("text", ""))
        if args.max_reactor_pages and page_no >= args.max_reactor_pages:
            if page_token:
                print(f"    reactor page cap reached at {args.max_reactor_pages}", file=sys.stderr)
            break
        if not page_token or page_token in seen_tokens:
            break
        seen_tokens.add(page_token)
        if args.request_delay > 0:
            time.sleep(args.request_delay)
    return events, False


def checkpoint_people(events: list[dict], path: str) -> tuple[int, int, int]:
    """Write and re-verify a clean checkpoint of all collected person events."""
    people, duplicate_events, invalid_events = merge_people(events)
    write_people(people, path)
    verified_rows = verify_people_csv(path)
    if verified_rows != len(people):
        raise RuntimeError("checkpoint row count changed during CSV verification")
    return verified_rows, duplicate_events, invalid_events


def harvest_all_cards(seat, keywords: str, args, max_pages: int = 10) -> list[dict]:
    """Thorough harvest: deep-scroll page 1 until stable, then walk every Next
    page, harvesting cards at each stabilization point. Merged + deduped by
    native_id (falls back to author+text when a card carries no urn)."""
    page = seat.page
    max_age_days = args.max_age_days if args.max_age_days is not None else 7
    facet = _date_facet(max_age_days)
    kw = keywords.replace(" ", "+")
    url = ("https://www.linkedin.com/search/results/content/"
           f"?keywords={kw}&origin=FACETED_SEARCH")
    if facet:
        url += f"&datePosted={facet}"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)

    def count_cards() -> int:
        try:
            return page.evaluate(
                "() => document.querySelectorAll('[data-testid=\"expandable-text-box\"]').length")
        except Exception:
            return 0

    all_rows: list[dict] = []
    seen_ids: set[str] = set()
    total_new = 0
    for page_no in range(1, max_pages + 1):
        stable, last = 0, count_cards()
        while stable < 2 and last < 200:
            page.mouse.wheel(0, 2600)
            page.wait_for_timeout(1800)
            cur = count_cards()
            stable = stable + 1 if cur == last else 0
            last = cur
        html = page.content()
        rows = parse_ssr_results(html)
        new = 0
        for r in rows:
            key = r.get("native_id") or (r.get("author", "") + "|" + r.get("post_text", "")[:80])
            if key in seen_ids:
                continue
            seen_ids.add(key)
            all_rows.append(r)
            new += 1
        print(f"  page {page_no}: cards={last} new={new} total={len(all_rows)}", file=sys.stderr)
        total_new += new
        clicked = False
        try:
            nxt = page.get_by_role("button", name="Next")
            if nxt.count() > 0:
                nxt.last.click(timeout=8000)
                clicked = True
                page.wait_for_timeout(6000)
        except Exception:
            pass
        if not clicked or total_new == 0:
            break
    return all_rows


def copy_post_links(seat, expected: int) -> list[str]:
    """Per result card: open the post menu, hit 'Copy link to post', read the
    clipboard. Returns the share links in card order."""
    page = seat.page
    links: list[str] = []
    menus = page.query_selector_all('button[aria-label^="Open control menu for post by"]')
    for mb in menus[:expected]:
        try:
            mb.click(timeout=6000)
            time.sleep(1.5)
            item = page.get_by_text("Copy link to post", exact=False)
            if item.count() == 0:
                item = page.get_by_text("Copy link", exact=False)
            if item.count() == 0:
                links.append("")
                page.keyboard.press("Escape")
                continue
            item.first.click()
            time.sleep(1.5)
            link = page.evaluate("() => navigator.clipboard.readText()")
            links.append((link or "").strip())
            page.keyboard.press("Escape")
            time.sleep(0.8)
        except Exception as e:
            print(f"  copy-link error: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
            links.append("")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return links


def resolve_links(session, links: list[str]) -> list[str]:
    """lnkd.in share links -> canonical post permalinks (follow redirects)."""
    out = []
    for l in links:
        if not l:
            out.append("")
            continue
        if "lnkd.in" not in l:
            out.append(l)
            continue
        try:
            r = session.get(l, timeout=20, allow_redirects=True)
            out.append(r.url.split("?")[0] if r.status_code == 200 else l)
        except Exception:
            out.append(l)
        time.sleep(0.5)
    return out


def search_rows(seat, http, keywords: str, args) -> list[dict]:
    """One content search: thorough harvest (deep scroll + all Next pages),
    post links via copy-link, date window applied."""
    rows = harvest_all_cards(seat, keywords, args)
    links = copy_post_links(seat, len(rows))
    resolved = resolve_links(http, links)
    for r, link, full in zip(rows, links, resolved):
        r["share_link"] = link
        r["post_url"] = full or f"urn:{r.get('native_id', '')}"
    max_age_days = args.max_age_days if args.max_age_days is not None else 7
    return filter_days(rows, max_age_days) if max_age_days > 0 else rows


def listen(args) -> int:
    try:
        facets = _parse_facet_args(args.facet)
        max_age_days = _effective_max_age(args, facets)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.search_page_size < 1:
        print("--search-page-size must be at least 1", file=sys.stderr)
        return 2
    if args.request_delay < 0:
        print("--request-delay cannot be negative", file=sys.stderr)
        return 2
    for option in ("delay", "max_search_pages", "max_posts", "max_reactor_pages",
                   "max_comment_pages"):
        if getattr(args, option) < 0:
            print(f"--{option.replace('_', '-')} cannot be negative", file=sys.stderr)
            return 2

    all_events: list[dict] = []
    stop_for_throttle = False
    stopped_by_user = False
    try:
        with LinkedInSession(args.profile_dir, headless=args.headless) as seat:
            seat.verify_session()
            for kw in args.keywords:
                print(f"searching: {kw}", file=sys.stderr)
                try:
                    refs = search_post_refs(seat, kw, args, facets, max_age_days)
                except LinkedInThrottled as exc:
                    print(f"stopping after throttle: {exc}", file=sys.stderr)
                    stop_for_throttle = True
                    break
                except Exception as e:
                    print(f"  search failed ({type(e).__name__}: {str(e)[:160]})", file=sys.stderr)
                    continue
                print(f"  {len(refs)} matching posts", file=sys.stderr)
                for index, ref in enumerate(refs, start=1):
                    print(f"  post {index}/{len(refs)}: {ref['native_id']}", file=sys.stderr)
                    try:
                        events, throttled = collect_post_events(seat, ref, args)
                    except Exception as exc:
                        print(f"    engagement collection failed ({type(exc).__name__}: "
                              f"{str(exc)[:120]})", file=sys.stderr)
                        events, throttled = [], False
                    all_events.extend(events)
                    checkpoint_people(all_events, args.out)
                    if throttled:
                        print("stopping after HTTP 429; keeping collected results", file=sys.stderr)
                        stop_for_throttle = True
                        break
                    if args.request_delay > 0:
                        time.sleep(args.request_delay)
                if stop_for_throttle:
                    break
                if args.delay > 0:
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        print("stopped by user; saving the collected results", file=sys.stderr)
        stopped_by_user = True

    people, dropped_dupes, invalid_events = merge_people(all_events)
    write_people(people, args.out)
    n = verify_people_csv(args.out)
    print(f"done. engagement_events={len(all_events)} duplicate_events_merged={dropped_dupes} "
          f"invalid_events_dropped={invalid_events} unique_people={n} -> {args.out}")
    if stop_for_throttle:
        print("output is partial because the seat returned HTTP 429", file=sys.stderr)
    return 130 if stopped_by_user else 0


def read_one(args) -> int:
    from .reader import read_post
    session = cr.Session(impersonate="chrome")
    row = read_post(session, args.url, log=lambda m: print(m, file=sys.stderr))
    if row is None:
        print("could not read that post (non-public or fetch failed)")
        return 1
    for k in ("post_url", "author_name", "author_profile", "posted_at",
              "reactions", "comment_count"):
        print(f"{k}: {row.get(k, '')}")
    print(f"text: {(row.get('post_text') or '')[:300]}")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_arg_parser().parse_args(argv)
    kws = list(args.keywords)
    if kws and kws[0] == "listen":
        kws = kws[1:]  # accept the README-style `listen` prefix
    if args.login or args.check:
        ok = False
        with LinkedInSession(args.profile_dir, headless=args.headless) as seat:
            if args.login:
                try:
                    seat.verify_session()
                    print("session already live")
                except Exception:
                    seat.login()
                ok = True
            st = seat.session_status()
            print(f"session status: {st}")
            ok = ok or st == 200
        return 0 if ok else 1
    if len(kws) == 1 and kws[0].startswith("http"):
        args.url = kws[0]
        return read_one(args)
    if not kws:
        print("give at least one keyword, or --login to start", file=sys.stderr)
        return 1
    args.keywords = kws
    return listen(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
