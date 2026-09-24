"""`film_parenthetical` (NEU-1460, DC-4): the digest's film header spells the feed row's
parenthetical exactly as the frontend's `filmParenthetical` does.

These are the nine fixtures of `describe("filmParenthetical")` in the frontend's
`lib/format.test.ts`, ported verbatim. If one of them changes there, it changes here: the two
are one rule written twice, and a reader who sees a film in the mail and on the feed must read
the same words."""

import pytest

from upmovies.app.services.digest_sender import film_parenthetical

NINE_COUNTRIES = [
    "Canada",
    "Colombia",
    "France",
    "Mexico",
    "Netherlands",
    "Switzerland",
    "Thailand",
    "UK",
    "USA",
]


def _parenthetical(
    *,
    production_countries: list[str] | None = None,
    directors: list[str] | None = None,
    release_year: int | None = None,
    arc_stage: str = "announced",
) -> str:
    return film_parenthetical(
        production_countries=production_countries or [],
        directors=directors or [],
        release_year=release_year,
        arc_stage=arc_stage,
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {
                "production_countries": ["USA"],
                "directors": ["Christopher Nolan"],
                "release_year": 2010,
            },
            "USA, Dir: Christopher Nolan, 2010",
            id="all three elements in country, director, year order",
        ),
        pytest.param(
            {"production_countries": ["Japan"], "directors": ["Ryusuke Hamaguchi"]},
            "Japan, Dir: Ryusuke Hamaguchi",
            id="no year, no empty slot",
        ),
        pytest.param({"release_year": 2010}, "2010", id="the year alone"),
        pytest.param({"production_countries": ["South Korea"]}, "South Korea", id="country alone"),
        pytest.param({"directors": ["Bong Joon-ho"]}, "Dir: Bong Joon-ho", id="director alone"),
        pytest.param(
            {"arc_stage": "shooting"}, "Shooting", id="arc-stage label only when all are absent"
        ),
        pytest.param(
            {"directors": ["Ethan Coen", "Joel Coen"]},
            "Dir: Ethan Coen/Joel Coen",
            id="co-directors join with a slash",
        ),
        pytest.param(
            {"directors": [f"Director {i + 1}" for i in range(14)]},
            "Dir: Director 1/Director 2 +12",
            id="directors capped at two with the remainder",
        ),
        pytest.param(
            {"production_countries": NINE_COUNTRIES, "directors": ["Apichatpong Weerasethakul"]},
            "Canada/Colombia/France +6, Dir: Apichatpong Weerasethakul",
            id="countries capped at three",
        ),
    ],
)
def test_the_frontend_fixtures(kwargs, expected):
    assert _parenthetical(**kwargs) == expected


def test_an_unknown_arc_stage_reads_as_announced():
    assert _parenthetical(arc_stage="bogus") == "Announced"
