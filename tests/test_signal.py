import json
import tempfile
from pathlib import Path

from linkedin_listener.signal import (
    SignalConfigError,
    SignalStore,
    deliver_webhooks,
    load_config,
    query_signature,
)


def test_load_config_normalizes_watches_and_facets():
    with tempfile.TemporaryDirectory(prefix="listener-signal-") as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps({
            "watches": [{
                "id": "cold-email",
                "keywords": " Cold email ",
                "facets": {"authorIndustry": [4, "8"]},
            }],
        }), encoding="utf-8")
        watches = load_config(path)
    assert watches == [{
        "id": "cold-email",
        "keywords": "Cold email",
        "facets": {"authorIndustry": ["4", "8"]},
        "max_age_days": 7,
    }]


def test_config_rejects_unknown_facets_and_duplicate_ids():
    with tempfile.TemporaryDirectory(prefix="listener-signal-") as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps({"watches": [
            {"id": "one", "keywords": "alpha", "facets": {"unknown": "x"}},
        ]}), encoding="utf-8")
        try:
            load_config(path)
        except SignalConfigError as exc:
            assert "unsupported search facet" in str(exc)
        else:
            raise AssertionError("expected invalid facet config to fail")

        path.write_text(json.dumps({"watches": [
            {"id": "same", "keywords": "alpha"},
            {"id": "same", "keywords": "beta"},
        ]}), encoding="utf-8")
        try:
            load_config(path)
        except SignalConfigError as exc:
            assert "duplicate watch id" in str(exc)
        else:
            raise AssertionError("expected duplicate watch ids to fail")


def test_query_signature_changes_when_watch_definition_changes():
    first = {"id": "watch", "keywords": "Cold email", "facets": {}, "max_age_days": 7}
    changed = {**first, "facets": {"authorIndustry": ["4"]}}
    assert query_signature(first) != query_signature(changed)


def test_seen_ledger_and_outbox_are_idempotent_and_jsonl_is_a_snapshot():
    with tempfile.TemporaryDirectory(prefix="listener-signal-") as directory:
        root = Path(directory)
        with SignalStore(root / "signals.sqlite3") as store:
            event = store.enqueue_signal("cold-email", "sig", "123", {
                "event_type": "linkedin.post.discovered",
                "post": {"native_id": "123"},
            })
            assert event is not None
            assert store.enqueue_signal("cold-email", "sig", "123", {"post": {}}) is None
            assert len(store.pending_webhooks()) == 1

            assert store.mark_baseline("cold-email", "sig", "456") is True
            assert store.mark_baseline("cold-email", "sig", "456") is False

            output = root / "events.jsonl"
            assert store.export_jsonl(output) == 1
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            assert [row["event_id"] for row in rows] == [event["event_id"]]


def test_webhook_delivery_marks_outbox_success(monkeypatch):
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("linkedin_listener.signal.urlopen", fake_urlopen)
    with (tempfile.TemporaryDirectory(prefix="listener-signal-") as directory,
          SignalStore(Path(directory) / "signals.sqlite3") as store):
        event = store.enqueue_signal("watch", "sig", "activity-1", {
            "event_type": "linkedin.post.discovered",
            "post": {"native_id": "activity-1"},
        })
        assert event
        delivered, failed = deliver_webhooks(store, "https://hooks.example.test/ingest")
        assert (delivered, failed) == (1, 0)
        assert captured["request"].get_header("Idempotency-key") == event["event_id"]
        assert store.pending_webhooks() == []
