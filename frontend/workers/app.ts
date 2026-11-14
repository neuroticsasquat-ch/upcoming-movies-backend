import { createRequestHandler, RouterContextProvider } from "react-router";
import * as Sentry from "@sentry/cloudflare";
import { type PageCache, serveEntityPage } from "@/lib/edge-cache";
import { cloudflareContext } from "@/lib/load-context";
import { sentryRelease, workerSentryOptions } from "@/lib/sentry";
import { SENTRY_TUNNEL_PATH, sentryTunnelUpstream } from "@/lib/sentry-tunnel";
import { throwIfWorkerTestError } from "@/lib/test-error";

// `SENTRY_DSN` is a Wrangler secret (`wrangler secret put SENTRY_DSN`), not a var in
// wrangler.jsonc, so the Bugsink DSN is set in Cloudflare and survives deploys (NEU-1594).
// `wrangler types` only sees vars, hence the declaration here. Unset → Sentry is disabled and
// the tunnel rejects every envelope. `SENTRY_TEST_ERROR_TOKEN` is a secret too: it arms the
// `?__test_error=<token>` trigger, which is off while it is unset.
type WorkerEnv = Env & { SENTRY_DSN?: string; SENTRY_TEST_ERROR_TOKEN?: string };

// The Worker's own cache, read per request rather than at module load. The tsconfig's DOM lib
// declares `CacheStorage` too, and its interface shadows the Workers `default` property, hence
// the cast.
function edgeCache(): PageCache {
  return (caches as unknown as { default: PageCache }).default;
}

const requestHandler = createRequestHandler(
  () => import("virtual:react-router/server-build"),
  import.meta.env.MODE,
);

// Proxy a browser Sentry envelope on to Bugsink's ingest endpoint (the `tunnel` target set in
// entry.client.tsx). Same-origin so ad blockers don't drop it; validated against our own
// DSN so it can't be used as an open relay.
//
// Work in raw bytes, not text: replay envelopes carry a binary (compressed) recording
// item, and a UTF-8 round-trip via request.text() would corrupt it (the ingest then rejects
// the envelope with "missing newline after payload"). We decode only the header line
// (ASCII JSON, up to the first newline) for validation and forward the bytes untouched.
async function handleSentryTunnel(request: Request, dsn: string): Promise<Response> {
  const body = await request.arrayBuffer();
  const bytes = new Uint8Array(body);
  const newline = bytes.indexOf(0x0a); // first "\n" — end of the envelope header
  const envelopeHeader = new TextDecoder().decode(
    newline === -1 ? bytes : bytes.subarray(0, newline),
  );
  const upstream = sentryTunnelUpstream(dsn, envelopeHeader);
  if (!upstream) return new Response("invalid envelope", { status: 400 });
  return fetch(upstream, {
    method: "POST",
    body,
    headers: { "Content-Type": "application/x-sentry-envelope" },
  });
}

const handler = {
  async fetch(request: Request, env: WorkerEnv, ctx: ExecutionContext) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === SENTRY_TUNNEL_PATH) {
      return handleSentryTunnel(request, env.SENTRY_DSN ?? "");
    }
    // Inside `withSentry`, so the throw is reported: confirms the Worker reaches Bugsink.
    throwIfWorkerTestError(url, env.SENTRY_TEST_ERROR_TOKEN);
    // Entity pages are served from the edge cache unless the request carries the timeline hint
    // (NEU-2272, ADR-0029); every other path renders as before. The key carries the build SHA,
    // so the cache is off in local dev, where there is none.
    return serveEntityPage(request, {
      cache: edgeCache(),
      buildSha: sentryRelease,
      waitUntil: ctx.waitUntil.bind(ctx),
      render: () => {
        const context = new RouterContextProvider();
        context.set(cloudflareContext, { env });
        return requestHandler(request, context);
      },
    });
  },
} satisfies ExportedHandler<WorkerEnv>;

// Wrap the SSR fetch handler so errors that escape it are reported; loader and render errors
// React Router catches itself go through `handleError` in entry.server.tsx (NEU-2273).
// DSN comes from the Worker env (the `SENTRY_DSN` secret); unset → disabled. The options,
// errors only and no tracing, are in `lib/sentry.ts`, beside the browser's.
export default Sentry.withSentry(
  (env: WorkerEnv) => workerSentryOptions(env.SENTRY_DSN),
  handler,
);
