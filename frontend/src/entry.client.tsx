import { startTransition, StrictMode } from "react";
import { hydrateRoot } from "react-dom/client";
import { HydratedRouter } from "react-router/dom";
import * as Sentry from "@sentry/react";
import { browserSentryOptions, shouldInitBrowserSentry } from "@/lib/sentry";

const dsn = import.meta.env.VITE_SENTRY_DSN;
if (dsn && shouldInitBrowserSentry(navigator.userAgent)) {
  Sentry.init(browserSentryOptions(dsn));
}

startTransition(() => {
  hydrateRoot(
    document,
    <StrictMode>
      <HydratedRouter />
    </StrictMode>,
  );
});
