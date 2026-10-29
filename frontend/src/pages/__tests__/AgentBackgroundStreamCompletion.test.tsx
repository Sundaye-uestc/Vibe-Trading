import { act, cleanup, render, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Agent } from "../Agent";
import { useAgentStore } from "@/stores/agent";

// Regression test for: a session's sidebar "thinking" spinner (driven by
// streamingSessionId) never clears once you navigate away from it mid-turn,
// even long after that turn actually finishes. The SSE stream for a
// backgrounded session is torn down the moment you switch away
// (doDisconnect() in the session-switch effect), so the session_completed
// event that would normally clear streamingSessionId never arrives. Reopening
// that session must reconcile the marker against its now-committed history
// instead of leaving it stuck for the rest of the tab's life.

const apiMock = vi.hoisted(() => ({
  getGoal: vi.fn(),
  getLLMSettings: vi.fn(),
  getRun: vi.fn(),
  getSession: vi.fn(),
  getSessionMessages: vi.fn(),
  sseUrl: vi.fn((sid: string) => `/sessions/${sid}/events`),
}));

const sseMock = vi.hoisted(() => ({
  connect: vi.fn(),
  disconnect: vi.fn(),
  onStatusChange: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: apiMock };
});

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => sseMock,
}));

function storedReply(sessionId: string) {
  return {
    message_id: `${sessionId}-reply`,
    session_id: sessionId,
    role: "assistant" as const,
    content: "done",
    created_at: "2026-08-01T00:00:00Z",
    linked_attempt_id: `${sessionId}-attempt`,
    tool_trail: [],
    metadata: {},
  };
}

function storedPrompt(sessionId: string) {
  return {
    message_id: `${sessionId}-prompt`,
    session_id: sessionId,
    role: "user" as const,
    content: "still working on it",
    created_at: "2026-08-01T00:00:00Z",
    linked_attempt_id: null,
    tool_trail: [],
    metadata: null,
  };
}

describe("Agent background stream completion reconciliation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAgentStore.getState().reset();
    apiMock.getSession.mockResolvedValue({ reasoning_effort: null });
    apiMock.getGoal.mockResolvedValue(null);
    apiMock.getLLMSettings.mockResolvedValue({
      provider: "deepseek",
      model_name: "deepseek-v4-pro",
      base_url: "https://api.deepseek.com/v1",
      api_key_configured: true,
      api_key_required: true,
      temperature: 0,
      timeout_seconds: 120,
      max_retries: 2,
      reasoning_effort: "low",
      sse_timeout_seconds: 90,
      env_path: "agent/.env",
      providers: [],
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("clears a background session's stale streamingSessionId once its reply is committed", async () => {
    apiMock.getSessionMessages.mockImplementation((sid: string) => (
      sid === "session-bg" ? Promise.resolve([storedReply(sid)]) : Promise.resolve([])
    ));

    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    render(<RouterProvider router={router} />);
    await waitFor(() => expect(apiMock.getSessionMessages).toHaveBeenCalledWith("session-one"));

    // Simulate: session-bg was mid-turn when the user navigated away from it earlier.
    act(() => {
      useAgentStore.setState({ streamingSessionId: "session-bg" });
    });

    await act(async () => {
      await router.navigate("/?session=session-bg");
    });

    await waitFor(() => {
      expect(useAgentStore.getState().streamingSessionId).toBeNull();
    });
  });

  it("leaves streamingSessionId alone when the background session genuinely has no reply yet", async () => {
    apiMock.getSessionMessages.mockImplementation((sid: string) => (
      sid === "session-bg" ? Promise.resolve([storedPrompt(sid)]) : Promise.resolve([])
    ));

    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    render(<RouterProvider router={router} />);
    await waitFor(() => expect(apiMock.getSessionMessages).toHaveBeenCalledWith("session-one"));

    act(() => {
      useAgentStore.setState({ streamingSessionId: "session-bg" });
    });

    await act(async () => {
      await router.navigate("/?session=session-bg");
    });

    await waitFor(() => {
      expect(apiMock.getSessionMessages).toHaveBeenCalledWith("session-bg");
    });
    expect(useAgentStore.getState().streamingSessionId).toBe("session-bg");
  });

  it.each(["history", "cache"])("cancels pending %s scroll work when the page unmounts", async (source) => {
    vi.useFakeTimers();
    apiMock.getSessionMessages.mockImplementation(() => source === "history"
      ? Promise.resolve([storedReply("session-one")])
      : new Promise(() => {}));
    if (source === "cache") {
      useAgentStore.getState().cacheSession("session-one", [{
        id: "cached", type: "answer", content: "cached answer", timestamp: 1,
      }]);
    }
    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    const view = render(<RouterProvider router={router} />);
    await act(async () => {});
    expect(useAgentStore.getState().messages.length).toBeGreaterThan(0);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    const frame = vi.spyOn(window, "requestAnimationFrame");
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(frame).not.toHaveBeenCalled();
  });

  it("does not schedule scroll work or update history when a request resolves after unmount", async () => {
    vi.useFakeTimers();
    let resolveMessages!: (value: ReturnType<typeof storedReply>[]) => void;
    apiMock.getSessionMessages.mockReturnValue(new Promise((resolve) => { resolveMessages = resolve; }));
    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    const view = render(<RouterProvider router={router} />);
    await act(async () => {});
    view.unmount();
    const before = useAgentStore.getState().messages;
    const frame = vi.spyOn(window, "requestAnimationFrame");
    await act(async () => { resolveMessages([storedReply("session-one")]); });
    expect(useAgentStore.getState().messages).toBe(before);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(frame).not.toHaveBeenCalled();
  });

  it("cancels a previous session's delayed scroll while the next history request is pending", async () => {
    vi.useFakeTimers();
    apiMock.getSessionMessages.mockImplementation((sid: string) => sid === "session-one"
      ? Promise.resolve([storedReply(sid)])
      : new Promise(() => {}));
    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    render(<RouterProvider router={router} />);
    await act(async () => {});
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await act(async () => { await router.navigate("/?session=session-two"); });
    const frame = vi.spyOn(window, "requestAnimationFrame");
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(frame).not.toHaveBeenCalled();
    expect(useAgentStore.getState().sessionId).toBe("session-two");
    expect(useAgentStore.getState().messages).toEqual([]);
  });

  it("ignores a previous session's history response after switching sessions", async () => {
    vi.useFakeTimers();
    let resolveOld!: (value: ReturnType<typeof storedReply>[]) => void;
    apiMock.getSessionMessages.mockImplementation((sid: string) => sid === "session-one"
      ? new Promise((resolve) => { resolveOld = resolve; })
      : Promise.resolve([storedReply(sid)]));
    const router = createMemoryRouter(
      [{ path: "/", element: <Agent /> }],
      { initialEntries: ["/?session=session-one"] },
    );
    render(<RouterProvider router={router} />);
    await act(async () => { await router.navigate("/?session=session-two"); });
    await act(async () => { vi.advanceTimersByTime(100); });
    const current = useAgentStore.getState().messages;
    expect(current.some((message) => message.id === "session-two-reply")).toBe(true);
    const frame = vi.spyOn(window, "requestAnimationFrame");
    await act(async () => { resolveOld([storedReply("session-one")]); });
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(useAgentStore.getState().messages).toBe(current);
    expect(frame).not.toHaveBeenCalled();
  });
});
