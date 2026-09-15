# NEU-1354 — Home route becomes the timeline when signed in; global feed at /feed; empty state

**Repo:** upcoming-movies-frontend · **Project:** bl: Consumer Pivot · **Milestone:** M3
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-12) · **Story:** NEU-1330
**Blocked by:** NEU-1351 (`GET /me/timeline`)

## Problem

D-12 says `/` is the signed-in user's timeline and the global feed for everyone else. The
frontend is server-rendered on a Cloudflare Worker and has a deliberate invariant
(`routes/public-layout.tsx`, `components/layout/HeaderAccount.tsx`): **the server render never
resolves auth.** The `["me"]` query only runs on the client, so the first paint is always the
logged-out default and there is no hydration mismatch; the account menu is a client island that
fills in after hydration. Deciding "timeline or feed" in the loader would require forwarding the
session cookie server-side and would turn `/` into a per-user, uncacheable render. Decided
(2026-09-15): keep the invariant and **swap client-side**.

## What to build

### 1. `/` — server-rendered feed, client-side timeline swap

- The `feed.tsx` loader is unchanged: it SSRs the global grouped feed (first `DAYS_PER_PAGE`
  days) for everyone.
- A new `TimelineOrFeed` island wraps the page body. It reads `useAuth()`:
  - `user == null` (anonymous, or `me` still loading) → render the SSR'd global feed exactly as
    today. No spinner while `me` resolves; the anonymous experience must not regress.
  - `user != null` → render `<TimelinePage />`, which fetches `GET /me/timeline` through a
    TanStack Query hook `useTimeline({ limit, offset })` (`api/me.ts`, `apiFetch`,
    `credentials: "include"`) and shows a compact skeleton in the content area until the first
    page arrives. The header, search bar and footer never move.
- `TimelinePage` reuses `FeedDayCard` and the existing `groupByDay` / "View more" pagination
  unchanged — the response shape is identical to `/feed/grouped` (NEU-1351 contract).
- Heading: "Your timeline" with a right-aligned link "All updates →" to `/feed`.
- **Empty state** (timeline `total === 0`): a short card — "Your timeline is empty. Follow
  people, studios and films to fill it." — with two buttons: "Get started" → `/welcome`
  (NEU-1358; until that route exists, link to `/me/follows`) and "Browse all updates" →
  `/feed`. Never an empty page.
- Anonymous `/` gains one line under the heading: "Sign in to see a timeline of the people you
  follow." linking to `/login?next=/`. Small, muted, no layout shift.
- `meta()` for `/` is unchanged (SEO copy describes the global feed; signed-in users are not
  indexed).

### 2. `/feed` — the global feed for everyone

- New route `routes/all-updates.tsx` under the public layout, registered in `routes.ts` as
  `route("feed", …)`. It reuses the same loader and page component as today's `/` (extract the
  current feed page into a shared `GlobalFeed` component + `loadGlobalFeed` loader helper so
  the two routes share one implementation). Heading "All updates". Its `meta()` gets a
  canonical URL of `/feed` and `description` copy identical to today's home.
- Header nav gains "All updates" → `/feed` (visible to everyone; `GlobalHeader.tsx`).
- `sitemap.xml` is produced by the backend and lists film pages; no change.

### 3. Query and cache behaviour

- `useTimeline` query key `["timeline", limit, offset]`; `staleTime` 60 s; `refetchOnMount:
  "always"` like `me`, so navigating back after a follow change refreshes.
- Following/unfollowing (NEU-1353/1355) invalidates `["timeline"]` in their mutation
  `onSuccess`; note this in `api/me.ts` so those tickets pick it up.
- Logging out (`AuthContext.logoutMut`) clears `["timeline"]` and the island falls back to the
  SSR'd feed data still held in `loaderData` — no refetch needed.

## Acceptance criteria

- Anonymous render of `/` is byte-for-byte the same server HTML as today plus the sign-in
  line; no hydration warnings in tests.
- With MSW returning a user from `/me` and a non-empty `/me/timeline`, `/` renders "Your
  timeline" with the timeline's day groups and the "All updates →" link; the global feed is not
  shown.
- With a user and `total: 0`, the empty-state card renders with both buttons.
- `/feed` renders the global feed for anonymous and signed-in users alike, with the "All
  updates" heading.
- "View more" pagination works on both pages (MSW offset test).
- Logout on `/` returns to the global feed without a network round-trip for the feed.
- `task test && task lint && task typecheck` green; prettier-clean.

## Out of scope

- Server-side per-user rendering of `/` (rejected, see Problem).
- The `/welcome` onboarding route itself (NEU-1358) and follow buttons (NEU-1353).
- Any change to the backend feed or timeline endpoints.
- Caching headers on `/` at the Cloudflare edge (unchanged; the page stays anonymous-safe).


## Amendment 2026-09-15 — access gate (D-41)

The signed-in branch of the client island splits again, on `AuthContext.user.entitled`
(NEU-1392). The follow graph is subscriber functionality and closed by default (D-37).

- **Entitled** — swap to `/me/timeline`, exactly as designed above.
- **Not entitled** — do *not* swap. Keep the global feed the server already rendered and mount a
  locked-timeline panel above it: what a timeline is, and that access is currently limited while
  the subscription tier is being built. Not "upgrade now" — nothing can be bought yet.

This is a third state, not the existing empty state. Keep them distinct: **"no follows yet"**
(entitled, link to `/welcome`) versus **"no access yet"** (unentitled, explain and stop).
Sending an unentitled user to `/welcome` only gets them refused there (NEU-1358).

The logged-out-first-paint invariant is untouched — `/` still SSRs the global feed in every
case, and entitlement, like auth, is only ever resolved on the client.
