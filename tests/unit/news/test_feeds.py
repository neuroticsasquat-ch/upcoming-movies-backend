from upmovies.news.feeds import feed_sources, is_google_source, per_film_google_sources

# The curated in-code trade feeds (NEU-717 expanded the set from 4 to 8).
_TRADE_NAMES = {
    "Deadline",
    "Variety",
    "The Hollywood Reporter",
    "ScreenRant",
    "IndieWire",
    "Filmmaker Magazine",
    "The Wrap",
    "Screen Daily",
}


def test_google_queries_carry_the_when_operator_when_enabled():
    google = [s for s in feed_sources(14, google_enabled=True) if s.name.startswith("Google News")]
    assert len(google) == 4
    # quote() encodes "when:14d" as "when%3A14d"
    assert all("when%3A14d" in s.url for s in google)


def test_no_google_queries_when_disabled():
    sources = feed_sources(14, google_enabled=False)
    assert not any(is_google_source(s.name) for s in sources)
    # Only the curated trade feeds remain.
    assert {s.name for s in sources} == _TRADE_NAMES


def test_trade_feeds_returned_in_both_modes():
    off = {s.name for s in feed_sources(14, google_enabled=False)}
    on = {s.name for s in feed_sources(14, google_enabled=True)}
    assert _TRADE_NAMES <= off
    assert _TRADE_NAMES <= on


def test_trade_feeds_are_returned_unchanged_without_a_when_operator():
    sources = feed_sources(14, google_enabled=True)
    deadline = next(s for s in sources if s.name == "Deadline")
    assert "when" not in deadline.url


def test_curated_trade_feed_urls():
    by_name = {s.name: s.url for s in feed_sources(14, google_enabled=False)}
    assert by_name["Deadline"] == "https://deadline.com/v/film/feed/"
    assert by_name["Variety"] == "https://variety.com/v/film/feed/"
    assert by_name["The Hollywood Reporter"] == "https://www.hollywoodreporter.com/c/movies/feed/"
    assert by_name["ScreenRant"] == "https://screenrant.com/feed/"
    # NEU-717 additions (verified live to return parseable RSS).
    assert by_name["IndieWire"] == "https://www.indiewire.com/c/news/feed/"
    assert by_name["Filmmaker Magazine"] == "https://filmmakermagazine.com/feed/"
    assert by_name["The Wrap"] == "https://www.thewrap.com/creative-content/movies/feed/"
    assert by_name["Screen Daily"] == "https://www.screendaily.com/45187.rss"


def test_per_film_sources_one_per_title_unquoted_with_when():
    sources = per_film_google_sources(["A Film", "Other Movie"], 14)
    assert len(sources) == 2
    assert all(s.name == "Google News: per-film" for s in sources)
    # quote() encodes "when:14d" as "when%3A14d"; unquoted => no encoded quote char
    assert all("when%3A14d" in s.url for s in sources)
    assert all("%22" not in s.url for s in sources)


def test_per_film_sources_empty_roster_is_empty():
    assert per_film_google_sources([], 14) == ()


def test_is_google_source_matches_all_google_labels():
    assert is_google_source("Google News: per-film") is True
    assert is_google_source("Google News: casting") is True
    assert is_google_source("Google News: release date") is True
    assert is_google_source("Google News: trailer") is True
    assert is_google_source("Google News: greenlight") is True


def test_is_google_source_false_for_trade_feeds():
    for name in _TRADE_NAMES:
        assert is_google_source(name) is False
