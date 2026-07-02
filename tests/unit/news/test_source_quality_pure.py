from uuid import uuid4

from upmovies.news.models import Story
from upmovies.news.source_quality import (
    best_tier,
    collect_domain_samples,
    domain_for_story,
    downgrade_confidence,
    effective_tier,
    normalize_domain,
)


def test_normalize_domain_strips_subdomain_and_www():
    assert normalize_domain("https://www.mshale.com/a/b?x=1") == "mshale.com"
    assert normalize_domain("http://m.mshale.com/amp/story") == "mshale.com"


def test_normalize_domain_handles_multipart_tld():
    assert normalize_domain("https://news.bbc.co.uk/story") == "bbc.co.uk"


def test_normalize_domain_none_or_hostless():
    assert normalize_domain(None) is None
    assert normalize_domain("not a url") is None


def test_domain_for_story_prefers_resolved():
    d = domain_for_story(
        url="https://news.google.com/rss/articles/XYZ", resolved_url="https://variety.com/x"
    )
    assert d == "variety.com"


def test_domain_for_story_unresolved_google_is_none():
    # An unresolved Google redirect must NOT be tagged as google.com.
    d = domain_for_story(url="https://news.google.com/rss/articles/XYZ", resolved_url=None)
    assert d is None


def test_domain_for_story_direct_publisher():
    assert domain_for_story(url="https://deadline.com/x", resolved_url=None) == "deadline.com"


def test_effective_tier_override_wins():
    assert (
        effective_tier(llm_tier="low", admin_override="trust", unresolved_default="acceptable")
        == "trusted"
    )
    assert (
        effective_tier(llm_tier="trusted", admin_override="block", unresolved_default="acceptable")
        == "blocked"
    )
    assert (
        effective_tier(llm_tier="trusted", admin_override="allow", unresolved_default="acceptable")
        == "acceptable"
    )


def test_effective_tier_none_override_uses_llm_tier():
    assert (
        effective_tier(llm_tier="low", admin_override="none", unresolved_default="acceptable")
        == "low"
    )


def test_effective_tier_missing_llm_tier_uses_default():
    assert (
        effective_tier(llm_tier=None, admin_override="none", unresolved_default="acceptable")
        == "acceptable"
    )


def test_best_tier_picks_highest_and_ignores_blocked():
    assert best_tier(["low", "acceptable", "low"], default="acceptable") == "acceptable"
    assert best_tier(["low", "blocked"], default="acceptable") == "low"
    assert best_tier([], default="acceptable") == "acceptable"


def test_downgrade_confidence_only_when_all_low():
    assert downgrade_confidence("confirmed", "low") == "rumored"
    assert downgrade_confidence("confirmed", "acceptable") == "confirmed"
    assert downgrade_confidence("rumored", "trusted") == "rumored"


def _story(url, resolved_url, title):
    return Story(id=uuid4(), source="X", url=url, resolved_url=resolved_url, title=title, raw={})


def test_collect_domain_samples_maps_domain_to_first_headline():
    stories = [
        _story("https://news.google.com/rss/x", "https://www.variety.com/a", "Variety one"),
        _story("https://news.google.com/rss/y", "https://variety.com/b", "Variety two"),
        _story("https://www.mshale.com/z", None, "Mshale one"),
        _story("https://news.google.com/rss/articles/unresolved", None, "no domain"),
    ]
    out = collect_domain_samples(stories)
    assert out == {"variety.com": "Variety one", "mshale.com": "Mshale one"}
