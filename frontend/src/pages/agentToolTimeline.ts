import {
  deriveActivityVerb,
  type ActivityState,
  type AgentActivity,
  type StoredAgentMessage,
} from "@/stores/agent";
import type { ToolFailureOutcome, ToolStepStatus } from "@/types/agent";

export interface ToolTimelineEntry {
  id?: string;
  call_id?: string;
  tool: string;
  arguments?: Record<string, string>;
  status?: ToolStepStatus;
  outcome?: ToolFailureOutcome;
  preview?: string;
  elapsed_ms?: number;
  timestamp?: number;
}

// Older sessions kept the first 200 characters of the locally generated
// denial. Require its exact envelope and message prefix, plus zero runtime.
// A source timeout, ambiguous provider search or free-text error stays red.
const legacyIdentityDenials = [
  ["identity_required", "A canonical, non-conflicting identity was not locked"],
  ["identity_conflict", "A canonical, non-conflicting identity was not locked"],
  ["identity_mismatch", "Consumer symbol/venue differs from the locked resolver identity"],
  ["identity_required", "Market-sensitive workflow selection is blocked"],
  ["identity_required", "Private-company routing requires a completed resolver result"],
  ["identity_conflict", "A resolver has locked this entity to a listed security"],
];

function historicalStepStatus(entry: ToolTimelineEntry): ToolStepStatus {
  // Previously an exhausted market lookup could be stored as successful.
  // Only a complete legacy payload with no symbol panel establishes this.
  if (entry.status === "ok" && entry.tool === "get_market_data" && entry.preview?.includes('"_unresolved"')) {
    try {
      const data = JSON.parse(entry.preview);
      if (data && Array.isArray(data._unresolved) && data._unresolved.length &&
          Object.keys(data).every((key) => key.startsWith("_"))) {
        return data._source_error === true ? "error" : "no_data";
      }
    } catch { /* Truncated previews keep their recorded status. */ }
  }
  if (entry.status !== "error") return entry.status || "ok";
  if (entry.outcome) return entry.outcome;
  if (entry.elapsed_ms === 0 && entry.preview && legacyIdentityDenials.some(([code, message]) =>
    entry.preview!.startsWith(`{"status": "error", "error_code": "${code}", "tool": ${JSON.stringify(entry.tool)}, "message": "${message}`),
  )) return "needs_resolution";
  return "error";
}

interface ToolTimelineOptions {
  fallbackTimestamp?: number;
  idPrefix?: string;
  attemptId?: string;
  state?: ActivityState;
  /** Attempt wall-clock start (ms). When known it beats the first-tool guess. */
  startedAt?: number;
  endedAt?: number;
  activity?: AgentActivity;
}

/** Convert one attempt's consolidated tool records into its durable activity row. */
export function buildToolTimelineMessages(
  entries: readonly ToolTimelineEntry[],
  options: ToolTimelineOptions = {},
): StoredAgentMessage[] {
  const fallbackTimestamp = options.fallbackTimestamp ?? Date.now();
  const idPrefix = options.idPrefix ?? "";
  const steps = entries.map((entry, index) => ({
    id: entry.id || entry.call_id || `${entry.tool}#${index + 1}`,
    tool: entry.tool,
    arguments: entry.arguments ?? {},
    status: historicalStepStatus(entry),
    preview: entry.preview,
    elapsed_ms: entry.elapsed_ms,
    timestamp: (
      typeof entry.timestamp === "number" && Number.isFinite(entry.timestamp)
        ? entry.timestamp
        : fallbackTimestamp + index
    ),
  }));
  // The first tool call is only a lower bound on when the attempt started: the
  // model thinks before it reaches for a tool, and a pure-text turn has no
  // tools at all. Prefer the attempt's recorded start when the caller has it.
  const inferredStart = steps.length > 0
    ? Math.min(...steps.map((step) => step.timestamp))
    : fallbackTimestamp;
  const startedAt = (
    typeof options.startedAt === "number" && Number.isFinite(options.startedAt)
      ? Math.min(options.startedAt, inferredStart)
      : inferredStart
  );
  const inferredEnd = Math.max(
    fallbackTimestamp,
    startedAt + steps.reduce((sum, step) => sum + (step.elapsed_ms ?? 0), 0),
  );
  const activity: AgentActivity = options.activity ?? {
    attemptId: options.attemptId || `${idPrefix}attempt`,
    state: options.state ?? "done",
    verb: deriveActivityVerb(steps[steps.length - 1]?.tool),
    steps,
    startedAt,
    endedAt: options.endedAt ?? inferredEnd,
  };

  return [{
    id: `${idPrefix}activity_${activity.attemptId}`,
    type: "thinking",
    content: "",
    meta: { activity },
    timestamp: activity.startedAt,
  }];
}
