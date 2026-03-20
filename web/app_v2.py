"""Compatibility shim for the legacy `web.app_v2` entrypoint.

`web.app` is the canonical FastAPI implementation. This module re-exports the
same objects so existing deployments that still start `uvicorn web.app_v2:app`
continue to work without code duplication.
"""

from web.app import *  # noqa: F401,F403
