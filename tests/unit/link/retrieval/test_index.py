from uuid import UUID, uuid4

from upmovies.link.retrieval.index import IndexedFilm, build_index, indexed_film
from upmovies.link.retrieval.normalize import SQUASH_FOLD_MIN_CHARS, squash_fold


def _film(title: str, **kwargs) -> IndexedFilm:
    return indexed_film(film_id=uuid4(), title=title, **kwargs)


class TestSearchableTitles:
    def test_title_original_title_and_alternatives_are_all_searchable(self):
        film = _film(
            "Runner",
            original_title="Coureur",
            alternative_titles=("El Corredor",),
        )
        assert [t.text for t in film.titles] == ["Runner", "Coureur", "El Corredor"]

    def test_each_searchable_title_carries_its_tokens_and_fold(self):
        film = _film("Spider-Man: Brand New Day")
        (title,) = film.titles
        assert title.tokens == ("spider", "man", "brand", "new", "day")
        assert title.folded == "spidermanbrandnewday"

    def test_titles_identical_in_both_fold_and_tokens_are_deduplicated(self):
        # TMDB routinely repeats the title as an alternative title, and the roster's
        # own "original_title != title" guard exists for the same reason. Scoring the
        # same string three times would be wasted work, not a stronger signal.
        film = _film(
            "Avatar: Fire and Ash",
            original_title="Avatar: Fire and Ash",
            alternative_titles=("Avatar Fire and Ash", "Avatar: Fire and Ash"),
        )
        assert [t.text for t in film.titles] == ["Avatar: Fire and Ash"]

    def test_dedup_is_not_a_similarity_judgement(self):
        # "&" and "and" are different strings to the squash-fold, so this is a genuinely
        # distinct spelling and must survive.
        film = _film("Avatar: Fire and Ash", alternative_titles=("Avatar Fire & Ash",))
        assert [t.text for t in film.titles] == ["Avatar: Fire and Ash", "Avatar Fire & Ash"]

    def test_a_spacing_variant_survives_dedup_because_it_tokenizes_differently(self):
        # "Naga Bandham" folds onto "Nagabandham" but is the only form carrying the
        # tokens "naga" and "bandham". Deduplicating on the fold alone would drop it and
        # leave the token scorer unable to reach a headline that spells it with a space.
        film = _film("Nagabandham", alternative_titles=("Naga Bandham", "The Snake Bond"))
        assert [t.text for t in film.titles] == ["Nagabandham", "Naga Bandham", "The Snake Bond"]

    def test_a_short_spacing_variant_is_reachable_only_through_its_tokens(self):
        # The regression the fold-only dedup would have caused. "Sunup" folds to five
        # characters — below SQUASH_FOLD_MIN_CHARS — so the substring rescue declines it
        # and the tokens of "Sun Up" are the film's only route in.
        film = _film("Sunup", alternative_titles=("Sun Up",))
        assert len(squash_fold("Sunup")) < SQUASH_FOLD_MIN_CHARS
        index = build_index([film])
        assert index.rescue_folds == ()
        assert index.films_for_token("sun") == frozenset({film.film_id})
        assert index.films_for_token("up") == frozenset({film.film_id})
        # The one-word spelling reaches nothing a headline written "Sun Up" would offer.
        assert index.films_for_token("sunup") == frozenset({film.film_id})

    def test_untokenizable_title_is_kept_as_a_searchable_form(self):
        # It contributes nothing to the token index and cannot rescue, but dropping it
        # would be a silent decision the scorer should own.
        film = _film("The A of")
        (title,) = film.titles
        assert title.tokens == ()


