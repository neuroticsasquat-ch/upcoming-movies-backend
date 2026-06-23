from upmovies.news.outlet import outlet_from_entry, outlet_from_title


def test_outlet_from_title_takes_trailing_segment():
    assert outlet_from_title("Big Movie Casts a Star - Deadline") == "Deadline"


def test_outlet_from_title_uses_last_separator():
    assert outlet_from_title("Dune: Part Three - Everything We Know - Variety") == "Variety"


def test_outlet_from_title_none_without_separator():
    assert outlet_from_title("No separator in this headline") is None


def test_outlet_from_title_none_on_empty_trailing_segment():
    assert outlet_from_title("Trailing dash - ") is None


def test_outlet_from_entry_prefers_source_element_over_title():
    entry = {"source": {"title": "The Hollywood Reporter"}, "title": "Headline - Wrong Name"}
    assert outlet_from_entry(entry) == "The Hollywood Reporter"


def test_outlet_from_entry_strips_whitespace_from_source():
    entry = {"source": {"title": "  Collider  "}}
    assert outlet_from_entry(entry) == "Collider"


def test_outlet_from_entry_falls_back_to_title_suffix_when_source_empty():
    entry = {"source": {}, "title": "Sequel Greenlit - Variety"}
    assert outlet_from_entry(entry) == "Variety"


def test_outlet_from_entry_uses_title_when_source_missing():
    entry = {"title": "Casting News - Deadline"}
    assert outlet_from_entry(entry) == "Deadline"


def test_outlet_from_entry_none_when_unresolvable():
    entry = {"source": {}, "title": "No outlet anywhere"}
    assert outlet_from_entry(entry) is None
