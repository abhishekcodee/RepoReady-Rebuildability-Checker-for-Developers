"""Offline tests for the optional Vercel HTTP adapter."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import unittest


SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


API_PATH = pathlib.Path(__file__).resolve().parents[1] / "api" / "index.py"
SPEC = importlib.util.spec_from_file_location("repoready_vercel_index", API_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeHandler(MODULE.handler):
    """Capture handler output without opening a socket or making a request."""

    def __init__(self) -> None:
        self.wfile = io.BytesIO()
        self.headers_sent: list[tuple[str, str]] = []
        self.status: int | None = None

    def send_response(self, status: int, message: str | None = None) -> None:
        self.status = status

    def send_header(self, keyword: str, value: str) -> None:
        self.headers_sent.append((keyword, value))

    def end_headers(self) -> None:
        return


class VercelAdapterTests(unittest.TestCase):
    def test_handler_is_a_supported_http_entrypoint(self) -> None:
        self.assertTrue(issubclass(MODULE.handler, MODULE.BaseHTTPRequestHandler))

    def test_get_returns_a_real_json_report_without_project_execution(self) -> None:
        response = FakeHandler()
        MODULE.handler.do_GET(response)

        self.assertEqual(response.status, 200)
        payload = json.loads(response.wfile.getvalue().decode("utf-8"))
        self.assertEqual(payload["tool"]["name"], "RepoReady")
        self.assertEqual(len(payload["checks"]), 8)

    def test_post_is_rejected(self) -> None:
        response = FakeHandler()
        MODULE.handler.do_POST(response)

        self.assertEqual(response.status, 405)
        self.assertEqual(json.loads(response.wfile.getvalue()), {"error": "method not allowed"})


if __name__ == "__main__":
    unittest.main()
