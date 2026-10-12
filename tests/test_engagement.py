from linkedin_listener.engagement import (
    build_reactors_payload,
    extract_next_page_token,
    parse_comment_cards,
    parse_reactors_flight,
)


def test_parse_comment_cards_extracts_people_and_company_accounts():
    html = """
    <div componentkey="CommentComponentReference_urn:li:comment:(activity:123,456)">
      <a href="https://www.linkedin.com/in/alex-rivera/">
        <p>Alex Rivera Verified Profile 2nd</p><p>Alex Rivera</p>
        <p>Growth Lead @Acme</p>
      </a>
      <p>Great post</p>
    </div>
    <div componentkey="CommentComponentReference_urn:li:comment:(activity:123,789)">
      <a href="https://www.linkedin.com/company/acme/">
        <p>Acme Company</p><p>1,200 followers</p>
      </a>
      <p>Thanks for sharing</p>
    </div>
    """

    rows = parse_comment_cards(html)
    assert len(rows) == 2
    assert rows[0]["author"] == "Alex Rivera"
    assert rows[0]["profile_url"] == "https://www.linkedin.com/in/alex-rivera"
    assert rows[0]["headline"] == "Growth Lead @Acme"
    assert rows[0]["author_type"] == "person"
    assert rows[1]["author_type"] == "company"


def test_parse_reactors_flight_associates_name_reaction_and_profile():
    flight = (
        '1:{"aria-label":"Alex Rivera reacted with Like, 1st degree connection, '
        'Growth Lead @Acme","profileUrl":"https://www.linkedin.com/in/alex-rivera/"}\n'
        '2:{"aria-label":"Sam Lee reacted with Love, 2nd degree connection, CEO",'
        '"profileUrl":"https://www.linkedin.com/in/sam-lee/"}\n'
    )

    rows = parse_reactors_flight(flight)
    assert len(rows) == 2
    assert rows[0]["author"] == "Alex Rivera"
    assert rows[0]["reaction_type"] == "Like"
    assert rows[0]["headline"] == "Growth Lead @Acme"
    assert rows[0]["profile_url"] == "https://www.linkedin.com/in/alex-rivera"
    assert rows[1]["is_self"] is False


def test_reactors_payload_carries_page_token_in_both_argument_sets():
    body = build_reactors_payload("123456", page_token="cursor")
    assert body["pagerId"].endswith("reactorsListByTypeScreen")
    assert body["clientArguments"]["payload"]["pageToken"] == "cursor"
    assert body["paginationRequest"]["requestedArguments"]["payload"]["pageToken"] == "cursor"
    assert body["clientArguments"]["requestedStateKeys"] == []


def test_extract_next_page_token_handles_escaped_flight_payload():
    assert extract_next_page_token('0:{"pageToken":"plain"}') == "plain"
    assert extract_next_page_token(r'0:"{\"pageToken\":\"opaque\"}"') == "opaque"
    assert extract_next_page_token('0:{"pageToken":""}') is None
