"""Template rendering — the three strictnesses in `mail/templates.py`, each asserted by the
bad mail it is there to prevent."""

import pytest

from upmovies.mail import (
    MailError,
    TemplateRenderError,
    UnknownTemplateError,
    available_templates,
    render,
)
from upmovies.mail.templates import validate_templates

VERIFY_CONTEXT: dict[str, object] = {
    "display_name": "Ada",
    "product_name": "Backlotter",
    "verify_url": "https://app.example.com/verify?token=abc123",
    "expires_in_hours": 24,
}


def _verify(**overrides: object):
    return render(
        "verify",
        {**VERIFY_CONTEXT, **overrides},
        sender="Backlotter <no-reply@example.com>",
        to="ada@example.com",
    )


def test_the_verify_template_renders_both_parts_and_the_envelope_fields():
    envelope = _verify()

    assert envelope.sender == "Backlotter <no-reply@example.com>"
    assert envelope.to == "ada@example.com"
    assert envelope.subject == "Confirm your email address"
    assert "https://app.example.com/verify?token=abc123" in envelope.text
    assert "https://app.example.com/verify?token=abc123" in envelope.html
    assert "Ada" in envelope.text


def test_the_text_part_carries_the_link_as_text_not_only_as_a_button():
    """The plain-text part has to stand alone (`types.Envelope`), and a bare `<a>` in the HTML
    is not a link a text client can follow."""
    envelope = _verify()

    assert "<a " not in envelope.text
    assert envelope.text.count("https://app.example.com/verify?token=abc123") >= 1


def test_the_ship_has_exactly_the_templates_it_claims():
    assert available_templates() == ("verify",)
    assert validate_templates() == []


# --- the three strictnesses ----------------------------------------------------


def test_a_missing_context_key_raises_rather_than_rendering_a_blank_link():
    """`StrictUndefined`. The whole point of this mail is the link; a context that spells the
    key differently must not produce a mail with nothing in it."""
    context = {k: v for k, v in VERIFY_CONTEXT.items() if k != "verify_url"}
    with pytest.raises(TemplateRenderError, match="verify_url"):
        render("verify", context, sender="a@b.c", to="ada@example.com")


def test_a_render_failure_is_catchable_as_a_mail_error():
    """`MailError` is what this package documents a caller as catching, so the most likely
    template fault must not escape it as a raw `jinja2.UndefinedError`."""
    from jinja2 import UndefinedError

    context = {k: v for k, v in VERIFY_CONTEXT.items() if k != "verify_url"}
    with pytest.raises(MailError) as exc:
        render("verify", context, sender="a@b.c", to="ada@example.com")

    assert isinstance(exc.value.__cause__, UndefinedError)


def test_display_name_is_genuinely_optional_despite_strict_undefined():
    """`{% if display_name %}` reads as optional and is not: `StrictUndefined.__bool__`
    raises. A user with no display name is an ordinary case, not a broken send."""
    context = {k: v for k, v in VERIFY_CONTEXT.items() if k != "display_name"}
    envelope = render("verify", context, sender="a@b.c", to="ada@example.com")

    assert envelope.text.startswith("Hi,")
    assert "https://app.example.com/verify?token=abc123" in envelope.text


def test_an_empty_display_name_greets_without_a_dangling_space():
    envelope = _verify(display_name="")
    assert envelope.text.startswith("Hi,")


def test_the_html_part_escapes_context_and_the_text_part_does_not():
    """Autoescape by extension. Escaping the text part would show the reader `&amp;`; not
    escaping the HTML part would let a display name close a tag."""
    envelope = _verify(display_name="Ada & <b>Lovelace</b>")

    assert "Ada &amp; &lt;b&gt;Lovelace&lt;/b&gt;" in envelope.html
    assert "Ada & <b>Lovelace</b>" in envelope.text


def test_a_multiline_subject_is_collapsed_to_one_header_line():
    """A subject is an SMTP header, and a context value that reaches one is how an injected
    newline would ride in."""
    envelope = _verify()
    assert "\n" not in envelope.subject
    assert "  " not in envelope.subject


def test_an_unknown_template_names_what_is_available():
    with pytest.raises(UnknownTemplateError, match="verify"):
        render("nope", {}, sender="a@b.c", to="ada@example.com")


def test_a_template_that_renders_an_empty_text_body_is_an_error(tmp_path, monkeypatch):
    """Jinja succeeding is not the same as the message being sendable."""
    from upmovies.mail import templates as templates_module

    empty = tmp_path / "hollow"
    empty.mkdir()
    (empty / "subject.txt").write_text("Subject")
    (empty / "body.txt").write_text("{{ nothing_much }}")
    (empty / "body.html").write_text("<p>something</p>")
    monkeypatch.setattr(templates_module._ENV.loader, "searchpath", [str(tmp_path)])
    monkeypatch.setattr(templates_module._ENV, "cache", {})

    with pytest.raises(MailError, match="empty plain-text body"):
        templates_module.render("hollow", {"nothing_much": ""}, sender="a@b.c", to="d@e.f")


def test_validate_templates_reports_an_incomplete_template(tmp_path, monkeypatch):
    """The boot check's real job: the templates are data files nothing in the import graph
    references, so a build that drops them starts perfectly and fails at the first send."""
    from upmovies.mail import templates as templates_module

    half = tmp_path / "halfdone"
    half.mkdir()
    (half / "subject.txt").write_text("Subject")
    monkeypatch.setattr(templates_module, "_TEMPLATE_ROOT", tmp_path)

    problems = templates_module.validate_templates()

    assert len(problems) == 1
    assert "halfdone" in problems[0]
    assert "body.txt" in problems[0] and "body.html" in problems[0]


def test_validate_templates_reports_a_missing_template_directory(tmp_path, monkeypatch):
    from upmovies.mail import templates as templates_module

    monkeypatch.setattr(templates_module, "_TEMPLATE_ROOT", tmp_path / "gone")

    assert "does not exist" in templates_module.validate_templates()[0]
