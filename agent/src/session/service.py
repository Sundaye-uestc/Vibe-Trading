"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.session.checkpoint import ResponseCheckpoint
from src.session.events import EventBus
from src.session.models import (
    Attempt,
    AttemptStatus,
    Message,
    Principal,
    Session,
)
from src.session.search import get_shared_index
from src.session.store import SessionStore

if TYPE_CHECKING:
    from src.agent.loop import AgentLoop

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
logger = logging.getLogger(__name__)


# A failed long run must leave enough bounded evidence for a follow-up to
# continue, while keeping the persisted transcript and the next prompt small.
# The complete tool result remains in the run trace/sidecars; these limits are
# only for the session-level recovery handoff.
_RECOVERY_TRAIL_MAX_ENTRIES = 64
_RECOVERY_ARG_MAX_CHARS = 2_000
_RECOVERY_PREVIEW_MAX_CHARS = 1_000
_RECOVERY_HISTORY_MAX_ENTRIES = 16
_RECOVERY_HISTORY_MAX_CHARS = 5_000
_COMPACT_RECOVERY_MAX_ENTRIES = 8
_COMPACT_RECOVERY_MAX_CHARS = 12_000


def _message_field(message: Any, name: str, default: Any = None) -> Any:
    """Read persisted messages and legacy dictionary records consistently."""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _message_metadata(message: Any) -> Dict[str, Any]:
    metadata = _message_field(message, "metadata")
    return metadata if isinstance(metadata, dict) else {}


#: Terminal attempt status -> SSE event name. Cancellation is its own event so
#: the UI can distinguish a user stop from a failure, both live and on reload.
_TERMINAL_EVENTS = {
    "completed": "attempt.completed",
    "cancelled": "attempt.cancelled",
    "failed": "attempt.failed",
}


class SessionBusyError(RuntimeError):
    """Raised when a session already has an in-flight run.

    One AgentLoop per session: a second concurrent send is rejected rather
    than queued, because two loops writing the same session would interleave
    their messages and attempts. Callers surface this as HTTP 409 so the user
    can wait for the running attempt or cancel it first.
    """


