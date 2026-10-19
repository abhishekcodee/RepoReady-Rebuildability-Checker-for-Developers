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


def _response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    """Write a UTF-8 response without exposing exception details."""
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    _response(handler, status, body, "application/json; charset=utf-8")


def _dashboard(report: str) -> str:
    """Render the safe report as a compact, dependency-free dashboard."""
    import json

    data = json.loads(report)
    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RepoReady — rebuildability report</title><style>
:root{{color-scheme:dark;--bg:#0b1018;--panel:#111a27;--line:#243247;--text:#e8eef7;--muted:#8fa0b8;--blue:#67a4ff;--green:#42d6a4;--amber:#f4bd62;--red:#ff7185}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 85% 0,#172b48 0,transparent 38%),var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:42px 28px 64px}}header{{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:34px}}.eyebrow{{color:var(--blue);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:11px}}h1{{font-size:34px;line-height:1.1;margin:8px 0}}p{{color:var(--muted);margin:8px 0}}.badge{{border:1px solid var(--line);border-radius:999px;padding:8px 13px;color:var(--green);white-space:nowrap}}.hero,.card{{background:rgba(17,26,39,.88);border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 35px #0003}}.hero{{display:flex;align-items:center;gap:30px;padding:26px;margin-bottom:18px}}.score{{width:130px;height:130px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--green) calc(var(--score)*1%),#25364e 0);position:relative;flex:0 0 auto}}.score:after{{content:"";position:absolute;inset:10px;background:var(--panel);border-radius:50%}}.score strong{{position:relative;z-index:1;font-size:30px}}.hero h2{{margin:0 0 5px;font-size:21px}}.stats{{display:flex;gap:24px;flex-wrap:wrap;margin-top:16px}}.stat b{{display:block;font-size:20px}}.stat span{{color:var(--muted);font-size:12px}}.grid{{display:grid;grid-template-columns:1.5fr 1fr;gap:18px}}.card{{padding:22px}}h3{{margin:0 0 18px;font-size:16px}}.check{{padding:14px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:14px}}.check:first-of-type{{border-top:0}}.check b{{display:block}}.check small{{color:var(--muted)}}.status{{font-size:11px;font-weight:800;border-radius:5px;padding:4px 8px;height:max-content}}.PASS{{color:var(--green);background:#12362f}}.FAIL{{color:var(--red);background:#3b1c28}}.WARNING{{color:var(--amber);background:#3b2e19}}.UNKNOWN,.NOT_APPLICABLE{{color:var(--muted);background:#243247}}.recommendation{{padding:12px 0;border-top:1px solid var(--line);color:var(--muted)}}.recommendation b{{display:block;color:var(--text);margin-bottom:3px}}code{{color:var(--blue)}}footer{{margin-top:20px;color:var(--muted);font-size:12px}}@media(max-width:700px){{main{{padding:26px 16px}}header,.hero{{align-items:flex-start;flex-direction:column}}.grid{{grid-template-columns:1fr}}}}
</style></head><body><main><header><div><div class="eyebrow">RepoReady / static analysis</div><h1>Rebuildability report</h1><p id="project"></p></div><div class="badge">Read-only scan</div></header><section class="hero"><div class="score" id="score"><strong id="scoreText"></strong></div><div><h2>How ready is this repository?</h2><p>Evidence-based checks for sharing, reproducing, and rebuilding a project.</p><div class="stats"><div class="stat"><b id="passed"></b><span>passed checks</span></div><div class="stat"><b id="coverage"></b><span>evidence coverage</span></div><div class="stat"><b id="recommendationCount"></b><span>recommendations</span></div></div></div></section><div class="grid"><section class="card"><h3>Check results</h3><div id="checks"></div></section><section class="card"><h3>Next actions</h3><div id="recommendations"></div></section></div><footer>Generated <span id="generated"></span> · API data remains available as JSON for automation.</footer></main><script>const report={embedded};const score=report.score.overall_score||0;document.getElementById('score').style.setProperty('--score',score);document.getElementById('scoreText').textContent=Math.round(score)+'/100';document.getElementById('project').textContent=report.project.path;document.getElementById('coverage').textContent=Math.round(report.score.coverage||0)+'%';document.getElementById('recommendationCount').textContent=report.recommendations.length;document.getElementById('passed').textContent=report.score.status_counts.PASS||0;document.getElementById('generated').textContent=new Date(report.generated_at).toLocaleString();document.getElementById('checks').innerHTML=report.checks.map(c=>`<div class="check"><div><b>${c.name.replaceAll('_',' ')}</b><small>${c.explanation}</small></div><span class="status ${c.status}">${c.status}</span></div>`).join('');document.getElementById('recommendations').innerHTML=report.recommendations.length?report.recommendations.map(r=>`<div class="recommendation"><b>${r.priority} · ${r.check_name.replaceAll('_',' ')}</b>${r.message}</div>`).join(''):'<p>No recommendations. This repository is looking solid.</p>';</script></body></html>'''


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

        accept = getattr(self, "headers", {}).get("Accept", "")
        if "text/html" in accept:
            _response(self, 200, _dashboard(report), "text/html; charset=utf-8")
        else:
            _json_response(self, 200, report)

    def do_POST(self) -> None:  # noqa: N802 - required HTTP handler method
        """Reject writes because the deployed adapter is read-only."""
        _json_response(self, 405, '{"error":"method not allowed"}')

    def log_message(self, format: str, *args: object) -> None:
        """Avoid logging request details that could contain sensitive data."""
        return
