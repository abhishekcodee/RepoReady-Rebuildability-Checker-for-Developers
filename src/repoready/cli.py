"""Command-line workflow for RepoReady."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .checks import run_checks
from .report import (
    ReportWriteError,
    _safe_text,
    _scanner_error_summary,
    build_report,
    write_json_report,
)
from .scanner import InvalidTargetError, scan_project
from .scoring import calculate_score, generate_recommendations


# Keep these values stable: they are part of the command-line interface.
SUCCESS = 0
INVALID_INPUT = 2
OPERATIONAL_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser used by the ``repoready`` command."""
    parser = argparse.ArgumentParser(
        prog="repoready",
        description=(
            "Inspect a software project with safe, static rebuildability checks. "
            "RepoReady never executes scanned project code or makes network calls."
        ),
        epilog=(
            "Exit codes: 0 successful scan/report (findings may exist), "
            "1 operational or JSON export error, 2 invalid CLI input or target."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="project directory to inspect",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        metavar="PATH",
        help="write the sanitized report as JSON to PATH",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include scanner error details in the limitations section",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _text(value: object) -> str:
    """Convert report values to display text without accessing source files."""
    return _safe_text(value)


def _write_stdout(text: str) -> None:
    """Write terminal text without crashing on a narrow Windows code page."""
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        sys.stdout.write(safe_text + "\n")


def _render_report(report: Mapping[str, Any], *, verbose: bool = False) -> str:
    """Render only sanitized values from :func:`build_report` for the terminal."""
    project = report.get("project")
    project_path = project.get("path", "(unknown)") if isinstance(project, Mapping) else "(unknown)"
    score = report.get("score")
    score_data = score if isinstance(score, Mapping) else {}
    overall = score_data.get("overall_score", 0)
    maximum = score_data.get("maximum", 0)
    coverage = score_data.get("coverage", 0)
    applicable = score_data.get("applicable_count", 0)
    evaluable = score_data.get("evaluable_count", 0)

    lines = [
        "RepoReady report",
        f"Project: {_text(project_path)}",
        f"Score: {_text(overall)}/{_text(maximum)}",
        f"Coverage: {_text(coverage)}%",
        f"Checks: {_text(applicable)} applicable, {_text(evaluable)} evaluable",
        "",
        "Checks:",
    ]
    checks = report.get("checks", ())
    if isinstance(checks, (list, tuple)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            name = _text(check.get("name", "unknown"))
            status = _text(check.get("status", "UNKNOWN"))
            explanation = _text(check.get("explanation", ""))
            lines.append(f"- [{status}] {name}: {explanation}")
    else:
        lines.append("- [UNKNOWN] No check results available")

    recommendations = report.get("recommendations", ())
    if isinstance(recommendations, (list, tuple)):
        lines.extend(("", "Recommendations:"))
        if recommendations:
            for recommendation in recommendations:
                if not isinstance(recommendation, Mapping):
                    continue
                priority = _text(recommendation.get("priority", "medium")).upper()
                message = _text(recommendation.get("message", ""))
                location = recommendation.get("path")
                line_number = recommendation.get("line_number")
                if location:
                    location_text = _text(location)
                    if line_number:
                        location_text += f":{_text(line_number)}"
                    message += f" ({location_text})"
                lines.append(f"- [{priority}] {message}")
        else:
            lines.append("- None")

    lines.extend(("", "Limitations:"))
    limitations: list[str] = []
    scanner = report.get("scanner")
    if isinstance(scanner, Mapping):
        scanner_limitations = scanner.get("limitations", ())
        if isinstance(scanner_limitations, (list, tuple)):
            limitations.extend(_text(item) for item in scanner_limitations)
        scan_errors = scanner.get("scan_errors", ())
        if isinstance(scan_errors, (list, tuple)):
            if verbose:
                limitations.extend(_scanner_error_summary(item) for item in scan_errors)
            elif scan_errors:
                limitations.append(
                    f"{len(scan_errors)} scanner error(s) were recorded; rerun with --verbose for details."
                )
        if verbose:
            skipped_paths = scanner.get("skipped_paths", ())
            if isinstance(skipped_paths, (list, tuple)):
                limitations.extend(f"Skipped path: {_text(item)}" for item in skipped_paths)
    score_limitations = score_data.get("limitations", ())
    if isinstance(score_limitations, (list, tuple)):
        limitations.extend(_text(item) for item in score_limitations)
    if isinstance(checks, (list, tuple)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            check_limitations = check.get("limitations", ())
            if isinstance(check_limitations, (list, tuple)):
                limitations.extend(_text(item) for item in check_limitations)
    unique_limitations = list(dict.fromkeys(limitations))
    if unique_limitations:
        lines.extend(f"- {item}" for item in unique_limitations)
    else:
        lines.append("- None reported")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Scan a project, print its report, and optionally export JSON."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.path is None:
        if args.json_path is not None:
            print("error: --json requires a project PATH", file=sys.stderr)
            return INVALID_INPUT
        parser.print_help()
        return SUCCESS

    try:
        snapshot = scan_project(args.path)
    except InvalidTargetError:
        print("error: scan target must be an existing directory", file=sys.stderr)
        return INVALID_INPUT
    except (OSError, ValueError):
        print("error: could not inspect the scan target", file=sys.stderr)
        return OPERATIONAL_ERROR
    except Exception:
        # A filesystem provider or scanner extension must not be able to put
        # its exception text in the terminal.
        print("error: could not inspect the scan target", file=sys.stderr)
        return OPERATIONAL_ERROR

    try:
        results = run_checks(snapshot)
        score = calculate_score(results)
        # Generate explicitly here so the complete Stage 3 workflow is invoked;
        # build_report repeats this from the same sanitized check results.
        generate_recommendations(results)
        report = build_report(snapshot, results, score)
    except Exception:
        # Do not expose exception details, which could contain project content.
        print("error: RepoReady could not complete the scan report", file=sys.stderr)
        return OPERATIONAL_ERROR

    if args.json_path is not None:
        try:
            output = write_json_report(report, args.json_path)
        except ReportWriteError:
            print("error: could not write the JSON report", file=sys.stderr)
            return OPERATIONAL_ERROR
        except (OSError, ValueError):
            print("error: could not write the JSON report", file=sys.stderr)
            return OPERATIONAL_ERROR
        except Exception:
            # Never expose report-path or filesystem-provider exception text.
            print("error: could not write the JSON report", file=sys.stderr)
            return OPERATIONAL_ERROR
    else:
        output = None

    _write_stdout(_render_report(report, verbose=args.verbose))
    if output is not None:
        _write_stdout(f"JSON report written to {_text(Path(output).as_posix())}")
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