class SessionService:
    """Session lifecycle service.

    Attributes:
        store: Session persistence store.
        event_bus: SSE event bus.
        runs_dir: Root runs directory.
    """

    def __init__(
        self,
        store: SessionStore,
        event_bus: EventBus,
        runs_dir: Path,
    ) -> None:
        """Initialize the session service.

        Args:
            store: Session persistence store.
            event_bus: SSE event bus.
            runs_dir: Root runs directory.
        """
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        # _active_loops is the cancellation handle only. It is populated after
        # the registry is built, which is far too late to serve as the
        # concurrency gate, so in-flight sessions are tracked separately and
        # reserved synchronously in send_message.
        self._active_loops: Dict[str, "AgentLoop"] = {}
        # Task handles are kept from the moment the run is scheduled, so a run
        # still building its registry can be cancelled. _active_loops only
        # exists once construction finished, which is far too late to be the
        # only cancellation route: a hung discovery would otherwise hold the
        # claim forever and lock the session behind 409.
        self._active_tasks: Dict[str, "asyncio.Task"] = {}
        # Only task cancellation requested through cancel_current is a user
        # cancellation. Event-loop shutdown also raises CancelledError, but
        # that attempt must remain recoverable on the next process start.
        self._user_cancel_requests: set[str] = set()
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._search_index = get_shared_index()
        self._recover_interrupted_attempts()
        self._warn_about_index_only_sessions()

    def _warn_about_index_only_sessions(self) -> None:
        """Warn when the index lists sessions that no longer exist on disk.

        Session directories are the source of truth for full message bodies;
        the index only mirrors them. An index row with no directory means the
        session is still findable by cross-session search but cannot be opened
        — the silent state a past data loss left behind. Reporting it keeps the
        next occurrence visible instead of letting it pass unnoticed.
        """
        try:
            indexed = self._search_index.indexed_session_ids()
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not block startup
            logger.warning("Session index consistency check skipped: %s", exc)
            return
        if not indexed:
            return
        on_disk: set[str] = set()
        if self.store.base_dir.exists():
            on_disk = {
                entry.name
                for entry in self.store.base_dir.iterdir()
                if entry.is_dir() and (entry / "session.json").is_file()
            }
        missing = sorted(indexed - on_disk)
        if missing:
            logger.warning(
                "Search index lists %d session(s) with no directory on disk "
                "(searchable but unopenable): %s",
                len(missing),
                ", ".join(missing),
            )

    def _recover_interrupted_attempts(self) -> None:
        """Finalize attempts that could not outlive the previous process.

        A newly constructed service has no live AgentLoop instances. Any
        attempt still marked pending or running therefore belongs to the
        previous process. Convert it to an explicit terminal state and expose
        the most recent durable response snapshot in conversation history.
        """
        recoverable = {AttemptStatus.PENDING, AttemptStatus.RUNNING}
        for attempt in self.store.list_attempts():
            if attempt.status not in recoverable:
                continue
            partial = self.store.get_partial_response(
                attempt.session_id, attempt.attempt_id
            )
            has_partial = bool(partial)
            existing_reply = self.store.get_message_for_attempt(
                attempt.session_id, attempt.attempt_id
            )
            if existing_reply is None:
                reply = Message(
                    session_id=attempt.session_id,
                    role="assistant",
                    content=self._format_interrupted_message(partial),
                    linked_attempt_id=attempt.attempt_id,
                    metadata={
                        "status": AttemptStatus.INTERRUPTED.value,
                        "partial": has_partial,
                        "recovery_reason": "service_restart",
                    },
                )
                self.store.append_message(reply)
                self._search_index.index_message(
                    attempt.session_id, "assistant", reply.content
                )
                attempt.mark_interrupted("service restarted before attempt completed")
            else:
                # The append-only reply is committed before attempt.json. If a
                # process exits between those writes, finish the second half
                # from the reply's terminal metadata instead of mislabelling a
                # complete response as interrupted.
                reply_status = existing_reply.metadata.get("status")
                if reply_status == AttemptStatus.COMPLETED.value:
                    attempt.mark_completed(summary=existing_reply.content)
                elif reply_status == AttemptStatus.CANCELLED.value:
                    attempt.mark_cancelled(reason="cancelled by user")
                elif reply_status == AttemptStatus.FAILED.value:
                    attempt.mark_failed(error="execution failed")
                else:
                    attempt.mark_interrupted(
                        "service restarted before attempt completed"
                    )
            self.store.update_attempt(attempt)
            self.store.delete_partial_response(attempt.session_id, attempt.attempt_id)

    @staticmethod
    def _format_interrupted_message(partial: Optional[str]) -> str:
        """Build the transcript message for a recovered attempt.

        Args:
            partial: Durable assistant text captured before restart.

        Returns:
            User-facing interruption notice, including partial text when any.
        """
        if partial:
            return (
                "Vibe Trading restarted before this response finished. "
                "The partial response recovered below may be incomplete:\n\n"
                f"{partial}\n\n"
                "Review it before sending a follow-up or retrying the request."
            )
        return (
            "Vibe Trading restarted before this response finished, so no "
            "complete assistant response was saved. Review any completed tool "
            "actions before retrying the request."
        )

    def _reserve_session(self, session_id: str) -> None:
        """Claim a session for one in-flight run.

        Args:
            session_id: Session to claim.

        Raises:
            SessionBusyError: If the session is already claimed.
        """
        with self._inflight_lock:
            if session_id in self._inflight:
                raise SessionBusyError(
                    f"Session {session_id} already has a run in progress"
                )
            self._inflight.add(session_id)

    def _release_session(self, session_id: str) -> None:
        """Release a session claim. Safe to call when no claim is held.

        Args:
            session_id: Session to release.
        """
        with self._inflight_lock:
            self._inflight.discard(session_id)

    def create_session(
        self,
        title: str = "",
        config: Optional[Dict[str, Any]] = None,
        owner: Optional["Principal"] = None,
    ) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.
            owner: Principal the session belongs to, from the authenticated
                request. Optional because sessions are also created by the CLI
                and by internal paths that have no request context; those get
                ``None``, which reads as "owner unknown" and is deliberately
                distinct from a principal that authenticated but cannot be
                attributed to a person (see ``Principal.attributable``).

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {}, owner=owner)
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID."""
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        self.event_bus.clear(session_id)
        deleted = self.store.delete_session(session_id)
        if deleted:
            # Keep the cross-session index in step with the store: a stale row
            # leaves the deleted session findable by search forever.
            self._search_index.remove_session(session_id)
        return deleted

    def set_session_title(self, session_id: str, title: str) -> Optional[Session]:
        """Rename a session and refresh its search-index row.

        Args:
            session_id: Session to rename.
            title: New title.

        Returns:
            The updated session, or ``None`` when it does not exist.
        """
        session = self.store.get_session(session_id)
        if session is None:
            return None
        session.title = title
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.update_session(session)
        # The index carries the title for search result headings, so without
        # this a rename is invisible to cross-session search.
        self._search_index.index_session(session.session_id, title)
        return session

    async def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a message to a session and trigger execution.

        Args:
            session_id: Session ID.
            content: Message content.
            role: Message role.
            include_shell_tools: Whether this attempt may use shell tools.
            reasoning_effort: Optional per-session override for the reasoning
                effort (``none``/``low``/``medium``/``high``/``max``, or ``""``
                for the provider default). ``None`` leaves the stored value
                untouched so a plain follow-up keeps the last choice.

        Returns:
            Dictionary containing message_id and attempt_id.

        Raises:
            ValueError: If the session does not exist.
            SessionBusyError: If the session already has a run in progress.
                Callers surface this as HTTP 409; the user can wait for the
                running attempt or cancel it first.
        """
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Claim the session before persisting anything. Reserving after the
        # user message is appended (or relying on _active_loops, which is only
        # populated once the registry is built) lets two concurrent sends both
        # store a message and create an attempt.
        if role == "user":
            self._reserve_session(session_id)
        handed_off = False

        try:
            message = Message(session_id=session_id, role=role, content=content)
            self.store.append_message(message)
            self._search_index.index_message(session_id, role, content)
            self.event_bus.emit(session_id, "message.received", {"message_id": message.message_id, "role": role, "content": content})

            if role != "user":
                return {"message_id": message.message_id}

            attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
            self.store.create_attempt(attempt)
            session.config["include_shell_tools"] = include_shell_tools
            if reasoning_effort is not None:
                session.config["reasoning_effort"] = reasoning_effort
            session.last_attempt_id = attempt.attempt_id
            session.updated_at = datetime.now().isoformat()
            self.store.update_session(session)
            self.event_bus.emit(session_id, "attempt.created", {"attempt_id": attempt.attempt_id, "prompt": content})

            task = asyncio.create_task(
                self._run_attempt(session, attempt, include_shell_tools=include_shell_tools)
            )
            self._active_tasks[session_id] = task
            # _run_attempt now owns the claim and releases it in its finally.
            handed_off = True
            return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}
        finally:
            if role == "user" and not handed_off:
                self._release_session(session_id)

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        """Return the message history."""
        return self.store.get_messages(session_id, limit)

    def cancel_current(self, session_id: str) -> bool:
        """Cancel the currently running AgentLoop for a session.

        Args:
            session_id: Session ID.

        Returns:
            Whether cancellation succeeded. True means an active loop existed and received a cancel signal.
        """
        loop = self._active_loops.get(session_id)
        if loop is not None:
            loop.cancel()
            return True
        # No loop yet: the run is still building its registry. Cancel the task
        # itself so the claim is released instead of stranding the session.
        task = self._active_tasks.get(session_id)
        if task is not None and not task.done():
            self._user_cancel_requests.add(session_id)
            task.cancel()
            return True
        return False

    async def compact_session(self, session_id: str) -> Dict[str, Any]:
        """Compress stored history into a handoff summary (non-destructive).

        The original messages stay in the append-only JSONL log; a compaction
        marker is appended after the folded head and retained tail, and
        :meth:`_convert_messages_to_history` uses its ``through`` boundary to
        replay the summary plus every retained message. Deleting the marker
        (or the whole file's last line) restores the uncompressed view, so this
        never destroys data.

        Args:
            session_id: Session whose stored history should be compressed.

        Returns:
            ``{"status": "compacted"|"noop", ...}`` with token estimates.

        Raises:
            ValueError: If the session does not exist.
            SessionBusyError: If a run is already in flight for the session.
        """
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        # Summarisation awaits a provider call. Hold the same claim as a turn
        # so another send/compaction cannot change the boundary mid-summary.
        self._reserve_session(session_id)
        try:
            return await self._compact_session(session)
        finally:
            self._release_session(session_id)

    async def _compact_session(self, session: Session) -> Dict[str, Any]:
        session_id = session.session_id
        marker, messages = self._partition_compacted_messages(
            self.store.get_messages(session_id, limit=None)
        )
        # Keep the most recent turns verbatim so the model still sees the tail
        # it is actively working on; only the head is folded into the summary.
        keep_tail = 4
        if len(messages) <= keep_tail + 1:
            return {
                "status": "noop",
                "reason": "history too short to compact",
            }

        head = messages[:-keep_tail]

        # Reuse the AgentLoop summarisation prompts so there is exactly one
        # handoff-summary format in the codebase.
        from src.agent.loop import (
            _ITERATIVE_UPDATE_PROMPT,
            _STRUCTURED_SUMMARY_PROMPT,
            _summary_chunks,
        )
        from src.providers.chat import ChatLLM
        from src.agent.llm_usage import _normalize_llm_usage
        from src.goal import GoalStore

        effort_override = None
        if session.config:
            raw_effort = session.config.get("reasoning_effort")
            if isinstance(raw_effort, str):
                effort_override = raw_effort

        # If an earlier compaction already folded a prefix, summarise that
        # summary plus the new turns instead of re-reading the raw prefix.
        prior_summary = self._compaction_content(marker) if marker else ""
        serialize = [entry for msg in head if (entry := self._history_message(msg))]

        if not serialize and not prior_summary:
            return {"status": "noop", "reason": "nothing to compact"}

        goal_store = GoalStore()
        goal_snapshot = goal_store.get_current_snapshot(session_id)
        goal_id = goal_snapshot["goal"]["goal_id"] if goal_snapshot else None
        usage_summary = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                         "calls": 0, "unreported_calls": 0}

        def budget_exhausted() -> bool:
            snapshot = goal_store.get_goal_snapshot(goal_id) if goal_id else None
            goal = (snapshot or {}).get("goal") or {}
            limit = goal.get("token_budget")
            return limit is not None and int(goal.get("tokens_used") or 0) >= limit

        def account_summary_usage(usage: Any) -> None:
            normalized = _normalize_llm_usage(usage)
            if normalized is None:
                usage_summary["unreported_calls"] += 1
                return
            usage_summary["calls"] += 1
            for key, value in normalized.items():
                usage_summary[key] = usage_summary.get(key, 0) + value
            if goal_id:
                # Manual compression is an auxiliary call within this goal,
                # not another research turn. Use only provider-reported tokens.
                goal_store.account_usage(
                    session_id=session_id, goal_id=goal_id, expected_goal_id=goal_id,
                    token_delta=normalized["total_tokens"], turn_delta=0,
                )
                snapshot = goal_store.get_goal_snapshot(goal_id)
                if snapshot:
                    self.event_bus.emit(session_id, "goal.updated", {"goal": snapshot["goal"], "snapshot": snapshot})

        if budget_exhausted():
            return {"status": "noop", "reason": "goal_token_budget_exhausted", "usage_metadata": usage_summary}
        llm = ChatLLM(reasoning_effort=effort_override)

        tokens_before = sum(len(m["content"]) for m in serialize) // 4
        summary = prior_summary
        loop = asyncio.get_running_loop()
        for chunk in _summary_chunks(serialize):
            if budget_exhausted():
                # The completed chunks do not cover the entire head. Leave the
                # append-only log/boundary intact so no evidence is folded away.
                return {"status": "noop", "reason": "goal_token_budget_exhausted", "usage_metadata": usage_summary}
            if summary:
                prompt = _ITERATIVE_UPDATE_PROMPT.format(
                    previous_summary=summary,
                    new_turns=chunk,
                    focus_section="",
                )
            else:
                prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section="") + chunk

            def _run_summary(text: str = prompt) -> Any:
                return llm.chat([{"role": "user", "content": text}])

            try:
                response = await loop.run_in_executor(_AGENT_EXECUTOR, _run_summary)
            except Exception as exc:
                account_summary_usage(getattr(exc, "usage_metadata", None))
                raise
            account_summary_usage(getattr(response, "usage_metadata", None))
            content = getattr(response, "content", "")
            if not isinstance(content, str) or not content.strip():
                # Do not advance ``through`` past a chunk the provider failed
                # to summarise. The original log remains usable for a retry.
                return {"status": "noop", "reason": "summarizer returned no content", "usage_metadata": usage_summary}
            summary = content

        if not summary:
            return {"status": "noop", "reason": "summarizer returned no content"}

        recovery_evidence = self._collect_compacted_evidence(marker, head)
        marker = Message(
            session_id=session_id,
            role="system",
            content=summary,
            metadata={
                "type": "compaction",
                "through": _message_field(head[-1], "message_id"),
                "tokens_before": tokens_before,
                "compacted_messages": len(serialize),
                "recovery_evidence": recovery_evidence,
                "usage_metadata": usage_summary,
            },
        )
        self.store.append_message(marker)
        self._search_index.index_message(session_id, "system", summary)
        self.event_bus.emit(
            session_id,
            "compact",
            {
                "message_id": marker.message_id,
                "compacted_messages": len(serialize),
                "tokens_before": tokens_before,
                "usage_metadata": usage_summary,
            },
        )
        return {
            "status": "compacted",
            "message_id": marker.message_id,
            "tokens_before": tokens_before,
            "compacted_messages": len(serialize),
            "usage_metadata": usage_summary,
        }

    async def _run_attempt(self, session: Session, attempt: Attempt, *, include_shell_tools: bool = False) -> None:
        """Execute an Attempt in the background.

        The whole body runs under try/finally: this coroutine owns the
        in-flight claim taken in :meth:`send_message`, and a failure anywhere —
        including in the pre-run bookkeeping below — must not leave the session
        permanently busy.
        """
        started_at = time.perf_counter()
        try:
            attempt.mark_running()
            # Wall-clock start (epoch seconds) is the one timing fact clients
            # cannot reconstruct on their own: a client that (re)connects
            # mid-attempt resumes its elapsed timer from it, and history
            # hydration derives the finished attempt's duration from it rather
            # than from the first tool call. It lives on the persisted attempt
            # so it survives the event ring buffer that first announced it.
            wall_started_at = attempt.started_at or time.time()
            self.store.update_attempt(attempt)
            self.event_bus.emit(
                session.session_id,
                "attempt.started",
                {"attempt_id": attempt.attempt_id, "started_at": wall_started_at},
            )
            # A compaction marker may be much older than the UI's 100-message
            # page. Load it before applying the bounded model-history window.
            messages = self.store.get_messages(session.session_id, limit=None)
            result = await self._run_with_agent(
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=dict(session.config),
            )
            status = result.get("status")
            if status == "success":
                attempt.mark_completed(summary=result.get("content", ""))
            elif status == "cancelled":
                # A cooperative cancel is not an outage; AttemptStatus.CANCELLED
                # existed but was dead because every non-success landed in the
                # failure branch.
                attempt.mark_cancelled(reason=result.get("reason", "cancelled by user"))
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")
            if result.get("metrics"):
                # Metrics were loaded from the run directory but never reached
                # the attempt, so the reply metadata below was always empty.
                attempt.metrics = result["metrics"]

            reply_metadata = {}
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
            result_tool_trail = result.get("tool_trail", [])
            if not isinstance(result_tool_trail, list):
                result_tool_trail = []
            # Preserve the historical full trail for successful replies; only
            # failed/cancelled recovery handoffs need the bounded
            # representation. The complete result is still available in the
            # run trace and its sidecars.
            persisted_tool_trail = (
                result_tool_trail
                if attempt.status == AttemptStatus.COMPLETED
                else self._bound_recovery_tool_trail(result_tool_trail)
            )
            if attempt.status != AttemptStatus.COMPLETED:
                recovery_context = self._build_recovery_context(
                    attempt.run_dir,
                    persisted_tool_trail,
                    total_tool_count=len(result_tool_trail),
                )
                if recovery_context:
                    reply_metadata["recovery_context"] = recovery_context
            reply_metadata["status"] = attempt.status.value
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics
            reply_metadata["elapsed_ms"] = max(0, round((time.perf_counter() - started_at) * 1000))
            reply_metadata["started_at"] = wall_started_at
            runtime_keys = (
                "provider",
                "configured_model",
                "model",
                "model_source",
                "reasoning_effort",
            )
            for key in runtime_keys:
                value = result.get(key)
                if value is not None:
                    reply_metadata[key] = value

            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
                # Keep the redacted tool trail for failed attempts too. A
                # follow-up recovery turn needs to know which evidence was
                # already collected; dropping it here forces the model to
                # either repeat expensive reads or claim that evidence is
                # unavailable after a transient final-response failure.
                tool_trail=persisted_tool_trail,
            )
            self.store.append_message(reply)
            # The append-only transcript is the user-visible source of truth.
            # Commit it before attempt.json so startup recovery can finish a
            # process interrupted between the two writes.
            self.store.update_attempt(attempt)
            self.store.delete_partial_response(session.session_id, attempt.attempt_id)
            self._search_index.index_message(session.session_id, "assistant", reply.content)
            self.event_bus.emit(
                session.session_id,
                _TERMINAL_EVENTS.get(attempt.status.value, "attempt.failed"),
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "summary": attempt.summary, "error": attempt.error, "run_dir": attempt.run_dir,
                 "started_at": wall_started_at, "ended_at": time.time(),
                 **{key: reply_metadata[key] for key in ("elapsed_ms", *runtime_keys) if key in reply_metadata}},
            )

        except asyncio.CancelledError:
            if session.session_id in self._user_cancel_requests:
                # cancel_current() cancels this task when the run has not
                # reached its AgentLoop yet. Keep that explicit user action
                # distinct from event-loop cancellation during server shutdown.
                attempt.mark_cancelled(reason="cancelled by user")
                self.store.update_attempt(attempt)
                self.store.delete_partial_response(
                    session.session_id, attempt.attempt_id
                )
                self.event_bus.emit(
                    session.session_id,
                    "attempt.cancelled",
                    {"attempt_id": attempt.attempt_id, "status": attempt.status.value},
                )
            else:
                logger.info(
                    "Leaving attempt %s recoverable after service task cancellation",
                    attempt.attempt_id,
                )
            raise
        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            self.store.delete_partial_response(session.session_id, attempt.attempt_id)
            self.event_bus.emit(session.session_id, "attempt.failed", {"attempt_id": attempt.attempt_id, "error": str(exc)})
        finally:
            # The only release path for the claim taken in send_message.
            self._active_tasks.pop(session.session_id, None)
            self._user_cancel_requests.discard(session.session_id)
            self._release_session(session.session_id)

    async def _run_with_agent(
        self,
        attempt: Attempt,
        messages: list = None,
        *,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an attempt with the V5 AgentLoop.

        Args:
            attempt: Current execution attempt.
            messages: Session message history.
            include_shell_tools: Whether the registry may include shell tools.
            session_config: Optional session-level config overrides. MCP server
                definitions under the ``mcpServers`` key are merged on top of
                the user config file via ``load_runtime_agent_config`` so each
                session can extend or override the global MCP server list.

        Returns:
            Result dictionary containing status, run_dir, run_id, metrics, and related fields.
        """
        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop, _default_max_iterations
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides

        # A per-session effort override rides in session.config (set by
        # send_message). Absent/None keeps the env default, so the field only
        # takes effect once a caller explicitly picks an effort.
        effort_override = None
        if session_config:
            raw_effort = session_config.get("reasoning_effort")
            if isinstance(raw_effort, str):
                effort_override = raw_effort
        # Only pass the kwarg when a choice was actually made: a no-argument
        # ChatLLM() is the historical call shape, and stubs/tests that patch
        # the class with a zero-arg factory must keep working.
        llm = (
            ChatLLM(reasoning_effort=effort_override)
            if effort_override is not None
            else ChatLLM()
        )
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()
        tool_trail: list[Dict[str, Any]] = []
        checkpoint = ResponseCheckpoint(self.store, attempt)

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            try:
                checkpoint.handle_event(event_type, data)
            except OSError as exc:
                # A checkpoint failure must not hide the live response or stop
                # the agent. The terminal attempt write still gets a chance.
                logger.warning(
                    "Could not checkpoint response for attempt %s: %s",
                    attempt_id,
                    exc,
                )
            if event_type in {"tool_call", "tool_result"}:
                self._record_tool_trail_event(tool_trail, event_type, data)
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        registry = await loop.run_in_executor(
            _AGENT_EXECUTOR,
            lambda: build_registry(
                persistent_memory=pm,
                include_shell_tools=include_shell_tools,
                agent_config=agent_config,
                session_id=session_id,
                event_callback=event_callback,
                warn_callback=_mcp_collision_warn,
            ),
        )

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=_default_max_iterations(),
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        # Build the message history context.
        history = self._convert_messages_to_history(messages) if messages else None
        from src.agent.recovery_evidence import load_prior_observations

        try:
            try:
                prior_observations = await loop.run_in_executor(
                    _AGENT_EXECUTOR,
                    lambda: load_prior_observations(self.store, session_id, messages or []),
                )
            except (OSError, ValueError, TypeError):
                logger.warning("Historical tool observations unavailable for session %s", session_id)
                prior_observations = ()
            recovery_kwargs = {"prior_observations": prior_observations} if prior_observations else {}
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                    **recovery_kwargs,
                ),
            )
        finally:
            self._active_loops.pop(session_id, None)
            try:
                checkpoint.flush()
            except OSError as exc:
                logger.warning(
                    "Could not flush response checkpoint for attempt %s: %s",
                    attempt_id,
                    exc,
                )

        result["tool_trail"] = tool_trail

        # Load metrics from the run output when available.
        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics

        return result

    @staticmethod
    def _bound_recovery_tool_trail(
        trail: list[Any],
    ) -> list[Dict[str, Any]]:
        """Copy a tool trail into the session log under a hard size bound.

        Tool events already carry redacted previews, but arguments can still be
        large (for example a generated strategy expression). Keep enough
        structure for recovery while leaving the complete payload in the run
        trace. Small entries are returned unchanged so the existing history API
        contract remains stable.
        """
        entries = [entry for entry in trail if isinstance(entry, dict)]
        if len(entries) > _RECOVERY_TRAIL_MAX_ENTRIES:
            # Keep the beginning for the initial data identity and the newest
            # entries for the point at which the run failed.
            head = min(8, _RECOVERY_TRAIL_MAX_ENTRIES // 2)
            entries = entries[:head] + entries[-(_RECOVERY_TRAIL_MAX_ENTRIES - head) :]

        bounded: list[Dict[str, Any]] = []
        for entry in entries:
            item = dict(entry)
            arguments = item.get("arguments")
            if isinstance(arguments, dict):
                try:
                    encoded = json.dumps(arguments, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    encoded = str(arguments)
                if len(encoded) > _RECOVERY_ARG_MAX_CHARS:
                    item["arguments"] = {
                        "_truncated": encoded[:_RECOVERY_ARG_MAX_CHARS]
                    }
                else:
                    # Round-trip through JSON so unusual argument values
                    # (Path, datetime, etc.) cannot make message persistence
                    # fail after the run itself has already completed.
                    try:
                        item["arguments"] = json.loads(encoded)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["arguments"] = {"_text": encoded}
            if "preview" in item:
                item["preview"] = str(item.get("preview") or "")[
                    :_RECOVERY_PREVIEW_MAX_CHARS
                ]
            bounded.append(item)
        return bounded

    @staticmethod
    def _build_recovery_context(
        run_dir: Optional[str],
        trail: list[Dict[str, Any]],
        *,
        total_tool_count: int,
    ) -> Dict[str, Any]:
        """Build a small, path-safe index for a failed run's next turn."""
        context: Dict[str, Any] = {
            "tool_count": max(total_tool_count, len(trail)),
            "tools": [
                {
                    "name": str(entry.get("tool") or "unknown"),
                    "status": str(entry.get("status") or "unknown"),
                }
                for entry in trail
            ],
        }
        if not run_dir:
            return context if trail else {}
        run_path = Path(run_dir)
        context["run_id"] = run_path.name
        # Expose only relative artifact names. Absolute host paths belong in
        # the trusted run trace, not in the conversation/API metadata.
        artifacts: list[str] = []
        if run_path.is_dir():
            for path in sorted(run_path.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(run_path).as_posix()
                except ValueError:
                    continue
                if relative == "run_card.json" or relative.startswith("artifacts/"):
                    artifacts.append(relative)
                if len(artifacts) >= 32:
                    break
        if artifacts:
            context["artifacts"] = artifacts
        return context

    @staticmethod
    def _record_tool_trail_event(
        tool_trail: list[Dict[str, Any]],
        event_type: str,
        data: Dict[str, Any],
    ) -> None:
        """Consolidate live tool events into a compact history record.

        Args:
            tool_trail: Mutable per-attempt trail.
            event_type: Agent event type (`tool_call` or `tool_result`).
            data: Already-redacted live event payload.
        """
        tool = str(data.get("tool") or "")
        if not tool:
            return

        call_id_value = data.get("call_id")
        call_id = call_id_value if isinstance(call_id_value, str) and call_id_value else None

        if event_type == "tool_call":
            entry: Dict[str, Any] = {
                "tool": tool,
                "status": "running",
                "arguments": (
                    dict(data["arguments"])
                    if isinstance(data.get("arguments"), dict)
                    else {}
                ),
                "timestamp": int(time.time() * 1000),
            }
            if call_id:
                entry["call_id"] = call_id
            tool_trail.append(entry)
            return

        match = None
        if call_id:
            match = next(
                (
                    entry
                    for entry in tool_trail
                    if entry.get("call_id") == call_id
                    and entry.get("status") == "running"
                ),
                None,
            )
        if match is None:
            match = next(
                (
                    entry
                    for entry in tool_trail
                    if entry.get("tool") == tool
                    and entry.get("status") == "running"
                ),
                None,
            )
        if match is None:
            match = {
                "tool": tool,
                "arguments": {},
                "timestamp": int(time.time() * 1000),
            }
            tool_trail.append(match)

        match["status"] = "ok" if data.get("status") == "ok" else "error"
        elapsed_ms = data.get("elapsed_ms")
        if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool):
            match["elapsed_ms"] = max(0, int(elapsed_ms))
        match["preview"] = str(data.get("preview") or "")
        if call_id:
            match["call_id"] = call_id

    @staticmethod
    def _partition_compacted_messages(messages: list) -> tuple[Any | None, list]:
        """Return the latest handoff plus its still-unfolded messages.

        Markers are appended *after* the retained tail. The ``through`` ID,
        rather than the marker position, therefore defines the folded prefix.
        Legacy markers without a resolvable boundary keep positional behavior.
        """
        compact_at = next(
            (index for index in range(len(messages) - 1, -1, -1)
             if _message_metadata(messages[index]).get("type") == "compaction"),
            -1,
        )
        if compact_at < 0:
            return None, messages
        marker = messages[compact_at]
        through_id = _message_metadata(marker).get("through")
        start = compact_at + 1
        if through_id:
            for index, message in enumerate(messages):
                if _message_field(message, "message_id") == through_id:
                    start = index + 1
                    break
        return marker, [
            message for message in messages[start:]
            if _message_metadata(message).get("type") != "compaction"
        ]

    @staticmethod
    def _recovery_text(message: Any) -> str:
        """Render the same bounded evidence for replay and summarisation."""
        trail = _message_field(message, "tool_trail", [])
        trail = [entry for entry in trail if isinstance(entry, dict)] if isinstance(trail, list) else []
        metadata = _message_metadata(message)
        context = metadata.get("recovery_context")
        context = context if isinstance(context, dict) else {}
        if _message_field(message, "role") != "assistant" or not (trail or context):
            return ""
        if metadata.get("status") == AttemptStatus.COMPLETED.value and not context:
            return ""
        lines = ["[Previous attempt evidence]"]
        run_id = str(context.get("run_id") or metadata.get("run_id") or "")
        if run_id:
            lines.append(f"run_id={run_id}")
        tool_count = context.get("tool_count")
        if isinstance(tool_count, int) and tool_count > len(trail):
            lines.append(f"tool_count={tool_count} (bounded preview)")
        artifacts = context.get("artifacts")
        if isinstance(artifacts, list) and artifacts:
            lines.append("artifacts=" + ", ".join(str(item) for item in artifacts[:32]))
        if len(trail) > _RECOVERY_HISTORY_MAX_ENTRIES:
            entries = trail[:4] + trail[-(_RECOVERY_HISTORY_MAX_ENTRIES - 4):]
            lines.append(f"tool_entries_omitted={len(trail) - len(entries)}")
        else:
            entries = trail
        for entry in entries:
            tool = str(entry.get("tool") or "unknown")
            status = str(entry.get("status") or "unknown")
            preview = str(entry.get("preview") or "").replace("\n", " ")[:500]
            line = f"- {tool} [{status}]"
            arguments = entry.get("arguments")
            if isinstance(arguments, dict) and arguments:
                try:
                    encoded_args = json.dumps(arguments, ensure_ascii=False, default=str)[:400]
                except (TypeError, ValueError):
                    encoded_args = str(arguments)[:400]
                line += f" args={encoded_args}"
            if preview:
                line += f": {preview}"
            lines.append(line)
        return "\n".join(lines)[:_RECOVERY_HISTORY_MAX_CHARS]

    @classmethod
    def _history_message(cls, message: Any) -> Dict[str, Any] | None:
        """Normalise one transcript message without trimming its body."""
        import re

        role = _message_field(message, "role", "user")
        if role not in ("user", "assistant"):
            return None
        content = _message_field(message, "content", "") or ""

        def shorten_run_dir(match: re.Match) -> str:
            run_id = Path(match.group(0).replace("Run directory:", "").strip()).name
            return f"[prev_run: {run_id}]" if run_id else ""

        content = re.sub(r"Run directory:\s*\S+", shorten_run_dir, content).strip()
        evidence = cls._recovery_text(message)
        if evidence:
            content = f"{content}\n\n{evidence}".strip()
        return {"role": role, "content": content} if content else None

    @classmethod
    def _collect_compacted_evidence(cls, marker: Any, messages: list) -> list[dict]:
        """Keep a bounded, deterministic recovery index alongside LLM prose.

        The summariser can omit precise run/artifact identities. Preserve the
        recent evidence separately so repeated compaction does not erase them.
        No file is reopened here: inputs are the already-redacted transcript.
        """
        previous = _message_metadata(marker).get("recovery_evidence", [])
        evidence = [dict(entry) for entry in previous if isinstance(entry, dict)] if isinstance(previous, list) else []
        for message in messages:
            text = cls._recovery_text(message)
            if not text:
                continue
            metadata = _message_metadata(message)
            context = metadata.get("recovery_context")
            context = context if isinstance(context, dict) else {}
            artifacts = context.get("artifacts", [])
            evidence.append({
                "message_id": str(_message_field(message, "message_id", ""))[:128],
                "run_id": str(context.get("run_id") or metadata.get("run_id") or "")[:128],
                "artifacts": [str(item)[:256] for item in artifacts[:16]] if isinstance(artifacts, list) else [],
                "text": text,
            })
        evidence = evidence[-_COMPACT_RECOVERY_MAX_ENTRIES:]
        # Keep complete recent records preferentially. A pathological legacy
        # record may contain escaped control characters, so enforce the budget
        # on the serialized representation, not only the displayed text.
        while len(json.dumps(evidence, ensure_ascii=False)) > _COMPACT_RECOVERY_MAX_CHARS:
            if len(evidence) > 1:
                evidence.pop(0)
            elif evidence and len(str(evidence[0].get("text", ""))) > 100:
                evidence[0]["text"] = str(evidence[0]["text"])[:len(str(evidence[0]["text"])) // 2]
            else:
                evidence = []
        return evidence

    @staticmethod
    def _compaction_content(marker: Any) -> str:
        summary = str(_message_field(marker, "content", "") or "").strip()
        evidence = _message_metadata(marker).get("recovery_evidence", [])
        if isinstance(evidence, list):
            # Recent failed runs take precedence within the handoff budget.
            excerpts = [str(entry.get("text", "")) for entry in reversed(evidence) if isinstance(entry, dict)]
            appendix = "\n\n".join(excerpts)[:_RECOVERY_HISTORY_MAX_CHARS]
            if appendix:
                summary += "\n\n" + appendix
        return summary.strip()

    @classmethod
    def _convert_messages_to_history(cls, messages: list) -> list[Dict[str, Any]]:
        """Replay the newest handoff and a bounded tail, excluding the prompt."""
        # The current prompt is passed separately to AgentLoop. Locate the
        # marker before trimming so long conversations cannot silently lose it.
        marker, leading = cls._partition_compacted_messages(messages[:-1])
        history = [entry for message in leading if (entry := cls._history_message(message))]
        max_history_chars = 12000
        total_chars = 0
        trimmed: list = []
        for message in reversed(history):
            content = message["content"]
            remaining = max_history_chars - total_chars
            if len(content) <= remaining:
                trimmed.append(message)
                total_chars += len(content)
                continue
            if not trimmed:
                trimmed.append({**message, "content": content[:remaining] + "\n[... truncated]"})
            break
        replayed = list(reversed(trimmed))
        summary = cls._compaction_content(marker) if marker else ""
        if summary:
            # Pin the handoff outside the newest-first tail budget. A user
            # block also works with providers requiring one leading system.
            replayed.insert(0, {
                "role": "user",
                "content": f"[Conversation compressed — handoff summary]\n\n{summary}",
            })
        return replayed

    @staticmethod
    def _load_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Load metrics.csv from a run directory."""
        import csv
        metrics_path = run_dir / "artifacts" / "metrics.csv"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return {k: float(v) for k, v in rows[0].items() if v}
        except Exception:
            pass
        return None

    @staticmethod
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message.

        Args:
            attempt: The terminal attempt.

        Returns:
            The reply text shown in the transcript.
        """
        if attempt.status == AttemptStatus.COMPLETED:
            if attempt.summary:
                return attempt.summary
            # Do not dress an empty answer up as a finished strategy run: say
            # that nothing came back so the user knows to retry or rephrase.
            return (
                "The run finished without producing any text output. "
                "Check the run artifacts, or rephrase the request and try again."
            )
        if attempt.status == AttemptStatus.CANCELLED:
            return "Run cancelled."
        return f"Execution failed: {attempt.error or 'unknown error'}"
