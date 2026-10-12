from types import SimpleNamespace

import pytest

from linkedin_listener.cli import _effective_max_age, _parse_facet_args


def test_parse_facet_args_accepts_repeated_keys_and_multiple_values():
    facets = _parse_facet_args([
        "authorIndustry=4,8",
        "sortBy=date_posted",
        "authorIndustry=4",
    ])
    assert facets == {"authorIndustry": ["4", "8"], "sortBy": ["date_posted"]}


def test_parse_facet_args_rejects_unknown_keys_and_empty_values():
    with pytest.raises(ValueError, match="supported keys"):
        _parse_facet_args(["notAFacet=value"])
    with pytest.raises(ValueError, match="needs at least one value"):
        _parse_facet_args(["datePosted="])


def test_default_exact_age_respects_an_explicit_linkedin_date_facet():
    assert _effective_max_age(SimpleNamespace(max_age_days=None), {}) == 7
    assert _effective_max_age(SimpleNamespace(max_age_days=None),
                              {"datePosted": ["past-month"]}) == 0
    assert _effective_max_age(SimpleNamespace(max_age_days=14),
                              {"datePosted": ["past-month"]}) == 14
