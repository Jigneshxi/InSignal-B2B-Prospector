from linkedin_listener.rsc import (
    build_search_payload,
    extract_post_refs,
    flight_post_urls,
    parse_flight,
)


def test_build_search_payload_carries_the_full_facet_schema():
    p = build_search_payload("cold email", start=0, count=10)
    assert p["pagerId"] == "com.linkedin.sdui.search.contentSearchResults"
    payload = p["clientArguments"]["payload"]
    for k in ("keywords", "startIndex", "count", "sortBy", "postedBy", "datePosted",
              "contentType", "fromMember", "mentionsOrganization", "mentionsMember",
              "fromOrganization", "authorCompany", "authorIndustry", "authorJobTitle",
              "spellCheckEnabled", "searchId"):
        assert k in payload, k
    assert payload["keywords"] == "cold email"
    assert payload["sortBy"] == []
    assert p["paginationRequest"]["requestedArguments"]["payload"]["keywords"] == "cold email"


def test_build_search_payload_accepts_facets():
    p = build_search_payload("gtm", datePosted="past-month", authorIndustry=["4"],
                             sortBy=["date_posted"])
    payload = p["clientArguments"]["payload"]
    assert payload["datePosted"] == ["past-month"]
    assert payload["authorIndustry"] == ["4"]
    assert payload["sortBy"] == ["date_posted"]


def test_payload_arguments_match_the_captured_pagination_shape():
    payload = build_search_payload("GTM Engineering", start=3, count=3, search_id="x")
    assert set(payload) == {"pagerId", "clientArguments", "paginationRequest"}
    assert set(payload["clientArguments"]) == {
        "$type", "requestedStateKeys", "payload", "requestMetadata", "states",
        "screenId", "knownTemplateIds",
    }
    pagination = payload["paginationRequest"]
    assert set(pagination) == {
        "$type", "pagerId", "trigger", "retryCount", "requestedArguments",
    }
    assert set(pagination["requestedArguments"]) == {
        "$type", "requestedStateKeys", "payload", "requestMetadata",
    }


def test_parse_flight_extracts_unique_post_refs_in_first_seen_order():
    flight = (
        '1:{"authorName":"Test Author","activity":"urn:li:activity:7300000000000000001"}\n'
        '2:{"authorName":"Second Test Author","activity":"urn:li:activity:7300000000000000002"}\n'
        '3:{"activity":"urn:li:activity:7300000000000000001"}\n'
    )
    refs = extract_post_refs(flight)
    assert len(refs) == 2
    assert all(r["native_id"].isdigit() and len(r["native_id"]) >= 15 for r in refs)
    assert [r["native_id"] for r in refs] == [
        "7300000000000000001", "7300000000000000002",
    ]
    assert refs[0]["author"] == "Test Author"


def test_flight_post_urls_are_canonical_permalinks():
    text = '0:{"a":"urn:li:activity:7301410127406841856","b":"urn:li:activity:7301410127406841856"}'
    urls = flight_post_urls(text)
    assert urls == ["https://www.linkedin.com/feed/update/urn:li:activity:7301410127406841856/"]


def test_parse_flight_skips_garbage():
    assert parse_flight('1:I["abc",[],"Mod"]\n2:{"a":1}\n6:string\n') == [{"a": 1}]
