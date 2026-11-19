"""AST guardrail enforcing AGENTS.md Rule 2: zero LLM/HTTP imports in forecast_core."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_TOP_LEVEL_MODULES = {
    "anthropic",
    "google",
    "genai",
    "httpx",
    "langchain",
    "langchain_core",
    "langgraph",
    "litellm",
    "ollama",
    "openai",
    "pydantic_ai",
    "requests",
    "urllib",
    "urllib3",
}


def test_forecast_core_has_zero_llm_or_http_imports() -> None:
    core_dir = Path(__file__).resolve().parents[2] / "src" / "vani_ledger" / "forecast_core"
    py_files = sorted(core_dir.rglob("*.py"))
    assert py_files, "forecast_core directory must contain Python modules"

    violations: list[str] = []
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN_TOP_LEVEL_MODULES:
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in FORBIDDEN_TOP_LEVEL_MODULES:
                    violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")

    assert not violations, f"Forbidden imports in forecast_core: {violations}"
