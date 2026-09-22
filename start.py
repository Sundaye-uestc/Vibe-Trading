"""Quick-start both backend and frontend dev servers for Vibe-Trading.

Usage:
    python start.py              # both servers
    python start.py --backend    # backend only (port 8899)
    python start.py --frontend   # frontend only (port 5899)
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_CMD = [sys.executable, "-m", "cli", "serve", "--port", "8899"]
FRONTEND_CMD = ["npm.cmd", "run", "dev"]
FRONTEND_DIR = ROOT / "frontend"


def stream_output(pipe, prefix: str) -> None:
    """Forward one subprocess stream to our stdout with a ``[prefix]`` tag.

    One thread per stream, never one thread for both: a process whose stderr is
    not drained blocks forever on the first write past the OS pipe buffer, and a
    server that never exits never closes stdout. Reading stdout to EOF before
    touching stderr is therefore a deadlock waiting for enough stderr output --
    and ``agent/src/core/runner.py`` logs its backtest progress to stderr.
    """
    if pipe is None:
        return
    try:
        for line in pipe:
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            print(f"[{prefix}] {text}", flush=True)
    except (ValueError, OSError):
        pass


# The backend prints UTF-8 (skill docs, Chinese market data, agent reports).
# Without this its Windows console code page is the source of truth and every
# non-ASCII log line is mangled, so pin the encoding on the child instead.
CHILD_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Vibe-Trading dev servers")
    parser.add_argument("--backend", action="store_true", help="Backend only")
    parser.add_argument("--frontend", action="store_true", help="Frontend only")
    args = parser.parse_args()

    both = not args.backend and not args.frontend
    run_backend = both or args.backend
    run_frontend = both or args.frontend

    # Validate frontend build exists
    frontend_build = FRONTEND_DIR / "dist" / "index.html"
    if run_frontend and not frontend_build.exists():
        print("[!] Frontend build not found. Run first: cd frontend && npm run build")
        print("    (or run with --backend only)")

    procs: list[subprocess.Popen] = []
    # (pipe, tag) pairs, one entry per stream so every one gets its own reader.
    streams: list[tuple[object, str]] = []

    def shutdown(sig, frame):
        print("\n[!] Shutting down...", flush=True)
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if run_backend:
        print(f"[backend] Starting on http://127.0.0.1:8899 ...")
        backend_proc = subprocess.Popen(
            BACKEND_CMD,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **CHILD_ENV},
        )
        procs.append(backend_proc)
        streams += [
            (backend_proc.stdout, "backend"),
            (backend_proc.stderr, "backend:err"),
        ]

    if run_frontend:
        print(f"[frontend] Starting on http://localhost:5899 ...")
        frontend_proc = subprocess.Popen(
            FRONTEND_CMD,
            cwd=str(FRONTEND_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            env={**os.environ, "BROWSER": "none"},
        )
        procs.append(frontend_proc)
        streams += [
            (frontend_proc.stdout, "frontend"),
            (frontend_proc.stderr, "frontend:err"),
        ]

    print("=" * 50)
    if run_backend:
        print("  Backend  → http://127.0.0.1:8899")
    if run_frontend:
        print("  Frontend → http://localhost:5899")
    print("  Press Ctrl+C to stop")
    print("=" * 50)

    from threading import Thread

    threads = [
        Thread(target=stream_output, args=(pipe, tag), daemon=True)
        for pipe, tag in streams
    ]
    for t in threads:
        t.start()

    try:
        while any(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        print("[!] Stopped.", flush=True)


if __name__ == "__main__":
    main()
