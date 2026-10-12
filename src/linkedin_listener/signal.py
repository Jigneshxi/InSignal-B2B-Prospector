"""Cron-friendly, first-seen LinkedIn post signal monitor.

Each invocation performs one polling pass. SQLite records seen posts and stores
an event outbox; an optional webhook and JSONL snapshot make the signals easy to
route into the operator's stack. Changes to engagement on an already-seen post
are deliberately outside this first signal pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .cli import LinkedInThrottled, collect_post_events, search_post_refs
from .export import merge_people
from .rsc import SEARCH_FACETS
from .session import LinkedInSession
from .ssr import parse_ssr_results

EVENT_TYPE = "linkedin.post.discovered"
_WATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SignalConfigError(ValueError):
    """Invalid monitor configuration."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_facets(value) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SignalConfigError("watch facets must be an object")
    result: dict[str, list[str]] = {}
    for key, raw_values in value.items():
        if key not in SEARCH_FACETS:
            raise SignalConfigError(f"unsupported search facet: {key}")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        cleaned = []
        for raw in values:
            if not isinstance(raw, (str, int)):
                raise SignalConfigError(f"values for facet {key} must be strings or integers")
            item = str(raw).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        if not cleaned:
            raise SignalConfigError(f"facet {key} must have at least one value")
        result[key] = cleaned
    return result


def load_config(path: str | Path) -> list[dict]:
    """Load and validate a JSON file containing keyword and hashtag watches."""
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SignalConfigError(f"could not read signal config ({type(exc).__name__})") from None
    if not isinstance(config, dict) or not isinstance(config.get("watches"), list):
        raise SignalConfigError("config must contain a watches array")

    watches = []
    seen_ids = set()
    for index, raw in enumerate(config["watches"], start=1):
        if not isinstance(raw, dict):
            raise SignalConfigError(f"watch {index} must be an object")
        if raw.get("enabled", True) is False:
            continue
        watch_id = raw.get("id")
        keywords = raw.get("keywords")
        if not isinstance(watch_id, str) or not _WATCH_ID.fullmatch(watch_id):
            raise SignalConfigError(
                f"watch {index} needs an id containing letters, digits, dot, underscore or hyphen"
            )
        if watch_id in seen_ids:
            raise SignalConfigError(f"duplicate watch id: {watch_id}")
        seen_ids.add(watch_id)
        if not isinstance(keywords, str) or not keywords.strip():
            raise SignalConfigError(f"watch {watch_id} needs non-empty keywords or a hashtag")
        facets = normalize_facets(raw.get("facets"))
        max_age = raw.get("max_age_days")
        if max_age is None:
            max_age = 0 if "datePosted" in facets else 7
        if not isinstance(max_age, int) or max_age < 0:
            raise SignalConfigError(f"watch {watch_id} max_age_days must be a non-negative integer")
        watches.append({
            "id": watch_id,
            "keywords": keywords.strip(),
            "facets": facets,
            "max_age_days": max_age,
        })
    if not watches:
        raise SignalConfigError("config contains no enabled watches")
    return watches


