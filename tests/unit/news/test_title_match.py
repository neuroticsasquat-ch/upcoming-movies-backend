from upmovies.news.title_match import title_matches


def test_full_title_match_is_kept():
    assert (
        title_matches(
            "Spider-Man: Brand New Day",
            "Spider-Man: Brand New Day wraps filming",
            min_ratio=0.5,
        )
        is True
    )


def test_partial_below_threshold_is_dropped():
    # title significant tokens: spider, man, brand, new, day (5)
    # headline shares only new, day -> 2/5 = 0.4 < 0.5
    assert (
        title_matches(
            "Spider-Man: Brand New Day",
            "A new day dawns at the bakery",
            min_ratio=0.5,
        )
        is False
    )


def test_whole_word_boundary_no_substring_match():
    # "war" must not match "warehouse"
    assert title_matches("War", "Warehouse fire downtown", min_ratio=0.5) is False
    assert title_matches("War", "This means war for the studio", min_ratio=0.5) is True


def test_stopwords_are_ignored():
    # significant title tokens: girl, clouds
    assert (
        title_matches("The Girl in the Clouds", "Girl in clouds gets a release date", min_ratio=0.5)
        is True
    )
    assert (
        title_matches("The Girl in the Clouds", "The release of the film", min_ratio=0.5) is False
    )


def test_no_significant_tokens_is_kept():
    # all-stopword / too-short title -> nothing to assess -> keep (high recall)
    assert title_matches("A", "totally unrelated headline", min_ratio=0.5) is True


def test_non_ascii_title_matches_non_ascii_headline():
    assert title_matches("Касса невест", "Касса невест выходит в прокат", min_ratio=0.5) is True
    assert (
        title_matches("Касса невест", "Stock futures rise on Wall Street", min_ratio=0.5) is False
    )


def test_numbered_sequel_keeps_on_topic_drops_generic():
    assert (
        title_matches(
            "How to Train Your Dragon 2", "How to Train Your Dragon 2 soars", min_ratio=0.5
        )
        is True
    )
    assert (
        title_matches(
            "How to Train Your Dragon 2", "Dragon boats race down the river", min_ratio=0.5
        )
        is False
    )


def test_curated_recall_rows_are_kept():
    # Real 'about' headline patterns must survive the filter (the recall guard).
    assert (
        title_matches(
            "Avengers: Doomsday",
            (
                "Famke Janssen Thinks Marvel Made A Mistake Not Bringing Her Back For "
                "'Avengers: Doomsday'"
            ),
            min_ratio=0.5,
        )
        is True
    )
    assert (
        title_matches("Love and War", "First Look At 'Love and War' Revealed", min_ratio=0.5)
        is True
    )
