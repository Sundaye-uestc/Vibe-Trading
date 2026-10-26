// Vitest aliases `window` to the Node global. Recent Node versions already
// expose Web Storage there, so Vitest may leave Node's storage in place instead
// of installing jsdom's browser implementation. Bind the real jsdom storage
// before application imports (including i18n's language detector) run.
const browserWindow = (globalThis as typeof globalThis & {
  jsdom?: { window: Window };
}).jsdom?.window;

// SSR tests explicitly select the Node environment and have no browser.
if (browserWindow) {
  for (const key of ["localStorage", "sessionStorage"] as const) {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      enumerable: true,
      writable: true,
      value: browserWindow[key],
    });
  }
}

export {};
