# NEU-1534 — Every mail carries a Reply-To that reaches a real inbox

**Target repo:** upcoming-movies-backend only. No migration, no frontend change. One Coolify
change after deploy (§5): set `MAIL_REPLY_TO`.

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1534 (no priority, no type)
**Project:** bl: Maintenance (no milestone, no project spec, no comments or relations)
**Related:** NEU-1338 (D-30, the mail gateway and `Envelope`), NEU-1460 (D-1460.1, `deliver`
as the render seam), NEU-1463 (DC-10, `Envelope.headers` and the Resend `headers` object),
NEU-1464 (admin preview / test-send), ADR-0007 (first-party adapter over the vendor SDK).
**Ground truth read (2026-10-05):** `mail/types.py` (`Envelope`, `Mailer`, `Transport`),
`mail/templates.py::render`, `mail/gateway.py` (`validate_mail_configuration`,
`MailGateway.send`/`deliver`), `mail/resend.py::_to_wire`, `mail/noop.py`,
`app/services/digest_sender.py::render_batch`, `routers/digest_admin.py`, `config.py`
(`MAIL_FROM`, `RESEND_API_KEY`), `.env.example`, `docker-compose.prod.yml`, `AGENTS.md`
(the digest-slot mail prerequisites), `tests/unit/mail/*`.

---

## 1. What is wrong, and what changes

Production sends through Resend from `MAIL_FROM`, which is a no-reply address: the domain is
verified for sending and nothing reads the mailbox. A reader who hits reply on a verification
mail or a digest gets a bounce, or silence. The Resend `POST /emails` body has a top-level
`reply_to` field for exactly this and the adapter never sets it.

After this ticket every mail the app sends — verify, reset, email change, the digest, and an
admin test-send — carries a Reply-To naming an address that does receive mail. The address is
configuration (`MAIL_REPLY_TO`), it rides on the `Envelope` like `sender` does, and the Resend
adapter puts it on the wire only when it is set. Nothing about a mail's bodies, subject or
headers changes.

## 2. Decisions

**D-1534.1 — The address is a setting, `MAIL_REPLY_TO`.** A sibling of `MAIL_FROM` in
`config.py`: `mail_reply_to: str = Field(default="", alias="MAIL_REPLY_TO")`, taking the same
two forms (`a@b.co` or `Name <a@b.co>`). Not derived from `MAIL_FROM`'s domain and not a
constant: the mailbox is a property of the deployment, and an invented address is one nobody
notices is wrong until a reply bounces. Empty by default for `MAIL_FROM`'s reason.

**D-1534.2 — Optional, validated when set.** `validate_mail_configuration` does **not** require
it for a transmitting provider. Empty means no `reply_to` on the wire, which is exactly today's
behaviour: a reply that bounces is the status quo, not a broken product, so it is not worth a
failed boot the way a missing key or an undeliverable `MAIL_FROM` is. A non-empty value without
an `@` is refused at boot with the same `"@" in value` check `MAIL_FROM` gets, for the same
reason (Resend rejects it at send time with a 422 nobody sees), reported alongside the other
faults in the one collected error. The check applies whenever the value is non-empty, for
transmitting providers only — `noop` transmits nothing and is never asked.

**D-1534.3 — It rides on the `Envelope`, set at render.** `Envelope` gains
`reply_to: str | None = None`, after `headers`. `templates.render` gains a keyword-only
`reply_to: str | None = None` beside `sender`, and both render points pass
`settings.mail_reply_to or None`: `MailGateway.send` for transactional mail and
`digest_sender.render_batch` for the digest. This honours the principle written into
`Envelope`: what the transport is handed is the whole description of what was sent, so
`NoopTransport.sent` and an admin preview's envelope carry it too. Rejected: stamping it in
`MailGateway` just before the transport (one call site, but an envelope a caller renders and
inspects would lack it), and smuggling `Reply-To` into `headers` (that map is the digest's
List-Unsubscribe pair, and Resend documents `reply_to` as its own field).

**D-1534.4 — Every mail.** Both render seams read the same setting, so the four transactional
templates, the digest, and `send_test_digest` all carry it. No per-template opt-out.

## 3. Changes

### `src/upmovies/config.py`
- Add `mail_reply_to` directly after `mail_from`, with a comment in the house style: why it
  exists (the sender cannot receive), why it is optional (§2 D-1534.2), and that it takes both
  address forms.

### `src/upmovies/mail/types.py`
- `Envelope.reply_to: str | None = None`, last field. Docstring paragraph beside `sender`'s:
  it rides on the envelope for the same reason, and `None` means the provider sets none.

### `src/upmovies/mail/templates.py`
- `render(name, context, *, sender, to, reply_to=None)` → `Envelope(..., reply_to=reply_to)`.

