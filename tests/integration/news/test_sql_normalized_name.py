"""`sql_normalized_name` ≡ `normalize_name`, over one fixture list (D-1437.3).

Two normalizations of a person's name are two different answers to "is this card about
somebody I follow", and the disagreement is invisible: the card is written under the Python
spelling by the carding paths and read under the SQL spelling by the delivery half, so a
divergence costs a follower their beat and logs nothing. Hence a parity test rather than a unit
test of either side, and an integration one — the SQL spelling is Postgres's `normalize`,
`lower`, `regexp_replace` and `btrim`, and nothing outside the database implements them.
"""

import pytest
from sqlalchemy import literal, select

from upmovies.news.subject_key import normalize_name, sql_normalized_name

AGREED = [
    pytest.param("C. Nolan", id="plain"),
    pytest.param("Denis Villeneuve", id="two words"),
    pytest.param("DENIS VILLENEUVE", id="upper case"),
    pytest.param("Léa Seydoux", id="accent"),
    pytest.param("Léa Seydoux", id="decomposed accent (NFKC composes it)"),
    pytest.param("Zendaya Coleman", id="non-breaking space (NFKC makes it a space)"),
    pytest.param("Josh  Brolin", id="double space"),
    pytest.param("  Austin Butler  ", id="padded with spaces"),
    pytest.param("\tAustin\tButler\n", id="padded and separated by tabs"),
    pytest.param("Тимоти Шаламе", id="cyrillic"),
    pytest.param("ﬁnn Wolfhard", id="ligature (NFKC splits it)"),
    pytest.param("Ólafur Arnalds", id="leading accent"),
    pytest.param("Anya\u2028Taylor-Joy", id="line separator"),
    pytest.param("Anya\u000bTaylor-Joy", id="vertical tab"),
    pytest.param("Anya\u1680Taylor-Joy", id="ogham space mark"),
]


@pytest.mark.parametrize("name", AGREED)
async def test_the_two_spellings_agree(session, name):
    in_sql = await session.scalar(select(sql_normalized_name(literal(name))))
    assert in_sql == normalize_name(name)


async def test_the_full_fold_divergence_is_pinned_rather_than_fixed(session):
    """The one accepted miss (documented on `sql_normalized_name`): Python's `casefold()` full
    -folds where SQL's `lower()` does not, so a name carrying `ß` normalizes differently on the
    two sides (`ss` here, `ß` there) — as does one carrying a Greek *final* sigma, which
    `casefold` maps to a medial one and `lower` leaves alone. An upper-case Greek name is not
    such a case and is in the agreeing list above: Postgres's `lower` does not apply the
    final-sigma rule Python's own `str.lower` does, which lands it on `casefold`'s answer.

    The third case is a whitespace one rather than a case one: `str.split()` treats U+0085 NEL
    as whitespace and Postgres's ``\\s`` character class does not, so a name carrying one keeps it
    on this side and loses it on the other. Every other separator checked — tab, newline, NBSP,
    line separator, vertical tab, the Ogham space mark — agrees, and is in the list above.

    Pinned as a failing pair rather than worked around: widening the SQL fold would mean
    reimplementing `casefold` in Postgres, and the real fix is person-id tokens in
    `subject_key`, which M2 deliberately did not add. A person whose TMDB name carries such a
    character is missed by the name branch and still reached by the resolved-mention branch,
    which matches on `person_id`. If this test ever starts failing, the divergence has closed
    and the caveat on the two functions can go."""
    for name in ("Kaiserstraße Müller", "Οδυσσεύς", "Anya\u0085Taylor-Joy"):
        in_sql = await session.scalar(select(sql_normalized_name(literal(name))))
        assert in_sql != normalize_name(name)
