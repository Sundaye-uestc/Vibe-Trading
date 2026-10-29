"""Verify a wheel installation or embedded runtime, never the source checkout.

Run with ``python -I scripts/check_packaged_runtime.py SITE_PACKAGES`` after
installing the application wheel there. Dependencies may come from the active
environment, but every imported application module must come from this directory.
No provider calls or account writes are performed.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

RESOURCES = (
    "src/channels/fonts/VibeReportCJK-Regular.ttf",
    "src/channels/fonts/OFL.txt",
    "src/channels/fonts/README.md",
    "src/agent/tool_loading.json",
    "src/providers/context_windows.json",
    "src/tools/paper_trade/paper_trade_step-v1.1.json",
    "src/tools/paper_trade/README.md",
)
MODULES = (
    "src.agent.tool_loading",
    "src.agent.context_budget",
    "src.agent.result_artifacts",
    "src.agent.recovery_evidence",
    "src.agent.recovery_evidence",
    "src.agent.llm_usage",
    "src.swarm.context",
    "src.clickhouse_connector",
    "src.tools.paper_trade.schema",
    "src.tools.paper_trade.accounts",
    "src.tools.paper_trade.tool",
    "src.tools.paper_trade.preflight",
    "src.api.paper_trade_routes",
    "cli.commands.paper_trade",
    "backtest.loaders.valuation_loader",
    "src.factors.zoo.fundamental.pb",
    "src.factors.zoo.fundamental.ps_ttm",
    "src.factors.zoo.fundamental.dividend_yield",
)


def check_resources(root: Path) -> None:
    for name in RESOURCES:
        resource = root / name
        if not resource.is_file() or not resource.read_bytes():
            raise RuntimeError(f"Missing or empty packaged resource: {name}")
        if name.endswith(".json"):
            try:
                document = json.loads(resource.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError(f"Invalid packaged JSON resource: {name}") from exc
            if not isinstance(document, dict) or not document:
                raise RuntimeError(f"Invalid packaged JSON object: {name}")


def check_module_origin(module: ModuleType, root: Path) -> None:
    """Reject an editable install silently filling gaps in the built wheel."""
    origin = getattr(module, "__file__", None)
    if origin is not None:
        paths = [origin]
    else:
        paths = list(getattr(module, "__path__", ()))
    if not paths or any(not Path(path).resolve().is_relative_to(root) for path in paths):
        raise RuntimeError(f"Application module loaded outside packaged runtime: {module.__name__}")


def check_runtime(root: Path) -> None:
    root = root.resolve(strict=True)
    check_resources(root)
    sys.path.insert(0, str(root))
    modules = {name: importlib.import_module(name) for name in MODULES}
    # Check transitive imports too: an editable install must not hide missing
    # helper modules in an otherwise valid wheel.
    for name, module in tuple(sys.modules.items()):
        if module is not None and name.split(".")[0] in {"src", "backtest", "cli"}:
            check_module_origin(module, root)

    policy = modules["src.agent.tool_loading"].loading_policy()
    assert policy["initial_tools"] and policy["minimum_tools"] > 0
    window, _ = modules["src.agent.context_budget"].resolve_window("", "__packaging_probe__")
    assert window > 0
    schema = modules["src.tools.paper_trade.schema"]
    assert schema.load_contract_schema("result")
    assert schema.load_contract_schema("error_envelope")
    for name, factor_id in (
        ("pb", "fund_pb"),
        ("ps_ttm", "fund_ps_ttm"),
        ("dividend_yield", "fund_dividend_yield"),
    ):
        factor = modules[f"src.factors.zoo.fundamental.{name}"]
        assert factor.__alpha_meta__["id"] == factor_id
        assert callable(factor.compute)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -I check_packaged_runtime.py SITE_PACKAGES")
    check_runtime(Path(sys.argv[1]))
    print("Packaged runtime resources, imports and contracts OK")
