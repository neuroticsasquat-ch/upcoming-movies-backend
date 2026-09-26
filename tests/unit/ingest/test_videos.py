"""The video poll's pure pieces: flattening a `/videos` payload, the trailer cut, and the
detail line the phase reports itself on (D-35)."""

from datetime import UTC, datetime

from tests.fixtures.tmdb import make_video, make_videos
from upmovies.ingest.tmdb.schemas import TMDBVideos
from upmovies.ingest.videos import (
    Video,
    VideosResult,
    is_trailer,
    videos_detail,
    videos_from_payload,
)


def _payload(*videos) -> TMDBVideos:
    return TMDBVideos.model_validate(make_videos(1, list(videos)))


def test_every_video_is_kept_whatever_its_type_or_site():
    """The ledger is the dedup rule, so it has to remember the videos that do not card too —
    a teaser an editor relabels `Trailer` next month is not a new video."""
    videos = videos_from_payload(
        _payload(
            make_video("aaa"),
            make_video("bbb", type="Teaser"),
            make_video("ccc", site="Vimeo", type="Clip"),
        )
    )

    assert [v.key for v in videos] == ["aaa", "bbb", "ccc"]


def test_the_same_key_listed_twice_is_one_video():
    """TMDB does carry one YouTube key under two of its own video ids. The ledger is keyed on
    (film, site, key), so two rows with one key would raise and cost the film its poll."""
    videos = videos_from_payload(
        _payload(make_video("aaa"), make_video("aaa", id="tmdb-duplicate", name="Trailer 1"))
    )

    assert [(v.key, v.name) for v in videos] == [("aaa", "Official Trailer")]


def test_a_film_with_nothing_to_watch_flattens_to_no_videos():
    """The ordinary answer for an unannounced title, and a 200 rather than a 404."""
    assert videos_from_payload(_payload()) == []


def test_published_at_is_parsed_off_the_wire():
    (video,) = videos_from_payload(_payload(make_video("aaa")))

    assert video.published_at == datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def test_a_video_tmdb_holds_no_publication_time_for_parses():
    """TMDB omits it on older rows, and sends `""` rather than null when it does."""
    (video,) = videos_from_payload(_payload(make_video("aaa", published_at="")))

    assert video.published_at is None


def _video(**overrides) -> Video:
    fields = {
        "site": "youtube",
        "key": "aaa",
        "type": "Trailer",
        "name": "Official Trailer",
        "published_at": None,
    }
    fields.update(overrides)
    return Video(**fields)


def test_a_youtube_trailer_is_the_beat():
    assert is_trailer(_video()) is True


def test_type_is_matched_case_insensitively():
    """Free text on TMDB's side, and a card missed over letter case is silent: the poll
    records the video, so it is never re-examined and the beat is lost for good."""
    assert is_trailer(_video(type="trailer")) is True


def test_a_teaser_is_not_a_trailer():
    """A different promise to a reader. A film that teases and then trailers would otherwise
    card twice for what the product calls one moment."""
    assert is_trailer(_video(type="Teaser")) is False


def test_the_stored_site_is_case_folded_to_match_the_ledgers_key():
    """`uq_film_video` is keyed on the stored `site`, so the folding has to happen where the
    payload is flattened — otherwise the dedup key here and the constraint's key are two
    different strings, and a casing change records, and cards, a video already known."""
    videos = videos_from_payload(_payload(make_video("aaa", site="YouTube")))

    assert [v.site for v in videos] == ["youtube"]


def test_a_casing_variant_inside_one_payload_is_one_video():
    videos = videos_from_payload(
        _payload(make_video("aaa", site="YouTube"), make_video("aaa", site="youtube"))
    )

    assert len(videos) == 1


def test_a_trailer_somewhere_other_than_youtube_does_not_card():
    """The card carries one key that the film page embeds as a YouTube player; a Vimeo key in
    that field renders an empty box."""
    assert is_trailer(_video(site="Vimeo")) is False


def test_detail_reports_baselined_beside_recorded():
    line = videos_detail(
        VideosResult(selected=10, polled=9, videos=31, recorded=4, baselined=2, cards=1, missing=1)
    )

    assert line == (
        "videos: 9/10 polled, 31 videos, 4 recorded, 2 baselined, 1 carded, 1 missing, 0 failed"
    )


def test_a_cold_catalogue_reads_as_baselined_rather_than_as_lost_cards():
    """`recorded` climbing with `carded` flat is the baseline rule working (ADR-0014), and
    without `baselined` on the line there is no way to read that from the outside."""
    line = videos_detail(VideosResult(selected=3, polled=3, videos=9, recorded=9, baselined=3))

    assert "9 recorded, 3 baselined, 0 carded" in line


def test_an_abort_is_named_on_the_line():
    line = videos_detail(
        VideosResult(
            selected=5,
            polled=1,
            failures=4,
            aborted=True,
            abort_error="aborted after 4 consecutive failures",
        )
    )

    assert line.endswith("; videos aborted: aborted after 4 consecutive failures")