def query_signature(watch: dict) -> str:
    """Stable fingerprint so edits to a watch's query start a new dedupe scope."""
    source = json.dumps({
        "keywords": watch["keywords"],
        "facets": watch["facets"],
        "max_age_days": watch["max_age_days"],
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _event_id(watch_id: str, signature: str, native_id: str) -> str:
    source = f"{watch_id}\0{signature}\0{native_id}\0{EVENT_TYPE}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class SignalStore:
    """SQLite seen-post ledger and durable webhook outbox."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS seen_posts (
                watch_id TEXT NOT NULL,
                query_signature TEXT NOT NULL,
                native_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (watch_id, query_signature, native_id)
            );
            CREATE TABLE IF NOT EXISTS signal_events (
                event_id TEXT PRIMARY KEY,
                watch_id TEXT NOT NULL,
                query_signature TEXT NOT NULL,
                native_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                webhook_attempts INTEGER NOT NULL DEFAULT 0,
                webhook_delivered_at TEXT,
                webhook_last_error TEXT,
                UNIQUE (watch_id, query_signature, native_id, event_type)
            );
            CREATE INDEX IF NOT EXISTS signal_events_pending_webhook
                ON signal_events (webhook_delivered_at, detected_at);
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def is_seen(self, watch_id: str, signature: str, native_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM seen_posts WHERE watch_id=? AND query_signature=? AND native_id=?",
            (watch_id, signature, native_id),
        ).fetchone()
        return row is not None

    def mark_baseline(self, watch_id: str, signature: str, native_id: str) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO seen_posts "
            "(watch_id, query_signature, native_id, first_seen_at) VALUES (?, ?, ?, ?)",
            (watch_id, signature, native_id, utc_now()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def enqueue_signal(self, watch_id: str, signature: str, native_id: str,
                       payload: dict) -> dict | None:
        """Atomically mark a post seen and persist its first-seen signal event."""
        detected_at = payload.get("detected_at") or utc_now()
        event_id = _event_id(watch_id, signature, native_id)
        event = dict(payload)
        event["event_id"] = event_id
        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO seen_posts "
                "(watch_id, query_signature, native_id, first_seen_at) VALUES (?, ?, ?, ?)",
                (watch_id, signature, native_id, detected_at),
            )
            if cursor.rowcount != 1:
                return None
            self.connection.execute(
                "INSERT INTO signal_events "
                "(event_id, watch_id, query_signature, native_id, event_type, detected_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, watch_id, signature, native_id, EVENT_TYPE, detected_at, serialized),
            )
        return event

    def pending_webhooks(self, limit: int = 1000) -> list[dict]:
        rows = self.connection.execute(
            "SELECT event_id, payload_json FROM signal_events "
            "WHERE webhook_delivered_at IS NULL ORDER BY detected_at, event_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"event_id": row["event_id"], "payload": json.loads(row["payload_json"])}
                for row in rows]

    def mark_webhook_result(self, event_id: str, error_code: str | None = None) -> None:
        delivered_at = utc_now() if error_code is None else None
        self.connection.execute(
            "UPDATE signal_events SET webhook_attempts=webhook_attempts+1, "
            "webhook_delivered_at=COALESCE(?, webhook_delivered_at), webhook_last_error=? "
            "WHERE event_id=?",
            (delivered_at, error_code, event_id),
        )
        self.connection.commit()

    def export_jsonl(self, path: str | Path) -> int:
        """Atomically replace the JSONL file with a deduped event snapshot."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.connection.execute(
            "SELECT payload_json FROM signal_events ORDER BY detected_at, event_id"
        ).fetchall()
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                for row in rows:
                    fh.write(row["payload_json"])
                    fh.write("\n")
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return len(rows)


def _webhook_url(env_name: str) -> str:
    if not _ENV_NAME.fullmatch(env_name):
        raise SignalConfigError("webhook environment variable name is invalid")
    value = os.environ.get(env_name, "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SignalConfigError(f"webhook URL in {env_name} must use HTTP or HTTPS")
    return value


def deliver_webhooks(store: SignalStore, webhook_url: str, timeout: float = 20) -> tuple[int, int]:
    """Deliver pending outbox entries. The URL is never logged or stored."""
    delivered = 0
    failed = 0
    for item in store.pending_webhooks():
        event_id = item["event_id"]
        request = Request(
            webhook_url,
            data=json.dumps(item["payload"], ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": event_id,
            },
            method="POST",
        )
        error_code = None
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                error_code = f"HTTP_{status}"
        except HTTPError as exc:
            error_code = f"HTTP_{exc.code}"
        except URLError:
            error_code = "URL_ERROR"
        except TimeoutError:
            error_code = "TIMEOUT"
        except Exception as exc:
            error_code = type(exc).__name__
        store.mark_webhook_result(event_id, error_code)
        if error_code is None:
            delivered += 1
        else:
            failed += 1
    return delivered, failed


def _signal_payload(watch: dict, ref: dict, person_events: list[dict],
                    page, partial: bool) -> dict:
    contacts, duplicate_events, invalid_events = merge_people(person_events)
    post: dict = {}
    try:
        cards = parse_ssr_results(page.content())
        post = next((row for row in cards if row.get("native_id") == ref["native_id"]), {})
    except Exception:
        post = {}

    posted_at = ref.get("posted_at")
    if hasattr(posted_at, "isoformat"):
        posted_at = posted_at.isoformat()
    elif posted_at is not None:
        posted_at = str(posted_at)
    return {
        "schema_version": 1,
        "event_type": EVENT_TYPE,
        "detected_at": utc_now(),
        "watch": {
            "id": watch["id"],
            "keywords": watch["keywords"],
            "facets": watch["facets"],
            "max_age_days": watch["max_age_days"],
        },
        "post": {
            "native_id": ref["native_id"],
            "urn": ref.get("urn", ""),
            "url": ref["post_url"],
            "posted_at": posted_at,
            "author": post.get("author") or ref.get("author", ""),
            "author_profile_url": post.get("profile_url", ""),
            "headline": post.get("headline", ""),
            "text": post.get("post_text", ""),
        },
        "people": contacts,
        "collection": {
            "partial": partial,
            "duplicate_contact_events_merged": duplicate_events,
            "invalid_contact_events_dropped": invalid_events,
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin-signal-monitor",
        description="Run one LinkedIn first-seen-post signal polling pass.",
    )
    parser.add_argument("--config", required=True, help="JSON file containing keyword/hashtag watches")
    parser.add_argument("--state-db", default="output/linkedin_signals.sqlite3",
                        help="SQLite seen-post ledger and event database")
    parser.add_argument("--jsonl-out", default="output/linkedin_signals.jsonl",
                        help="atomic JSONL event snapshot path")
    parser.add_argument("--no-jsonl", action="store_true", help="skip the JSONL snapshot")
    parser.add_argument("--webhook-env", default="LINKEDIN_SIGNAL_WEBHOOK_URL",
                        help="environment variable containing the webhook URL")
    parser.add_argument("--baseline-only", action="store_true",
                        help="record current matches as seen without emitting events")
    parser.add_argument("--profile-dir", default=os.environ.get("LI_PROFILE_DIR", ".li-profile"),
                        help="persistent LinkedIn Chrome profile")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="run without a visible Chrome window (default: headless)")
    parser.add_argument("--search-page-size", type=int, default=3)
    parser.add_argument("--max-search-pages", type=int, default=0,
                        help="0 follows search pagination until exhausted")
    parser.add_argument("--max-posts-per-watch", type=int, default=0,
                        help="test limit per watch; 0 processes all matches")
    parser.add_argument("--max-reactor-pages", type=int, default=0,
                        help="reactor pages per new post; 0 follows all page tokens")
    parser.add_argument("--max-comment-pages", type=int, default=0,
                        help="comment-load actions per new post; 0 follows until exhausted")
    parser.add_argument("--request-delay", type=float, default=20.0,
                        help="pause between search/engagement requests")
    return parser


def run_monitor(args) -> int:
    try:
        watches = load_config(args.config)
        webhook = _webhook_url(args.webhook_env)
    except SignalConfigError as exc:
        print(f"signal config error: {exc}", file=sys.stderr)
        return 2
    for name in ("search_page_size", "max_search_pages", "max_posts_per_watch",
                 "max_reactor_pages", "max_comment_pages"):
        if getattr(args, name) < (1 if name == "search_page_size" else 0):
            print(f"--{name.replace('_', '-')} has an invalid value", file=sys.stderr)
            return 2
    if args.request_delay < 0:
        print("--request-delay cannot be negative", file=sys.stderr)
        return 2

    search_args = SimpleNamespace(
        search_page_size=args.search_page_size,
        max_search_pages=args.max_search_pages,
        max_posts=args.max_posts_per_watch,
        max_reactor_pages=args.max_reactor_pages,
        max_comment_pages=args.max_comment_pages,
        request_delay=args.request_delay,
    )
    counts = {"matches": 0, "new": 0, "seen": 0, "baseline": 0}
    was_throttled = False
    with SignalStore(args.state_db) as store:
        try:
            with LinkedInSession(args.profile_dir, headless=args.headless) as seat:
                seat.verify_session()
                for watch in watches:
                    signature = query_signature(watch)
                    print(f"watch {watch['id']}: searching {watch['keywords']}", file=sys.stderr)
                    try:
                        refs = search_post_refs(
                            seat,
                            watch["keywords"],
                            search_args,
                            watch["facets"],
                            watch["max_age_days"],
                        )
                    except LinkedInThrottled:
                        print("stopping after LinkedIn returned HTTP 429", file=sys.stderr)
                        was_throttled = True
                        break
                    counts["matches"] += len(refs)
                    watch_new = 0
                    watch_seen = 0
                    for ref in refs:
                        native_id = ref.get("native_id", "")
                        if not native_id:
                            continue
                        if store.is_seen(watch["id"], signature, native_id):
                            counts["seen"] += 1
                            watch_seen += 1
                            continue
                        if args.baseline_only:
                            if store.mark_baseline(watch["id"], signature, native_id):
                                counts["baseline"] += 1
                            continue
                        try:
                            person_events, throttled = collect_post_events(seat, ref, search_args)
                        except Exception as exc:
                            print(f"  post collection failed ({type(exc).__name__}); "
                                  "will retry next run", file=sys.stderr)
                            continue
                        payload = _signal_payload(watch, ref, person_events, seat.page, throttled)
                        event = store.enqueue_signal(watch["id"], signature, native_id, payload)
                        if event is not None:
                            counts["new"] += 1
                            watch_new += 1
                        if args.request_delay > 0:
                            time.sleep(args.request_delay)
                        if throttled:
                            print("stopping after HTTP 429; queued partial event", file=sys.stderr)
                            was_throttled = True
                            break
                    print(f"  matches={len(refs)} already_seen={watch_seen} "
                          f"new_events={watch_new}", file=sys.stderr)
                    if was_throttled:
                        break
        except KeyboardInterrupt:
            print("stopped by user; stored signals remain available", file=sys.stderr)
            was_throttled = True
        except Exception as exc:
            print(f"signal run failed ({type(exc).__name__})", file=sys.stderr)
            return 1

        if not args.no_jsonl:
            try:
                jsonl_count = store.export_jsonl(args.jsonl_out)
            except OSError as exc:
                print(f"JSONL export failed ({type(exc).__name__})", file=sys.stderr)
                return 1
        else:
            jsonl_count = None

        delivered = failed = 0
        if webhook:
            delivered, failed = deliver_webhooks(store, webhook)

    print(f"signal pass complete: matches={counts['matches']} new_events={counts['new']} "
          f"already_seen={counts['seen']} baseline_recorded={counts['baseline']}")
    print(f"SQLite event database: {args.state_db}")
    if jsonl_count is not None:
        print(f"JSONL snapshot: {args.jsonl_out} ({jsonl_count} events)")
    if webhook:
        print(f"webhook deliveries={delivered} failures={failed}")
    else:
        print(f"webhook not configured; set the {args.webhook_env} environment variable to enable it")
    if failed:
        print("failed webhook events remain queued for retry", file=sys.stderr)
        return 1
    return 3 if was_throttled else 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return run_monitor(build_arg_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
