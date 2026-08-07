"""Cheap, high-recall title-token match for filtering per-film Google News results
before they reach the link stage. Pure — no I/O.

A per-film Google search (`<title> when:Nd`, deliberately unquoted) returns a large,
roughly title-independent junk floor. This drops headlines that clearly aren't about the
film while keeping anything with enough title-token overlap; the LLM linker remains the
precision authority on whatever survives.

Tokenization is shared with candidate retrieval rather than duplicated here, so a change to
`link/retrieval/normalize.significant_tokens` moves this filter's behaviour too — check
`scripts/measure_title_filter.py` before retuning it."""

from upmovies.link.retrieval.normalize import significant_tokens


def title_matches(film_title: str, headline: str, *, min_ratio: float) -> bool:
    """True if the headline should be kept for this film's per-film search.

    High-recall: a title with no significant tokens (all-stopword / single-char / numeric
    / non-Latin that tokenizes to nothing) is always kept — we don't filter what we can't
    assess. Otherwise keep when the fraction of significant title tokens present as whole
    words in the headline is >= min_ratio (single-token titles thus require that token)."""
    title_tokens = significant_tokens(film_title)
    if not title_tokens:
        return True
    headline_tokens = set(significant_tokens(headline))
    hits = sum(1 for t in title_tokens if t in headline_tokens)
    return hits / len(title_tokens) >= min_ratio
