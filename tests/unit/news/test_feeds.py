from upmovies.news.feeds import feed_sources, per_film_google_sources


def test_google_queries_carry_the_when_operator():
    google = [s for s in feed_sources(14) if s.name.startswith("Google News")]
    assert len(google) == 4
    # quote() encodes "when:14d" as "when%3A14d"
    assert all("when%3A14d" in s.url for s in google)


def test_trade_feeds_are_returned_unchanged_without_a_when_operator():
    sources = feed_sources(14)
    deadline = next(s for s in sources if s.name == "Deadline")
    assert deadline.url == "https://deadline.com/feed/"
    assert "when" not in deadline.url


def test_per_film_sources_one_per_title_unquoted_with_when():
    sources = per_film_google_sources(["A Film", "Other Movie"], 14)
    assert len(sources) == 2
    assert all(s.name == "Google News: per-film" for s in sources)
    # quote() encodes "when:14d" as "when%3A14d"; unquoted => no encoded quote char
    assert all("when%3A14d" in s.url for s in sources)
    assert all("%22" not in s.url for s in sources)


def test_per_film_sources_empty_roster_is_empty():
    assert per_film_google_sources([], 14) == ()
