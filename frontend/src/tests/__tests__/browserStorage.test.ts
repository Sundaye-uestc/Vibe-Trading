const browserWindow = (globalThis as typeof globalThis & {
  jsdom: { window: Window };
}).jsdom.window;

describe("browser storage test environment", () => {
  afterEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it.each(["localStorage", "sessionStorage"] as const)("binds %s to the real jsdom Storage", (key) => {
    expect(globalThis[key]).toBe(browserWindow[key]);
    expect(window[key]).toBe(browserWindow[key]);
    expect(globalThis[key]).toBeInstanceOf(Storage);
    browserWindow[key].setItem("browser-key", "browser-value");
    expect(window[key].getItem("browser-key")).toBe("browser-value");
    globalThis[key].removeItem("browser-key");
    expect(browserWindow[key].getItem("browser-key")).toBeNull();
  });

  it("keeps local storage and session storage independent", () => {
    localStorage.setItem("same-key", "local");
    sessionStorage.setItem("same-key", "session");
    expect(localStorage.getItem("same-key")).toBe("local");
    expect(sessionStorage.getItem("same-key")).toBe("session");
  });
});

export {};
