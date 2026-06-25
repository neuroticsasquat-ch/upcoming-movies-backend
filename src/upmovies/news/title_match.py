"""Cheap, high-recall title-token match for filtering per-film Google News results
before they reach the link stage. Pure — no I/O.

A per-film Google search (`<title> when:Nd`, deliberately unquoted) returns a large,
roughly title-independent junk floor. This drops headlines that clearly aren't about the
film while keeping anything with enough title-token overlap; the LLM linker remains the
precision authority on whatever survives."""

import re

# Common words that carry no disambiguating signal in a film title.
_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "part"}
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _significant_tokens(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def title_matches(film_title: str, headline: str, *, min_ratio: float) -> bool:
    """True if the headline should be kept for this film's per-film search.

    High-recall: a title with no significant tokens (all-stopword / single-char / numeric
    / non-Latin that tokenizes to nothing) is always kept — we don't filter what we can't
    assess. Otherwise keep when the fraction of significant title tokens present as whole
    words in the headline is >= min_ratio (single-token titles thus require that token)."""
    title_tokens = _significant_tokens(film_title)
    if not title_tokens:
        return True
    headline_tokens = set(_significant_tokens(headline))
    hits = sum(1 for t in title_tokens if t in headline_tokens)
    return hits / len(title_tokens) >= min_ratio
