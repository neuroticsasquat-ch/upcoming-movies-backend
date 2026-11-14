import type { CloudflareOptions } from "@sentry/cloudflare";
import { type BrowserOptions, replayIntegration } from "@sentry/react";
import {
  API_OVERLOADED_ERROR_NAME,
  API_UNAVAILABLE_ERROR_NAME,
  ApiOverloadedError,
  ApiUnavailableError,
} from "@/api/public";
import { SENTRY_TUNNEL_PATH } from "@/lib/sentry-tunnel";

// Set by vite.config.ts on a build that knows its commit (NEU-1594); undefined in dev, in
// tests, and on a build without one.
declare const __GIT_SHA__: string | undefined;

/** The Sentry/Bugsink release: the git SHA the source maps were uploaded under. Read by both
 *  the browser (`entry.client.tsx`) and the Worker (`workers/app.ts`) init, so the two report
 *  the same release. Undefined rather than a placeholder when the build had none, so events
 *  carry no release instead of a wrong one. */
export const sentryRelease: string | undefined =
  typeof __GIT_SHA__ === "string" ? __GIT_SHA__ : undefined;

/** Clients whose browser errors are artefacts of the client, not of our code. Lightpanda, a
 *  headless crawler, fails to parse `/me`'s valid JSON 401 body and reports the rejection as
 *  unhandled although `api/client.ts` catches it: ~100 false events a day, each with a replay
 *  (NEU-2270, Bugsink `UPCOMING-MOVIES-FRONTEND-3`). */
const SKIPPED_USER_AGENTS = /Lightpanda/i;

/** Whether `entry.client.tsx` should start the browser SDK for this user agent. Skipping the
 *  init, rather than filtering in `beforeSend`, also stops the client's replays. */
export function shouldInitBrowserSentry(userAgent: string): boolean {
  return !SKIPPED_USER_AGENTS.test(userAgent);
}

/** The part of a Sentry event the filters below read and write. Structural, so the one function
 *  serves both SDKs' event types. */
type ReportedEvent = {
  exception?: { values?: { type?: string; value?: string }[] };
  fingerprint?: string[];
};

type EventHint = { originalException?: unknown };

/**
 * Drop the API's overload answer (NEU-2273). A burst sheds requests by design, and one Bugsink
 * event per shed page load would bury the errors that matter. The SSR loaders already turn it
 * into the busy page before anything reports it (`lib/overload.ts`); this catches the paths
 * that don't, such as a browser fetch whose rejection goes unhandled.
 *
 * Matched on the instance where the SDK still has it, and on the exception type otherwise. The
 * type is the error's hand-set `name`, so it survives minification.
 */
export function dropOverloaded<E extends ReportedEvent>(event: E, hint: EventHint): E | null {
  if (hint.originalException instanceof ApiOverloadedError) return null;
  if (event.exception?.values?.some((value) => value.type === API_OVERLOADED_ERROR_NAME))
    return null;
  return event;
}

/**
 * Group every unreachable-API event into one Bugsink issue per status (NEU-2284). The message
 * names the path, so without a fingerprint a crawl during an outage files one issue per URL it
 * touched: 785 of them on 2026-11-13. Reported rather than dropped, because unlike an overload
 * this is an outage, and the Worker is where it shows first.
 *
 * The status comes from the instance where the SDK still has it, and otherwise from the
 * message's trailing `failed: <status>`.
 */
export function groupUnavailable<E extends ReportedEvent>(event: E, hint: EventHint): E {
  let status: number | undefined;
  if (hint.originalException instanceof ApiUnavailableError) {
    status = hint.originalException.status;
  } else {
    const value = event.exception?.values?.find(
      (candidate) => candidate.type === API_UNAVAILABLE_ERROR_NAME,
    );
    if (!value) return event;
    const match = /failed: (\d{3})$/.exec(value.value ?? "");
    status = match ? Number(match[1]) : undefined;
  }
  event.fingerprint = ["api-unavailable", String(status ?? "unknown")];
  return event;
}

/** Both SDKs' `beforeSend` (`entry.client.tsx`, `workers/app.ts`): drop the API's overload
 *  answer, and group an unreachable API's events by status. */
export function beforeSend<E extends ReportedEvent>(event: E, hint: EventHint): E | null {
  const kept = dropOverloaded(event, hint);
  return kept && groupUnavailable(kept, hint);
}

// Errors only, no tracing, in both SDKs: no `tracesSampleRate` is what keeps an SDK's tracing
// off (NEU-2292). Bugsink keeps errors and discards spans, so the old 0.1 sent envelopes nobody
// could read, and turning them away cost Bugsink and its Postgres ~1 core during a crawl
// (docs/measurements/2026-11-13-neu-2286-api-throughput.md §1). If a tracing backend is ever
// added, tracing comes back with it, pointed at it.

/** The browser SDK's options (`entry.client.tsx`). Envelopes go through our own origin so ad
 *  blockers (Brave Shields, uBlock, …) don't drop the request to the Bugsink host; the Worker
 *  proxies them on (`workers/app.ts`). Replays are recorded only around an error. */
export function browserSentryOptions(dsn: string): BrowserOptions {
  return {
    dsn,
    tunnel: SENTRY_TUNNEL_PATH,
    integrations: [replayIntegration({ maskAllText: true, blockAllMedia: true })],
    replaysSessionSampleRate: 0.0,
    replaysOnErrorSampleRate: 1.0,
    environment: import.meta.env.MODE,
    release: sentryRelease,
    beforeSend,
  };
}

/** The Worker SDK's options (`workers/app.ts`). `dsn` is the `SENTRY_DSN` secret; unset, the
 *  SDK stays off. With no `tracesSampleRate` here, `withSentry` falls back to a
 *  `SENTRY_TRACES_SAMPLE_RATE` Worker var, so setting one would turn tracing back on: don't. */
export function workerSentryOptions(dsn: string | undefined): CloudflareOptions {
  return {
    dsn,
    environment: import.meta.env.MODE,
    release: sentryRelease,
    beforeSend,
  };
}
