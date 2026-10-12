import os

import pytest

from linkedin_listener.reader import flatten, parse_date, parse_ldjson, read_post

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "li_post_guest.html")

SYNTHETIC = """<html><head>
<script type="application/ld+json">{"@context":"http://schema.org","@type":"VideoObject","@id":"https://www.linkedin.com/posts/testuser_how-activity-7301410127406841856-AG5A","datePublished":"2025-03-01T01:17:07.993Z","headline":"how to write a cold email","description":"full post text here","commentCount":3,"creator":{"@type":"Person","name":"Test User","url":"https://www.linkedin.com/in/testuser"},"interactionStatistic":[{"@type":"InteractionCounter","interactionType":"http://schema.org/LikeAction","userInteractionCount":51},{"@type":"InteractionCounter","interactionType":"https://schema.org/CommentAction","userInteractionCount":3}],"comment":[{"@type":"Comment","datePublished":"2025-03-01T11:48:38.631Z","text":"first comment text","creator":{"@type":"Person","name":"Commenter One"}}]}</script>
</head></html>"""


def _fixture_html():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


def test_parse_ldjson_synthetic():
    obj = parse_ldjson(SYNTHETIC)
    assert obj["@type"] == "VideoObject"
    assert obj["datePublished"] == "2025-03-01T01:17:07.993Z"
    assert obj["creator"]["url"].endswith("/in/testuser")


def test_parse_ldjson_returns_empty_when_absent():
    assert parse_ldjson("<html><body>authwall page</body></html>") == {}
    assert parse_ldjson("") == {}


def test_flatten_maps_all_row_fields():
    obj = parse_ldjson(SYNTHETIC)
    row = flatten(obj)
    assert row["post_url"].startswith("https://www.linkedin.com/posts/testuser_")
    assert row["author_name"] == "Test User"
    assert row["author_profile"].endswith("/in/testuser")
    assert row["posted_at"] == "2025-03-01T01:17:07.993Z"
    assert row["reactions"] == 51
    assert row["comment_count"] == 3
    assert row["post_text"] == "full post text here"
    assert len(row["comments"]) == 1
    assert row["comments"][0]["author"] == "Commenter One"


def test_parse_date_handles_iso_z():
    row = flatten(parse_ldjson(SYNTHETIC))
    d = parse_date(row)
    assert d is not None and d.year == 2025 and d.tzinfo is not None


def test_parse_date_bad_value_is_none():
    assert parse_date({"posted_at": "not-a-date"}) is None
    assert parse_date({}) is None


class FakeResponse:
    def __init__(self, status, text, url="https://www.linkedin.com/posts/x/"):
        self.status_code = status
        self.text = text
        self.url = url


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=30, cookies=None):
        self.calls.append(url)
        return self.response


def test_read_post_public_page():
    s = FakeSession(FakeResponse(200, SYNTHETIC))
    row = read_post(s, "https://www.linkedin.com/posts/testuser_how-activity-1-AG5A/")
    assert row is not None
    assert row["author_name"] == "Test User"
    assert len(s.calls) == 1


def test_read_post_login_wall_returns_none():
    s = FakeSession(FakeResponse(200, "<html>authwall</html>",
                                 url="https://www.linkedin.com/uas/login?session_redirect=x"))
    assert read_post(s, "https://www.linkedin.com/posts/x/") is None


def test_read_post_error_returns_none():
    class Boom:
        def get(self, url, timeout=30):
            raise ConnectionError("nope")

    assert read_post(Boom(), "https://www.linkedin.com/posts/x/") is None


@pytest.mark.skipif(not os.path.exists(FIXTURE), reason="real-capture fixture is local-only")
def test_parse_ldjson_real_capture():
    obj = parse_ldjson(_fixture_html())
    assert obj["@type"] == "VideoObject"
    assert obj["datePublished"] == "2025-03-01T01:17:07.993Z"
    assert obj["creator"]["name"] == "Fatimah Hussain"
    assert len(obj["comment"]) == 3
    row = flatten(obj)
    assert row["reactions"] == 51
    assert row["author_profile"].endswith("/in/fatimah-hussain")