### `src/upmovies/mail/gateway.py`
- `MailGateway.send` passes `reply_to=self._settings.mail_reply_to or None` to `render`.
- `validate_mail_configuration`: inside the `TRANSMITTING_PROVIDERS` branch, after the
  `MAIL_FROM` check: `if settings.mail_reply_to and "@" not in settings.mail_reply_to:` append
  `f"MAIL_PROVIDER is {provider!r} but MAIL_REPLY_TO is {settings.mail_reply_to!r}, which is
  not an email address"`.
- `deliver` is untouched: the caller's envelope already carries the field.

### `src/upmovies/mail/resend.py`
- `_to_wire`: `if envelope.reply_to: body["reply_to"] = envelope.reply_to`. A string, not a
  list — Resend accepts either, and one address is what the setting holds. An envelope with
  `reply_to=None` produces a body byte-for-byte identical to today's (the same discipline as
  `headers`). Update the `_to_wire` docstring's "five fields" wording and the module docstring's
  "one POST with five fields" line.

### `src/upmovies/app/services/digest_sender.py`
- `render_batch` passes `reply_to=settings.mail_reply_to or None` to `render`.

### `src/upmovies/mail/noop.py`
- No code change. The log line stays recipient + subject; the recorded `Envelope` carries the
  field for tests.

### `src/upmovies/routers/digest_admin.py`
- No change: the preview returns a body only, and the test-send goes through `deliver` with
  an envelope `render_batch` already stamped.

## 4. Acceptance criteria and tests

All in `tests/unit/mail/` unless noted; the suite stays network-free (respx routes as in
`test_resend.py`).

1. **Wire body.** `test_resend.py`: an envelope with `reply_to="Tom <hello@backlotter.com>"`
   puts `"reply_to": "Tom <hello@backlotter.com>"` at the top level of the JSON body, not under
   `headers` and not as an HTTP request header. An envelope with `reply_to=None` sends no
   `reply_to` key (extend `test_an_envelope_without_headers_sends_no_headers_key` or add a
   sibling).
2. **Render seam.** `test_templates.py`: `render(..., reply_to=...)` lands on
   `Envelope.reply_to`; omitted gives `None`.
3. **Gateway.** `test_gateway.py`: a gateway built on settings with `mail_reply_to` set sends
   an envelope whose `reply_to` is that value (assert via `NoopTransport.sent` or the respx
   body); with `mail_reply_to=""` the envelope's `reply_to` is `None`, never `""`.
4. **Boot validation.** `test_gateway.py`: resend with `mail_reply_to=""` boots; resend with
   `mail_reply_to="Backlotter"` fails naming `MAIL_REPLY_TO`; both address forms are accepted;
   `noop` with `mail_reply_to="Backlotter"` boots; the collected-faults test gains the case so a
   bad `MAIL_REPLY_TO` is reported in the same error as a missing key.
5. **Digest.** Wherever `render_batch` is already tested against settings (e.g.
   `tests/unit/mail/test_digest_template.py` or the integration digest-sender tests), one
   assertion that the rendered envelope's `reply_to` equals `settings.mail_reply_to` when set.
6. **Unchanged surfaces.** Existing tests for headers, idempotency keys, retries, admin
   preview and test-send pass untouched.

Commit on `main` runs the full pre-commit suite (~2.5 min; foreground with a 600 s timeout).

## 5. Docs and deploy

- **`.env.example`**: add `# MAIL_REPLY_TO=Backlotter <hello@backlotter.com>` under the
  `MAIL_FROM` line with a one-sentence comment: optional, the address replies land in, because
  `MAIL_FROM` is a sending-only address; validated for an `@` when set.
- **`docker-compose.prod.yml`**: add `MAIL_REPLY_TO: "${MAIL_REPLY_TO:-}"` beside `MAIL_FROM`,
  for the Coolify reason the surrounding comment already states (a variable absent at first
  deploy is one somebody adds by hand).
- **`AGENTS.md`**: in the digest-slot mail prerequisites bullet, one clause: `MAIL_REPLY_TO`
  is optional but should be set in prod, since `MAIL_FROM` cannot receive.
- **`RELEASE_NOTES.md`**: one line under the unreleased section.
- **Coolify (Tom, after deploy):** set `MAIL_REPLY_TO` on the production API service to the
  receiving address, confirm with `printenv`, and send one admin test digest and reply to it.
  Until the variable is set, behaviour is unchanged.
- No `CONTEXT.md` or ADR change: a reply-to is configuration, not domain vocabulary, and no
  architectural stance moves.

## 6. Out of scope

- Reading or routing replies (no inbound mail, no support tooling).
- Per-template or per-cadence reply-to addresses.
- A display-name default or any parsing of the address beyond the `@` check.
- Making `MAIL_REPLY_TO` required at boot; revisit if a deploy ships without it and replies
  are being lost in practice.
