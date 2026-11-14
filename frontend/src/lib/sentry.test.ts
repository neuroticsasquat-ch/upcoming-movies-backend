import { afterEach, describe, expect, it, vi } from "vitest";

// `__GIT_SHA__` is compiled in by vite.config.ts on a build that knows its commit; tests have no
// define, so a global stands in for it and each case re-imports the module to re-read it.
describe("sentryRelease", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("is the build's git SHA when one was compiled in", async () => {
    vi.stubGlobal("__GIT_SHA__", "abc123def");
    const { sentryRelease } = await import("./sentry");
    expect(sentryRelease).toBe("abc123def");
  });

  it("is undefined, not a placeholder, when the build had no SHA", async () => {
    const { sentryRelease } = await import("./sentry");
    expect(sentryRelease).toBeUndefined();
  });
});

describe("shouldInitBrowserSentry", () => {
  const CHROME =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/130.0.0.0 Safari/537.36";

  it("skips the Lightpanda headless crawler, whatever the case", async () => {
    const { shouldInitBrowserSentry } = await import("./sentry");
    expect(shouldInitBrowserSentry("Lightpanda/1.0")).toBe(false);
    expect(shouldInitBrowserSentry("lightpanda/2.3")).toBe(false);
  });

  it("initialises for a real browser", async () => {
    const { shouldInitBrowserSentry } = await import("./sentry");
    expect(shouldInitBrowserSentry(CHROME)).toBe(true);
  });

  it("initialises when the user agent is empty", async () => {
    const { shouldInitBrowserSentry } = await import("./sentry");
    expect(shouldInitBrowserSentry("")).toBe(true);
  });
});

describe("dropOverloaded", () => {
  const event = (type: string) => ({ exception: { values: [{ type }] } });

  it("drops the API's overload answer by instance", async () => {
    const { dropOverloaded } = await import("./sentry");
    const { ApiOverloadedError } = await import("@/api/public");
    const error = new ApiOverloadedError("/people/x", 5);
    expect(dropOverloaded(event("Error"), { originalException: error })).toBeNull();
  });

  it("drops it by exception type when the instance is gone", async () => {
    const { dropOverloaded } = await import("./sentry");
    expect(dropOverloaded(event("ApiOverloadedError"), {})).toBeNull();
  });

  it("keeps every other event", async () => {
    const { dropOverloaded } = await import("./sentry");
    const kept = event("Error");
    expect(dropOverloaded(kept, { originalException: new Error("boom") })).toBe(kept);
  });
});

describe("groupUnavailable", () => {
  type TestEvent = {
    exception: { values: { type: string; value: string }[] };
    fingerprint?: string[];
  };
  const event = (type: string, value: string): TestEvent => ({
    exception: { values: [{ type, value }] },
  });

  it("gives two paths with the same status the same fingerprint", async () => {
    const { groupUnavailable } = await import("./sentry");
    const { ApiUnavailableError } = await import("@/api/public");
    const people = new ApiUnavailableError("/people/525-christopher-nolan", 503);
    const studio = new ApiUnavailableError("/companies/1-lucasfilm/events", 503);
    const a = groupUnavailable(event("Error", people.message), { originalException: people });
    const b = groupUnavailable(event("Error", studio.message), { originalException: studio });
    expect(a.fingerprint).toEqual(["api-unavailable", "503"]);
    expect(b.fingerprint).toEqual(a.fingerprint);
  });

  it("gives different statuses different fingerprints", async () => {
    const { groupUnavailable } = await import("./sentry");
    const { ApiUnavailableError } = await import("@/api/public");
    const fingerprints = [502, 503, 504].map((status) => {
      const error = new ApiUnavailableError("/films/x", status);
      return groupUnavailable(event("Error", error.message), { originalException: error })
        .fingerprint;
    });
    expect(new Set(fingerprints.map(String)).size).toBe(3);
  });

  it("reads the status from the message when the instance is gone", async () => {
    const { groupUnavailable } = await import("./sentry");
    const grouped = groupUnavailable(event("ApiUnavailableError", "GET /people/x failed: 502"), {});
    expect(grouped.fingerprint).toEqual(["api-unavailable", "502"]);
  });

  it("leaves every other event's grouping alone", async () => {
    const { groupUnavailable } = await import("./sentry");
    const other = groupUnavailable(event("Error", "GET /people/x failed: 500"), {
      originalException: new Error("GET /people/x failed: 500"),
    });
    expect(other.fingerprint).toBeUndefined();
  });
});

