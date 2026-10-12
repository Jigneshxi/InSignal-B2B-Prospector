import tempfile
from pathlib import Path

from linkedin_listener.export import merge_people, verify_people_csv, write_people


def test_merge_people_dedupes_profiles_and_preserves_engagement_sources():
    rows = [
        {
            "person_name": "Alex Rivera",
            "profile_url": "https://www.linkedin.com/in/alex-rivera/",
            "headline": "Growth Lead @Acme",
            "engagement_type": "posted",
            "source_post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
            "keyword": "GTM",
        },
        {
            "person_name": "Alex Rivera",
            "profile_url": "https://linkedin.com/in/alex-rivera",
            "engagement_type": "commented",
            "source_post_url": "https://www.linkedin.com/feed/update/urn:li:activity:2/",
            "keyword": "GTM",
        },
        {
            "person_name": "Alex Rivera",
            "profile_url": "https://www.linkedin.com/in/alex-rivera/",
            "engagement_type": "reacted",
            "reaction_type": "Like",
            "source_post_url": "https://www.linkedin.com/feed/update/urn:li:activity:2/",
            "keyword": "GTM",
        },
        {
            "person_name": "Company page",
            "profile_url": "https://www.linkedin.com/company/acme/",
            "engagement_type": "commented",
        },
    ]

    people, duplicate_events, invalid_events = merge_people(rows)
    assert len(people) == 1
    assert duplicate_events == 2
    assert invalid_events == 1
    assert people[0]["engagement_types"] == "posted; commented; reacted"
    assert people[0]["reaction_types"] == "Like"
    assert people[0]["post_count"] == 2
    assert people[0]["source_keywords"] == "GTM"


def test_final_csv_is_reread_and_verified():
    people, _, _ = merge_people([{
        "person_name": "Alex Rivera",
        "profile_url": "https://www.linkedin.com/in/alex-rivera/",
        "engagement_type": "posted",
        "source_post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
    }])
    with tempfile.TemporaryDirectory(prefix="linkedin-listener-") as directory:
        output = Path(directory) / "people.csv"
        write_people(people, str(output))
        assert verify_people_csv(str(output)) == 1


def test_merge_people_strips_linkedin_badges_and_duplicate_display_text():
    rows = [
        {
            "person_name": "Cynthia Barnes Executive Top Voice Profile 2nd Cynthia Barnes",
            "profile_url": "https://www.linkedin.com/in/cynthia-barnes/",
            "engagement_type": "commented",
        },
        {
            "person_name": "Hamza Ali, Open to work 3rd+ Hamza Ali • 3rd+",
            "profile_url": "https://www.linkedin.com/in/hamza-ali/",
            "engagement_type": "reacted",
        },
    ]
    people, duplicates, invalid = merge_people(rows)
    assert [row["person_name"] for row in people] == ["Cynthia Barnes", "Hamza Ali"]
    assert duplicates == 0
    assert invalid == 0
