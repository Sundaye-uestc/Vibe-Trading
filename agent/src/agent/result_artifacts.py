"""Preserve large, redacted observations in the current task workspace.

The model receives a small JSON receipt instead of an unrecoverable prefix.
The complete result and ordered parts are immutable: changing an existing
artifact causes explicit truncation fallback, never an overwrite or a false
claim that the modified file contains the original observation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import RLock

from src.config.limits import TOOL_RESULT_LIMIT, truncate_tool_result
from src.tools.redaction import redact_tool_result

logger = logging.getLogger(__name__)
_WRITE_LOCK = RLock()


def _contained_path(path: Path, root: Path) -> None:
    """Refuse changed paths and symlink/junction aliases below the run root."""
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("Result artifact escapes the task workspace")
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("Result artifact path contains a link")


def _write_original(path: Path, content: str, root: Path) -> None:
    """Publish complete bytes without replacing any existing artifact.

    A temporary sibling plus an exclusive hard link prevents a concurrent
    reader from observing a newly created but partly written result. An
    existing file must contain the exact same bytes before it is reused.
    """
    _contained_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _contained_path(path, root)
    encoded = content.encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise ValueError("Existing result artifact was modified")
        return

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".result-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        _contained_path(path, root)
        _contained_path(temporary, root)
        try:
            os.link(temporary, path)
        except FileExistsError:
            _contained_path(path, root)
            if not path.is_file() or path.read_bytes() != encoded:
                raise ValueError("Existing result artifact was modified") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _encoded(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fits(payload: str) -> bool:
    # Keep receipts and parts below both the existing character cap and a
    # conservative UTF-8 wire cap; short original results retain their contract.
    return len(payload.encode("utf-8")) <= TOOL_RESULT_LIMIT


def _read_envelope(path: Path, content: str) -> str:
    return _encoded({"status": "ok", "path": str(path), "content": content})


def _local_readers(registry) -> tuple[bool, bool]:
    from src.tools.bash_tool import BashTool
    from src.tools.read_file_tool import ReadFileTool

    # A remote or overridden tool with the same name need not read this
    # filesystem. Only the built-in implementations establish this path.
    return type(registry.get("read_file")) is ReadFileTool, type(registry.get("bash")) is BashTool


def readable_result_workspace(run_dir: str | Path | None, registry) -> Path | None:
    """Resolve an existing allowed task directory with a known local reader.

    Context compaction can reuse this check before saving old observations.
    A registered shell never bypasses the run-directory allowlist.
    """
    if not run_dir:
        return None
    from src.tools.path_utils import safe_run_dir

    try:
        root = safe_run_dir(str(run_dir))
        # A credential-shaped workspace name cannot be advertised as a usable
        # locator without leaking it or producing a path that no longer exists.
        locator = _encoded({"path": str(root)})
        if json.loads(redact_tool_result(locator))["path"] != str(root):
            return None
        return root if root.is_dir() and any(_local_readers(registry)) else None
    except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError):
        return None


def save_result_parts(content: str, folder: Path, root: Path) -> list[str]:
    """Save ordered parts that survive the real file-reader delivery path.

    Each part fits inside ``read_file``'s JSON envelope, including escaped
    paths, quotes and newlines. Its content must also survive the redaction
    pass on that envelope unchanged, so another delivery cannot silently
    alter the already-scrubbed observation or create another result receipt.
    """
    from src.tools.path_utils import safe_run_dir

    root = safe_run_dir(str(root))
    if not root.is_dir():
        raise ValueError("Result workspace does not exist")
    _contained_path(folder, root)
    offset = 0
    parts: list[str] = []
    while offset < len(content):
        path = folder / f"{len(parts) + 1}.txt"
        low, high = 0, min(len(content) - offset, TOOL_RESULT_LIMIT)
        while low < high:
            count = (low + high + 1) // 2
            if _fits(_read_envelope(path, content[offset:offset + count])):
                low = count
            else:
                high = count - 1
        if not low:
            raise ValueError("Artifact path leaves no room for readable content")

        # Cutting a word can introduce a new credential-shaped prefix. Reduce
        # this part until its second pass is stable rather than skipping scrub.
        while low:
            part = content[offset:offset + low]
            reread = redact_tool_result(_read_envelope(path, part))
            if _fits(reread) and json.loads(reread).get("content") == part:
                break
            low //= 2
        if not low:
            raise ValueError("Result cannot be split without altering its content")
        _write_original(path, part, root)
        parts.append(str(path))
        offset += low
    return parts


def deliver_tool_result(result: str, run_dir: str | Path | None, registry) -> str:
    """Return a bounded result or a receipt for its complete redacted artifact.

    ``run_dir`` must be the caller's current task directory, not a model's
    arbitrary path argument. Only allowlisted, existing run directories and
    registered local file readers can receive artifacts. Saving failures or
    unavailable reading fall back to the established explicit truncation
    notice, without claiming a file was delivered.
    """
    result = redact_tool_result(result)
    if len(result) <= TOOL_RESULT_LIMIT or not run_dir:
        return truncate_tool_result(result)

    root = readable_result_workspace(run_dir, registry)
    if root is None:
        return truncate_tool_result(result)
    try:
        reader, shell = _local_readers(registry)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError):
        return truncate_tool_result(result)
    if not (reader or shell):
        return truncate_tool_result(result)

    try:
        payload = json.loads(result)
        is_json = True
    except (ValueError, TypeError):
        payload, is_json = None, False

    try:
        digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
        folder = root / "tool-results" / digest
        path = folder / ("result.json" if is_json else "result.txt")
        with _WRITE_LOCK:
            _write_original(path, result, root)
            parts = save_result_parts(result, folder / "parts", root)
    except (OSError, UnicodeError, ValueError):
        logger.warning("Could not preserve complete tool result", exc_info=True)
        return truncate_tool_result(result)

    read_hint = (
        "Use read_file on parts/1.txt through part_count in order, passing the current run_dir. "
        if reader else "Use bash to read the complete file or the ordered part files. "
    )
    receipt = {
        "_result_file": {
            "path": str(path), "sha256": digest,
            "format": "json" if is_json else "text", "characters": len(result),
            "size_bytes": len(result.encode("utf-8")),
            "parts_dir": str(folder / "parts"), "part_count": len(parts),
        },
        "_delivery": (
            "The complete redacted tool result is saved in this task workspace. "
            + read_hint
            + "Do not refetch data merely because this preview is shortened. "
            "Source row limits/sampling still apply: inspect the original coverage before calculating; "
            "saving does not fill missing rows."
        ),
    }
    if isinstance(payload, dict):
        for key in ("status", "ok", "_provenance", "_unresolved", "_diagnostics"):
            if key in payload:
                receipt[key] = payload[key]
    if len(_encoded(receipt).encode("utf-8")) > TOOL_RESULT_LIMIT // 2:
        for key in ("_diagnostics", "_provenance", "_unresolved"):
            receipt.pop(key, None)
        receipt["_metadata_location"] = "Source metadata is in the complete result file."

    room = max(0, TOOL_RESULT_LIMIT // 4 - len(_encoded(receipt).encode("utf-8")))
    while room:
        receipt["_preview"] = result[:room // 2] + "\n... preview ...\n" + result[-room // 2:]
        encoded = _encoded(receipt)
        if _fits(encoded):
            return encoded
        room //= 2
    receipt.pop("_preview", None)
    encoded = _encoded(receipt)
    return encoded if _fits(encoded) else truncate_tool_result(result)
