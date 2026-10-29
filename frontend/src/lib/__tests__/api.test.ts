import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";

async function loadApiModule() {
  vi.resetModules();
  return import("../api");
}

describe("api request helper", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => ""),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("shows validation explanations without echoing rejected input", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [
      { loc: ["body", "from_date"], msg: "Dates must use YYYY-MM-DD", input: "private-input" },
      { loc: ["body", "symbols", 0], msg: "Invalid symbol", ctx: { secret: "private-input" } },
    ] }), { status: 422, headers: { "content-type": "application/json" } })));
    const { paperApi } = await loadApiModule();
    await expect(paperApi.preflight({ market: "cn_a", from_date: "", to_date: "", symbols: [] })).rejects.toMatchObject({
      status: 422, message: "from_date: Dates must use YYYY-MM-DD; symbols.0: Invalid symbol",
    });
  });

  it("keeps a readable HTTP fallback for unrecognized error objects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { input: "private-input" } }), { status: 422 })));
    const { paperApi } = await loadApiModule();
    await expect(paperApi.list()).rejects.toMatchObject({ status: 422, message: "HTTP 422" });
  });

  it("rejects an incomplete data-check response instead of crashing the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ partial: { status: "blocked" } }),
      { status: 200, headers: { "content-type": "application/json" } })));
    const { paperApi } = await loadApiModule();
    await expect(paperApi.preflight({ market: "cn_a", from_date: "2026-10-26", to_date: "2026-10-28", symbols: [] }))
      .rejects.toMatchObject({ status: 502, message: expect.stringContaining("Invalid data-check response") });
  });

  it("rejects incomplete watchlist status responses before they reach the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ configured: true }),
      { status: 200, headers: { "content-type": "application/json" } })));
    const { paperApi } = await loadApiModule();
    await expect(paperApi.dataStatus()).rejects.toMatchObject({ status: 502, message: expect.stringContaining("Invalid market-data status") });
  });

  it("updates only the saved watchlist without accepting source credentials or a date override", async () => {
    const status = { configured: true, update_available: true, status_code: "ready", watchlist: ["600522.SH"],
      latest_closed_date: "2026-10-28", coverage: [], last_job: null, limits: { max_symbols: 10, lookback_days: 200 } };
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(status), {
      status: 200, headers: { "content-type": "application/json" },
    })));
    vi.stubGlobal("fetch", fetchMock);
    const { paperApi } = await loadApiModule();
    await paperApi.saveWatchlist(["600522.SH"]);
    await paperApi.updateData();
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/paper-data/watchlist", expect.objectContaining({ method: "PUT", body: '{"symbols":["600522.SH"]}' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/paper-data/update", expect.objectContaining({ method: "POST", body: "{}" }));
  });

  it("rejects non-JSON responses with a descriptive error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><html><body>SPA</body></html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
      ),
    );

    const { api } = await loadApiModule();

    await expect(api.getChannelStatus()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      message: expect.stringContaining("Expected JSON from /channels/status, got text/html"),
    } satisfies Partial<ApiError>);
  });

  it("posts an authenticated connector verification request with an encoded profile id", async () => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => "remote-test-key"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", connection_state: "connected" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { api } = await loadApiModule();

    await expect(api.verifyConnector("longbridge/live sdk")).resolves.toMatchObject({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/live/connectors/longbridge%2Flive%20sdk/verify?force=true",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer remote-test-key" }),
      }),
    );
  });

  it("sends the stored API key when fetching a correlation matrix", async () => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => "remote-test-key"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ labels: ["A", "B"], matrix: [[1, 0], [0, 1]] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { api } = await loadApiModule();

    await expect(api.getCorrelation("A,B", 90, "pearson")).resolves.toEqual({
      labels: ["A", "B"],
      matrix: [[1, 0], [0, 1]],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/correlation?codes=A%2CB&days=90&method=pearson",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer remote-test-key" }),
      }),
    );
  });

  it("sends the stored API key when fetching a correlation regime timeline", async () => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => "remote-test-key"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    const regime = {
      labels: ["A", "B"],
      dates: ["2024-01-01"],
      density: [0.5],
      smoothed: [0.5],
      fused: [0],
      episodes: [],
      params: {},
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(regime), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { api } = await loadApiModule();

    await expect(api.getCorrelationRegime("A,B", 90)).resolves.toEqual(regime);
    expect(fetchMock).toHaveBeenCalledWith(
      "/correlation/regime?codes=A%2CB&days=90",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer remote-test-key" }),
      }),
    );
  });

  it("resolves authorization errors through the active i18n key", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "server fallback" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    const apiModule = await loadApiModule();
    const { default: i18n } = await import("@/i18n");
    i18n.addResource(
      "en",
      "translation",
      "agent.authRequired",
      "Add an API key in Settings.",
    );
    await i18n.changeLanguage("en");

    expect(apiModule.AUTH_REQUIRED_MESSAGE).toBe("Add an API key in Settings.");
    await expect(apiModule.api.getCorrelation("A,B", 90, "pearson")).rejects.toMatchObject({
      status: 401,
      message: "Add an API key in Settings.",
    } satisfies Partial<ApiError>);
  });
});
