import './i18n';
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router";
import { Toaster } from "sonner";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { router } from "./router";
import { AVATAR_RESIZE_TARGET, backgroundResizeTarget } from "./lib/imageResize";
import { compactStoredImage, IMAGE_STORAGE_KEYS } from "./lib/storage";
import "highlight.js/styles/github-dark-dimmed.min.css";
import "./index.css";

// Re-encode any image stored before downscaling existed. A legacy
// full-resolution blob can occupy the whole localStorage quota on its own,
// which makes every later write fail — including a 30KB avatar — so this is
// what unwedges an origin that already has one.
void (async () => {
  const targets: Record<string, { maxWidth: number; maxHeight: number }> = {
    "qa-agent-avatar": AVATAR_RESIZE_TARGET,
    "qa-user-avatar": AVATAR_RESIZE_TARGET,
    "qa-background": backgroundResizeTarget(),
  };
  let compacted = false;
  for (const key of IMAGE_STORAGE_KEYS) {
    if (await compactStoredImage(key, targets[key])) compacted = true;
  }
  if (compacted) {
    // The surfaces that read these keys hold the pre-compaction value; tell
    // them to re-read so the smaller blob is what they render.
    window.dispatchEvent(new CustomEvent("avatar-changed", { detail: { tab: "user" } }));
    window.dispatchEvent(new CustomEvent("avatar-changed", { detail: { tab: "agent" } }));
  }
})();

const prefetchMiniEquityChart = () => {
  void import("@/components/charts/MiniEquityChart");
};

const idleWindow = window as Window & {
  requestIdleCallback?: (
    callback: IdleRequestCallback,
    options?: IdleRequestOptions,
  ) => number;
};

if (typeof idleWindow.requestIdleCallback === "function") {
  idleWindow.requestIdleCallback(prefetchMiniEquityChart, { timeout: 2000 });
} else {
  window.setTimeout(prefetchMiniEquityChart, 0);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <RouterProvider router={router} />
      <Toaster position="bottom-right" richColors closeButton duration={3500} />
    </ErrorBoundary>
  </StrictMode>
);