class TestTokenIndex:
    def test_token_maps_to_every_film_carrying_it(self):
        a = _film("Avatar: Fire and Ash")
        b = _film("Avatar: The Last Airbender")
        index = build_index([a, b])
        assert index.films_for_token("avatar") == frozenset({a.film_id, b.film_id})
        assert index.films_for_token("ash") == frozenset({a.film_id})

    def test_stopwords_and_single_characters_never_enter_the_index(self):
        film = _film("How to Train Your Dragon 2")
        index = build_index([film])
        assert index.films_for_token("to") == frozenset()
        assert index.films_for_token("2") == frozenset()
        assert index.films_for_token("dragon") == frozenset({film.film_id})

    def test_alternative_title_tokens_are_indexed(self):
        film = _film("Runner", alternative_titles=("El Corredor",))
        index = build_index([film])
        assert index.films_for_token("corredor") == frozenset({film.film_id})

    def test_unknown_token_yields_an_empty_set(self):
        index = build_index([_film("Runner")])
        assert index.films_for_token("nagabandham") == frozenset()

    def test_films_for_tokens_unions_across_tokens(self):
        a = _film("Avatar: Fire and Ash")
        b = _film("Runner")
        index = build_index([a, b])
        assert index.films_for_tokens(["avatar", "runner"]) == {a.film_id, b.film_id}

    def test_films_for_tokens_of_nothing_is_empty(self):
        index = build_index([_film("Runner")])
        assert index.films_for_tokens([]) == set()


class TestRescueFolds:
    def test_every_long_enough_title_fold_is_offered_for_substring_rescue(self):
        film = _film("Nagabandham")
        index = build_index([film])
        assert [(f.folded, f.film_id) for f in index.rescue_folds] == [
            ("nagabandham", film.film_id)
        ]

    def test_short_folds_are_withheld(self):
        # "war" would match inside "warehouse"; normalize.squash_fold_matches declines it,
        # and the index declines to offer it in the first place.
        short = _film("War")
        assert len("war") < SQUASH_FOLD_MIN_CHARS
        index = build_index([short])
        assert index.rescue_folds == ()

    def test_alternative_titles_are_offered_too(self):
        film = _film("Nagabandham", alternative_titles=("Naga Bandham", "The Snake Bond"))
        index = build_index([film])
        assert {f.folded for f in index.rescue_folds} == {"nagabandham", "thesnakebond"}

    def test_a_fold_shared_by_two_of_a_films_titles_is_offered_once(self):
        # "Nagabandham" and "Naga Bandham" are both searchable titles — they tokenize
        # differently — but they fold to the same string, and one substring test settles
        # both. Offering it twice would only make the scorer do the work twice.
        film = _film("Nagabandham", alternative_titles=("Naga Bandham",))
        assert len(film.titles) == 2
        index = build_index([film])
        assert [f.folded for f in index.rescue_folds] == ["nagabandham"]


class TestDisplayFields:
    def test_display_fields_are_carried_so_rendering_needs_no_second_round_trip(self):
        film = _film(
            "Runner",
            original_title="Coureur",
            year=2027,
            overview="A courier runs.",
            genres=("Drama", "Thriller"),
            director="Ana Ruiz",
            cast=("Lee Park", "Mira Sen", "Tom Ade"),
            collection="The Runner Collection",
        )
        index = build_index([film])
        got = index.film(film.film_id)
        assert got is not None
        assert (got.title, got.original_title, got.year) == ("Runner", "Coureur", 2027)
        assert got.overview == "A courier runs."
        assert got.genres == ("Drama", "Thriller")
        assert got.director == "Ana Ruiz"
        assert got.cast == ("Lee Park", "Mira Sen", "Tom Ade")
        assert got.collection == "The Runner Collection"

    def test_film_lookup_of_an_unknown_id_is_none(self):
        index = build_index([_film("Runner")])
        assert index.film(UUID(int=0)) is None

    def test_films_are_exposed_in_build_order(self):
        a, b = _film("Aaa Film"), _film("Bbb Film")
        index = build_index([a, b])
        assert [f.film_id for f in index.films] == [a.film_id, b.film_id]

    def test_size_reports_the_indexed_film_count(self):
        index = build_index([_film("Aaa Film"), _film("Bbb Film")])
        assert index.size == 2


class TestEmptyIndex:
    def test_an_empty_catalog_builds_an_empty_index(self):
        index = build_index([])
        assert index.size == 0
        assert index.films == ()
        assert index.rescue_folds == ()
        assert index.films_for_token("avatar") == frozenset()
