"""Text normalization for candidate retrieval. Pure — no I/O.

Two normalizations feed the retrieval scorer:

* **Tokenization** (`significant_tokens`) — lowercase, Unicode-aware word split, dropping
  single characters and stopwords. This is the canonical implementation; `news/title_match`
  scores its per-film Google News filter with the same tokens.
* **Squash-fold** (`squash_fold`) — lowercase and strip *all* punctuation and whitespace,
  so ``"Naga Bandham"`` and ``"Nagabandham"`` both fold to ``nagabandham``. Applied as a
  substring test (`squash_fold_matches`), it converts the corpus's one genuine retrieval
  miss into a hit, taking titles-only recall from 89/93 to 90/93 (ADR-0008).

**No ASCII folding.** Accents are preserved verbatim: ``unaccent``-equivalent folding is a
plausible addition for the multilingual catalog, but it is unmeasured, and ADR-0008's stance
is that unmeasured signals do not enter. Measure it against the labeled fixture first.

Both normalizations compose to NFC first, and that is load-bearing rather than tidiness: a
combining mark is not a word character, so decomposed input would otherwise lose its accents
to the squash-fold and split mid-word under tokenization (``amélie`` → ``ame``, ``lie``).
TMDB JSON and feedparser'd RSS make no normalization-form guarantee, so a title and a headline
that read identically can arrive in different forms — composing first is what makes them
comparable, and it is also what keeps the no-ASCII-folding stance above honest."""

import re
import unicodedata

# Common words that carry no disambiguating signal in a film title.
_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "part"}
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Everything that is not a word character, plus the underscore \w admits.
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)

# The squash-fold matches as a substring, so short titles would match inside unrelated
# words ("war" inside "warehouse"). Below this folded length the fold declines and the
# token scorer is left to judge the title on its own.
SQUASH_FOLD_MIN_CHARS = 6


def _lower_nfc(text: str) -> str:
    """Lowercase and compose to NFC — the common prelude to both normalizations."""
    return unicodedata.normalize("NFC", text.lower())


def significant_tokens(text: str) -> list[str]:
    """Lowercased word tokens, dropping single characters and stopwords.

    Order is preserved and duplicates are kept, so callers can score against the token
    count of a title."""
    return [t for t in _WORD_RE.findall(_lower_nfc(text)) if len(t) > 1 and t not in _STOPWORDS]


def squash_fold(text: str) -> str:
    """Lowercase `text` with all punctuation and whitespace removed.

    Letters (including non-ASCII) and digits survive; everything else goes, so spelling
    variants that differ only in spacing or punctuation collapse onto one another."""
    return _NON_WORD_RE.sub("", _lower_nfc(text))


def squash_fold_matches(title: str, text: str) -> bool:
    """True if `title`'s squash-fold appears inside `text`'s squash-fold.

    A substring test, not an equality test — the tracked title `nagabandham` must match
    inside `nagabandhammovietrailerlaunch`. Titles folding to fewer than
    `SQUASH_FOLD_MIN_CHARS` characters never match (see the constant)."""
    folded_title = squash_fold(title)
    if len(folded_title) < SQUASH_FOLD_MIN_CHARS:
        return False
    return folded_title in squash_fold(text)
