"""Text normalization for candidate retrieval. Pure — no I/O.

Two normalizations feed the retrieval scorer:

* **Tokenization** (`significant_tokens`) — lowercase, Unicode-aware word split, dropping
  single characters and stopwords, after collapsing runs of initials (see below). This is
  the canonical implementation; `news/title_match` scores its per-film Google News filter
  with the same tokens.
* **Squash-fold** (`squash_fold`) — lowercase and strip *all* punctuation and whitespace,
  so ``"Naga Bandham"`` and ``"Nagabandham"`` both fold to ``nagabandham``. Applied as a
  substring test (`squash_fold_matches`), it converts the corpus's one genuine retrieval
  miss into a hit, taking titles-only recall from 89/93 to 90/93 (ADR-0008).

**Initialism collapse.** A run of initials is one word: ``F.A.S.T.`` names the same thing
``FAST`` does, and both sides of the index have to agree on that or they cannot join. The
tracked film *F.A.S.T.* was unreachable for exactly this reason — every letter of the title
is dropped by the ``len > 1`` rule, and the headline that named it spelled it the same
dotted way, so the story side emitted no ``fast`` either. Collapsing inside
`significant_tokens`, before the word split, is what makes titles and story text move
together by construction; a retrieval-local fork would drift from `news/title_match`.

Two guards keep it narrow. The run is **letters only** — admitting digits chews `7/4/2026`
and `9.4/10` into tokens, destroying the year and, measured, costing candidates. And the
collapsed form must reach `COLLAPSED_INITIALISM_MIN_CHARS`, because the collapse invents a
token the surface text never literally contains: at two characters ``U.S.`` alone puts 489
production headlines against every film with ``us`` in its title.

**Why not the fold route.** Lowering `SQUASH_FOLD_MIN_CHARS` for titles that tokenize to
nothing looks like the cheaper fix and is not: ``squash_fold("S/H/V")`` is ``shv``, which
occurs inside ``nashville``, and a fold rescue is full credit — 104 production stories
would rank that film first. The token index matches whole words, so ``fast`` can never
match inside ``breakfast``. That asymmetry is the whole reason the fix lives here (ADR-0008).

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

# Two or more <letter><separator> pairs, plus an optional bare trailing letter, where the
# separator is a period or a slash. The lookbehind refuses a run that starts immediately
# after an alphanumeric, so it cannot fire mid-word ("noS.O.S" keeps its "nos").
_INITIALISM_RE = re.compile(r"(?:(?<![^\W_])[^\W\d_][./]){2,}[^\W\d_]?")

# The collapse *invents* a token the surface text never literally contains, so it is held
# to a stricter bar than the >1-character rule ordinary tokens face. Below this the run is
# left alone and tokenizes exactly as it did before the collapse existed.
COLLAPSED_INITIALISM_MIN_CHARS = 3


def _lower_nfc(text: str) -> str:
    """Lowercase and compose to NFC — the common prelude to both normalizations."""
    return unicodedata.normalize("NFC", text.lower())


def _collapse_initialism(match: re.Match[str]) -> str:
    """Join a run of initials into one word, or leave it verbatim if it is too short."""
    run = match.group()
    collapsed = run.replace(".", "").replace("/", "")
    return collapsed if len(collapsed) >= COLLAPSED_INITIALISM_MIN_CHARS else run


def significant_tokens(text: str) -> list[str]:
    """Lowercased word tokens, dropping single characters and stopwords.

    Runs of initials are collapsed to one word first, so ``F.A.S.T.`` tokenizes as ``FAST``
    does — see the module docstring for the guards on that.

    Order is preserved and duplicates are kept, so callers can score against the token
    count of a title."""
    collapsed = _INITIALISM_RE.sub(_collapse_initialism, _lower_nfc(text))
    return [t for t in _WORD_RE.findall(collapsed) if len(t) > 1 and t not in _STOPWORDS]


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
