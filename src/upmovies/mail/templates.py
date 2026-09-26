"""Rendering a named template into an `Envelope`, and the boot check that the templates are
there to render.

**One template is a directory**, not a file: `templates/<name>/` holding `subject.txt`,
`body.txt` and `body.html`. The alternative — `verify.subject.txt` beside `verify.txt` beside
`verify.html` — puts three files of one message in a directory with three files of every other
message, and the copy for a single mail stops being reviewable as a unit. It also makes
"is this template complete?" a glob rather than a directory listing, which is what
`validate_templates` below needs it to be.

Three deliberate strictnesses, each of which turns a silent bad mail into a loud failure:

* **`StrictUndefined`.** A missing context key raises instead of rendering empty. The whole
  point of the `verify` mail is a link; a template referencing `{{ verify_url }}` against a
  context that spells it `url` must not send a mail with a blank link in it.
* **Autoescape on `.html` only.** The HTML part escapes, the text part and the subject do not —
  escaping a plain-text body would render `&amp;` to the reader. Selected by extension rather
  than by a flag per call, so a new template cannot opt itself out by accident.
* **An empty text body is an error.** `text` is the part that must stand alone (see
  `types.Envelope`), so a template that renders it away has failed even though Jinja
  succeeded."""

import pathlib
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, TemplateNotFound
from jinja2 import select_autoescape as _select_autoescape

from upmovies.mail.types import (
    Envelope,
    MailError,
    TemplateRenderError,
    UnknownTemplateError,
)

_TEMPLATE_ROOT = pathlib.Path(__file__).parent / "templates"

_SUBJECT = "subject.txt"
_TEXT = "body.txt"
_HTML = "body.html"
_REQUIRED_PARTS: tuple[str, ...] = (_SUBJECT, _TEXT, _HTML)

_WHITESPACE = re.compile(r"\s+")

_ENV = Environment(
    loader=FileSystemLoader(_TEMPLATE_ROOT),
    # Escaping is decided by the part's extension, so `body.html` escapes and `body.txt` and
    # `subject.txt` do not. `default_for_string=False` matters because it is what keeps a
    # future `Template.from_string` helper honest rather than silently unescaped-by-default.
    autoescape=_select_autoescape(enabled_extensions=("html",), default_for_string=False),
    undefined=StrictUndefined,
    # Both on so a template can use block tags for layout without every `{% if %}` leaving a
    # blank line in the plain-text part, where the reader can see it.
    trim_blocks=True,
    lstrip_blocks=True,
)


def available_templates() -> tuple[str, ...]:
    """Every template name under `mail/templates/`, sorted.

    A directory is a template by virtue of being a directory; whether it is a *complete* one
    is `validate_templates`' question, so that a half-finished template is reported as
    incomplete at boot rather than being invisible here and `UnknownTemplateError` later."""
    if not _TEMPLATE_ROOT.is_dir():
        return ()
    return tuple(sorted(p.name for p in _TEMPLATE_ROOT.iterdir() if p.is_dir()))


def validate_templates() -> list[str]:
    """Every reason the template tree could not serve a send, as human-readable lines.

    Returns problems rather than raising so `gateway.validate_mail_configuration` can report
    them together with the provider and credential faults — a deploy with two things wrong
    should learn about both from one failed boot.

    Worth checking at boot at all because the failure it catches is a *packaging* one. The
    templates are data files inside a Python package; nothing in the import graph references
    them, so a build that shipped the `.py` files and not the `.txt` ones imports perfectly
    and fails at the first send instead."""
    if not _TEMPLATE_ROOT.is_dir():
        return [f"the mail template directory {_TEMPLATE_ROOT} does not exist"]
    names = available_templates()
    if not names:
        return [f"the mail template directory {_TEMPLATE_ROOT} contains no templates"]
    problems: list[str] = []
    for name in names:
        missing = [part for part in _REQUIRED_PARTS if not (_TEMPLATE_ROOT / name / part).is_file()]
        if missing:
            problems.append(f"mail template {name!r} is missing {', '.join(missing)}")
    return problems


def _render_part(name: str, part: str, context: dict[str, object]) -> str:
    try:
        template = _ENV.get_template(f"{name}/{part}")
    except TemplateNotFound as exc:
        raise UnknownTemplateError(
            f"no mail template {name!r} (missing {name}/{part}); "
            f"available: {', '.join(available_templates()) or '<none>'}"
        ) from exc
    try:
        return template.render(context)
    except TemplateError as exc:
        # `UndefinedError` is the one that actually happens, but every Jinja render failure is
        # the same thing to a caller — this template cannot be turned into a message — and
        # `TemplateError` is their common base.
        raise TemplateRenderError(
            f"mail template {name}/{part} could not be rendered: {exc}"
        ) from exc


def render(name: str, context: dict[str, object], *, sender: str, to: str) -> Envelope:
    """Render template `name` against `context` into a ready-to-send `Envelope`.

    Renders into the transport's own value type rather than into a separate "rendered
    template" record, so there is exactly one shape describing a message in this package. The
    second record would differ from `Envelope` only by the two fields the caller already
    holds, and keeping the two in step would be nobody's job.

    The subject is collapsed to a single line of single-spaced text. Partly cosmetic — a
    template author should be free to wrap a long subject across source lines — and partly
    not: a subject is an SMTP header, and context values that reach one (a film title, a
    display name) are the kind of thing an injected newline rides in on."""
    subject = _WHITESPACE.sub(" ", _render_part(name, _SUBJECT, context)).strip()
    text = _render_part(name, _TEXT, context).strip()
    html = _render_part(name, _HTML, context).strip()
    if not subject:
        raise MailError(f"mail template {name!r} rendered an empty subject")
    if not text:
        raise MailError(
            f"mail template {name!r} rendered an empty plain-text body; the text part is the "
            f"one that has to stand on its own, so an HTML-only send is not a valid message"
        )
    return Envelope(sender=sender, to=to, subject=subject, text=text, html=html)
