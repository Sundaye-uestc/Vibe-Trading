import { act, render, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Agent } from "../Agent";
import { useAgentStore } from "@/stores/agent";

const apiMock = vi.hoisted(() => ({
  getGoal: vi.fn(),
  getLLMSettings: vi.fn(),
  getRun: vi.fn(),
  getSession: vi.fn(),
  getSessionMessages: vi.fn(),
  sseUrl: vi.fn((sid: string) => `/sessions/${sid}/events`),
}));
const sseMock = vi.hoisted(() => ({
  connect: vi.fn(), disconnect: vi.fn(), onStatusChange: vi.fn(),
}));
vi.mock("@/lib/api", async () => ({
  ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), api: apiMock,
}));
vi.mock("@/hooks/useSSE", () => ({ useSSE: () => sseMock }));

async function renderSession() {
  const router = createMemoryRouter([{ path: "/", element: <Agent /> }], {
    initialEntries: ["/?session=session-partial"],
  });
  render(<RouterProvider router={router} />);
  await waitFor(() => expect(sseMock.connect).toHaveBeenCalled());
}

describe("Degraded session completion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAgentStore.getState().reset();
    apiMock.getGoal.mockResolvedValue(null);
    apiMock.getSession.mockResolvedValue({});
    apiMock.getSessionMessages.mockResolvedValue([]);
    apiMock.getRun.mockResolvedValue({});
    apiMock.getLLMSettings.mockResolvedValue({
      provider: "deepseek", model_name: "deepseek-flash", providers: [],
      api_key_configured: true, api_key_required: true, sse_timeout_seconds: 90,
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true, value: vi.fn(),
    });
  });

  it.each([true, false])("hydrates degraded=%s without presenting it as full success", async (degraded) => {
    apiMock.getSessionMessages.mockResolvedValue([{
      message_id: "reply", session_id: "session-partial", role: "assistant",
      content: "Retained observations", created_at: "2026-11-12T00:00:00Z",
      linked_attempt_id: "attempt-partial", tool_trail: [],
      metadata: { status: "completed", degraded, run_id: "run-partial", elapsed_ms: 2000 },
    }]);
    await renderSession();
    await waitFor(() => {
      const messages = useAgentStore.getState().messages;
      expect(messages.find((msg) => msg.meta?.activity)?.meta?.activity?.state)
        .toBe(degraded ? "partial" : "done");
      expect(messages.find((msg) => msg.type === "answer")?.degraded).toBe(degraded);
    });
  });

  it("keeps a local identity preflight distinct from an upstream failure in live steps", async () => {
    await renderSession();
    await waitFor(() => expect(useAgentStore.getState().sessionLoading).toBe(false));
    const handlers = sseMock.connect.mock.calls.at(-1)?.[1] as Record<string, (data: Record<string, unknown>) => void>;
    act(() => {
      useAgentStore.getState().startActivity("attempt-check", 1000);
      useAgentStore.getState().setStatus("streaming");
      for (const outcome of ["needs_resolution", "no_data", "error"]) {
        const event = { attempt_id: "attempt-check", tool: "get_market_data", call_id: outcome };
        handlers.tool_call(event);
        handlers.tool_result({ ...event, status: "error", outcome });
      }
    });
    expect(useAgentStore.getState().toolCalls.map((entry) => entry.status)).toEqual(["needs_resolution", "no_data", "error"]);
  });

  it("archives a live degraded completion once and preserves its partial answer metadata", async () => {
    await renderSession();
    await waitFor(() => expect(useAgentStore.getState().sessionLoading).toBe(false));
    act(() => {
      useAgentStore.getState().startActivity("attempt-partial", 1000);
      useAgentStore.getState().setStatus("streaming");
    });
    const handlers = sseMock.connect.mock.calls.at(-1)?.[1] as Record<string, (data: Record<string, unknown>) => Promise<void>>;
    const completed = {
      attempt_id: "attempt-partial", status: "completed", degraded: true,
      summary: "Retained observations", started_at: 1, ended_at: 3, elapsed_ms: 2000,
    };
    await act(async () => { await handlers["attempt.completed"](completed); });
    await act(async () => { await handlers["attempt.completed"](completed); });
    const state = useAgentStore.getState();
    expect(state.activity).toBeNull();
    expect(state.status).toBe("idle");
    const activities = state.messages.filter((msg) => msg.meta?.activity);
    expect(activities).toHaveLength(1);
    expect(activities[0].meta?.activity).toMatchObject({ state: "partial", endedAt: 3000 });
    const answers = state.messages.filter((msg) => msg.type === "answer");
    expect(answers).toHaveLength(1);
    expect(answers[0]).toMatchObject({ degraded: true, content: "Retained observations" });
  });
});