describe("beforeSend", () => {
  it("still drops the API's overload answer", async () => {
    const { beforeSend } = await import("./sentry");
    const { ApiOverloadedError } = await import("@/api/public");
    const error = new ApiOverloadedError("/people/x", 5);
    const dropped = { exception: { values: [{ type: "ApiOverloadedError" }] } };
    expect(beforeSend(dropped, { originalException: error })).toBeNull();
    expect(beforeSend(dropped, {})).toBeNull();
  });

  it("groups an unreachable API's event", async () => {
    const { beforeSend } = await import("./sentry");
    const { ApiUnavailableError } = await import("@/api/public");
    const error = new ApiUnavailableError("/people/x", 504);
    const event: {
      exception: { values: { type: string; value: string }[] };
      fingerprint?: string[];
    } = { exception: { values: [{ type: "ApiUnavailableError", value: error.message }] } };
    const sent = beforeSend(event, { originalException: error });
    expect(sent?.fingerprint).toEqual(["api-unavailable", "504"]);
  });
});

// NEU-2292: Bugsink keeps errors and discards spans, so any traces sample rate sent envelopes
// nobody could read, and Bugsink and its Postgres burned ~1 core turning them away during a
// crawl. An unset rate is what keeps an SDK from tracing; the rest is the error contract.
describe("browserSentryOptions", () => {
  it("reports errors with replays on error, and does not trace", async () => {
    const { beforeSend, browserSentryOptions, sentryRelease } = await import("./sentry");
    const { SENTRY_TUNNEL_PATH } = await import("./sentry-tunnel");
    // One call: the replay integration refuses a second instance in the same page.
    const options = browserSentryOptions("https://k@bugsink.example/4");
    expect(Object.keys(options).sort()).toEqual(
      [
        "beforeSend",
        "dsn",
        "environment",
        "integrations",
        "release",
        "replaysOnErrorSampleRate",
        "replaysSessionSampleRate",
        "tunnel",
      ].sort(),
    );
    expect(options).not.toHaveProperty("tracesSampleRate");
    expect(options).not.toHaveProperty("tracesSampler");
    const integrations = Array.isArray(options.integrations) ? options.integrations : [];
    expect(integrations.map((integration) => integration.name)).toEqual(["Replay"]);
    expect(options.dsn).toBe("https://k@bugsink.example/4");
    expect(options.tunnel).toBe(SENTRY_TUNNEL_PATH);
    expect(options.replaysSessionSampleRate).toBe(0);
    expect(options.replaysOnErrorSampleRate).toBe(1);
    expect(options.beforeSend).toBe(beforeSend);
    expect(options.environment).toBe(import.meta.env.MODE);
    expect(options.release).toBe(sentryRelease);
  });
});

describe("workerSentryOptions", () => {
  it("reports errors and does not trace", async () => {
    const { beforeSend, sentryRelease, workerSentryOptions } = await import("./sentry");
    const options = workerSentryOptions("https://k@bugsink.example/4");
    expect(Object.keys(options).sort()).toEqual(
      ["beforeSend", "dsn", "environment", "release"].sort(),
    );
    expect(options).not.toHaveProperty("tracesSampleRate");
    expect(options).not.toHaveProperty("tracesSampler");
    expect(options.dsn).toBe("https://k@bugsink.example/4");
    expect(options.beforeSend).toBe(beforeSend);
    expect(options.environment).toBe(import.meta.env.MODE);
    expect(options.release).toBe(sentryRelease);
  });

  it("leaves the DSN unset, and so the SDK off, when the Worker has none", async () => {
    const { workerSentryOptions } = await import("./sentry");
    expect(workerSentryOptions(undefined).dsn).toBeUndefined();
  });
});
