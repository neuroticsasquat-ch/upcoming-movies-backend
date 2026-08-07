import unicodedata

from upmovies.link.retrieval.normalize import (
    SQUASH_FOLD_MIN_CHARS,
    significant_tokens,
    squash_fold,
    squash_fold_matches,
)


class TestSignificantTokens:
    def test_lowercases_and_splits_on_punctuation(self):
        assert significant_tokens("Spider-Man: Brand New Day") == [
            "spider",
            "man",
            "brand",
            "new",
            "day",
        ]

    def test_drops_stopwords(self):
        assert significant_tokens("The Girl in the Clouds") == ["girl", "clouds"]

    def test_drops_single_characters(self):
        # "2" and "a" carry no disambiguating signal on their own.
        assert significant_tokens("How to Train Your Dragon 2") == [
            "how",
            "train",
            "your",
            "dragon",
        ]

    def test_is_unicode_aware(self):
        assert significant_tokens("Касса невест") == ["касса", "невест"]

    def test_decomposed_input_tokenizes_the_same_as_composed(self):
        # A combining mark is not a word character, so without the NFC pass decomposed
        # input splits mid-word: "amélie" -> ["ame", "lie"].
        title = "Amélie: Le Retour"
        assert significant_tokens(unicodedata.normalize("NFD", title)) == significant_tokens(
            unicodedata.normalize("NFC", title)
        )
        assert significant_tokens(unicodedata.normalize("NFD", title)) == [
            "amélie",
            "le",
            "retour",
        ]

    def test_all_stopword_title_yields_nothing(self):
        assert significant_tokens("The A of") == []

    def test_empty_text_yields_nothing(self):
        assert significant_tokens("") == []


class TestSquashFold:
    def test_strips_whitespace_and_lowercases(self):
        # The regression case: the tracked title and the headline's spelling differ
        # only by a space.
        assert squash_fold("Naga Bandham") == "nagabandham"
        assert squash_fold("Nagabandham") == "nagabandham"

    def test_strips_all_punctuation(self):
        assert squash_fold("Spider-Man: Brand New Day") == "spidermanbrandnewday"
        assert squash_fold("'Love and War'") == "loveandwar"

    def test_strips_underscores_and_interior_whitespace_runs(self):
        assert squash_fold("Mission_Impossible\t--\nThe  Final Reckoning") == (
            "missionimpossiblethefinalreckoning"
        )

    def test_keeps_digits(self):
        assert squash_fold("How to Train Your Dragon 2") == "howtotrainyourdragon2"

    def test_preserves_non_ascii_letters(self):
        # No ASCII folding — accents are preserved verbatim (see the module docstring).
        assert squash_fold("Amélie: Le Retour") == "amélieleretour"
        assert squash_fold("Касса невест") == "кассаневест"

    def test_decomposed_input_folds_the_same_as_composed(self):
        # Without the NFC pass, [\W_] strips the combining mark and silently performs the
        # ASCII folding ADR-0008 says must be measured before it enters.
        title = "Amélie: Le Retour"
        assert squash_fold(unicodedata.normalize("NFD", title)) == "amélieleretour"
        assert squash_fold(unicodedata.normalize("NFD", title)) == squash_fold(
            unicodedata.normalize("NFC", title)
        )

    def test_punctuation_only_text_folds_to_empty(self):
        assert squash_fold("--- !!! ---") == ""
        assert squash_fold("") == ""


class TestSquashFoldMatches:
    def test_naga_bandham_regression_pair(self):
        # The one genuine retrieval miss in the labeled corpus: tracked "Nagabandham"
        # against a headline spelling it "Naga Bandham". Token overlap scores it zero;
        # the squash-fold rescues it. Both headlines are quoted verbatim from
        # tests/fixtures/link/validation_set.json.
        assert (
            squash_fold_matches("Nagabandham", "Naga Bandham Movie Trailer Launch - Telugu Times")
            is True
        )
        # ...and symmetrically, a tracked "Naga Bandham" against the run-together spelling.
        assert (
            squash_fold_matches(
                "Naga Bandham",
                "This Week's OTT & Movie Releases in India (July 2-3, 2026): "
                "Alpha, Nagabandham, Super Subbu & More",
            )
            is True
        )

    def test_matches_as_substring_not_equality(self):
        assert (
            squash_fold_matches("Avatar: Fire and Ash", "'Avatar Fire and Ash' gets a new trailer")
            is True
        )

    def test_unrelated_headline_does_not_match(self):
        assert squash_fold_matches("Nagabandham", "Stock futures rise on Wall Street") is False

    def test_short_title_is_guarded_against_matching_inside_a_word(self):
        # Folded "war" is 3 chars: without the guard it matches inside "warehouse".
        assert squash_fold_matches("War", "Warehouse fire downtown") is False
        # Even an honest mention is declined — the token scorer, not the fold, owns
        # short titles.
        assert squash_fold_matches("War", "This means war for the studio") is False

    def test_minimum_length_boundary(self):
        assert len(squash_fold("Alien")) == SQUASH_FOLD_MIN_CHARS - 1
        assert squash_fold_matches("Alien", "Aliens vs the world") is False

        assert len(squash_fold("Aliens")) == SQUASH_FOLD_MIN_CHARS
        assert squash_fold_matches("Aliens", "Aliens vs the world") is True

    def test_empty_and_punctuation_only_titles_never_match(self):
        assert squash_fold_matches("", "Naga Bandham Movie Trailer Launch") is False
        assert squash_fold_matches("---", "Naga Bandham Movie Trailer Launch") is False

    def test_empty_text_never_matches(self):
        assert squash_fold_matches("Nagabandham", "") is False
