"""Render the real Web templates and execute the settings script in Node.

These helpers exist so the regression can drive the *actual* settings page
(`collectConfig()` over the controls the browser would render) instead of
re-declaring the payload shape in Python.  The Node probe is only used when
`node` is available; the VPS regression box has no Node, so the probe tests
skip there and the FastAPI route tests cover the same contracts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
PROBE = ROOT / "tests" / "js" / "dom_probe.mjs"


def template_environment() -> Environment:
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    environment.globals["url_for"] = (
        lambda endpoint, **kwargs: "/static" + str(kwargs.get("path", ""))
    )
    return environment


def render_page(name: str, context: dict) -> str:
    return template_environment().get_template(name).render(**context)


def node_available() -> bool:
    return shutil.which("node") is not None


def run_collect_probe(html: str, scenarios: list[dict], timeout: int = 120) -> dict:
    """Run collectConfig() in Node against the rendered page."""
    if not node_available():
        raise RuntimeError("node is not available")
    completed = subprocess.run(
        ["node", str(PROBE)],
        input=json.dumps({"html": html, "scenarios": scenarios}),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "dom_probe failed:\n" + (completed.stderr or "<no stderr>")
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"dom_probe produced invalid JSON: {completed.stdout[:2000]}") from exc
