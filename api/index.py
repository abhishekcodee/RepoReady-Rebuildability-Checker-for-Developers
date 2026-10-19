"""Vercel HTTP adapter for the RepoReady static scanner.

The primary RepoReady interface remains the local CLI. This adapter exposes a
read-only report for the repository bundled into the Vercel deployment so the
Python runtime has a real HTTP entrypoint. It never accepts a filesystem path
from a request and never executes scanned project code.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from repoready.checks import run_checks  # noqa: E402
from repoready.report import build_report, serialize_report  # noqa: E402
from repoready.scanner import InvalidTargetError, scan_project  # noqa: E402
from repoready.scoring import calculate_score  # noqa: E402


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    """Write a UTF-8 JSON response without exposing exception details."""
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class handler(BaseHTTPRequestHandler):
    """Vercel's supported ``BaseHTTPRequestHandler`` entrypoint."""

    def do_GET(self) -> None:  # noqa: N802 - required HTTP handler method
        """Return a report for the repository deployed with this function."""
        try:
            snapshot = scan_project(PROJECT_ROOT)
            results = run_checks(snapshot)
            score = calculate_score(results)
            report = serialize_report(build_report(snapshot, results, score))
        except (InvalidTargetError, OSError, ValueError):
            _json_response(self, 500, '{"error":"RepoReady could not scan the deployment"}')
            return

        _json_response(self, 200, report)

    def do_POST(self) -> None:  # noqa: N802 - required HTTP handler method
        """Reject writes because the deployed adapter is read-only."""
        _json_response(self, 405, '{"error":"method not allowed"}')

    def log_message(self, format: str, *args: object) -> None:
        """Avoid logging request details that could contain sensitive data."""
        return
