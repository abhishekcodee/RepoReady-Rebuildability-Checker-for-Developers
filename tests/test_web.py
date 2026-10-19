"""Checks for the static Vercel dashboard shell."""

from pathlib import Path
import unittest


class WebDashboardTests(unittest.TestCase):
    def test_dashboard_is_static_and_points_to_report_api(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")

        self.assertIn("RepoReady", html)
        self.assertIn("fetch('/api'", html)
        self.assertIn('class="ring"', html)
        self.assertIn("checks", html)


if __name__ == "__main__":
    unittest.main()
