"""Versioned, safe JSON reports for RepoReady scans.

Reports contain check metadata and references only.  They deliberately never
copy scanned source text, including the contents of environment files.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, TypeAlias

from . import __version__
from .checks import CheckResult, Status
from .scanner import ProjectSnapshot
from .scoring import Recommendation, ScoreResult, calculate_score, generate_recommendations


REPORT_SCHEMA_VERSION: Final[str] = "1.0"
TOOL_NAME: Final[str] = "RepoReady"

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Report: TypeAlias = dict[str, JsonValue]


class ReportWriteError(RuntimeError):
    """Raised when a report cannot be written atomically."""


_STATUS_ORDER: Final[tuple[Status, ...]] = (
    Status.PASS,
    Status.FAIL,
    Status.WARNING,
    Status.NOT_APPLICABLE,
    Status.UNKNOWN,
)

# This intentionally targets assignment-like forms rather than arbitrary
# prose.  It protects synthetic CheckResult values while retaining useful
# labels and surrounding explanations.
_SECRET_ASSIGNMENT = re.compile(
    r"(?ix)(\b(?:api[ _-]?key|access[ _-]?token|auth(?:entication)?[ _-]?token|"
    r"client[ _-]?secret|password|passwd|secret|token|private[ _-]?key)\b"
    r"\s*(?:=|:)\s*)("
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\]\}\)]+)"
    r")"
)
_SECRET_PREFIX = re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")


def _redact(value: str) -> str:
    """Redact obvious secret assignments and well-known token prefixes."""

    redacted = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return _SECRET_PREFIX.sub("[REDACTED]", redacted)


def _path_value(value: Path | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def _status_value(value: object) -> str:
    if isinstance(value, Status):
        return value.value
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str):
        candidate = value.strip().upper()
        supported = {status.value for status in _STATUS_ORDER}
        return candidate if candidate in supported else Status.UNKNOWN.value
    return Status.UNKNOWN.value


def _safe_text(value: object) -> str:
    if isinstance(value, str):
        return _redact(value)
    return _redact(str(value))


def _safe_sequence(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [_safe_text(values.decode() if isinstance(values, bytes) else values)]
    try:
        return [_safe_text(value) for value in values]  # type: ignore[union-attr]
    except TypeError:
        return [_safe_text(values)]


def _json_safe(value: object) -> JsonValue:
    """Convert supported report values into JSON-compatible primitives."""

    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, (datetime,)):
        return _timestamp(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value  # type: ignore[return-value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _timestamp(value: datetime) -> str:
    """Return an ISO-8601 UTC timestamp with the required ``Z`` suffix."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _check_report(result: CheckResult, recommendations: tuple[Recommendation, ...]) -> dict[str, JsonValue]:
    status = _status_value(result.status)
    path = result.path if isinstance(result.path, Path) else None
    line_number = result.line_number if isinstance(result.line_number, int) and result.line_number > 0 else None
    matching = next(
        (
            recommendation
            for recommendation in recommendations
            if recommendation.check_name == result.name
            and recommendation.severity.value == status
            and recommendation.path == path
            and recommendation.line_number == line_number
        ),
        None,
    )
    recommendation_text = _safe_text(result.recommendation) if result.recommendation else ""
    priority: str | None = None
    if matching is not None:
        recommendation_text = _safe_text(matching.message)
        priority = matching.priority.value
    return {
        "name": _safe_text(result.name),
        "status": status,
        "explanation": _safe_text(result.explanation),
        "evidence": _safe_sequence(result.evidence),
        "recommendation": recommendation_text,
        "priority": priority,
        "applicable": result.applicable is not False,
        "path": _path_value(path),
        "line_number": line_number,
        "limitations": _safe_sequence(result.limitations),
    }


def _recommendation_report(recommendation: Recommendation) -> dict[str, JsonValue]:
    return {
        "priority": recommendation.priority.value,
        "severity": recommendation.severity.value,
        "check_name": _safe_text(recommendation.check_name),
        "message": _safe_text(recommendation.message),
        "path": _path_value(recommendation.path),
        "line_number": recommendation.line_number,
    }


def _status_counts(results: Iterable[CheckResult]) -> dict[str, int]:
    counts = {status.value: 0 for status in _STATUS_ORDER}
    for result in results:
        status = _status_value(result.status)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _scanner_limitations(snapshot: ProjectSnapshot) -> list[str]:
    limitations = [
        "Static filesystem inspection does not execute project code or follow symlinks.",
        "Sensitive files such as .env and private-key files are not loaded.",
    ]
    if snapshot.skipped_paths:
        limitations.append(f"Skipped {len(snapshot.skipped_paths)} path(s) by scanner policy.")
    for entry in snapshot.files:
        if entry.error:
            limitations.append(f"{entry.relative_path.as_posix()}: {entry.error}")
    return list(dict.fromkeys(_redact(item) for item in limitations))


def build_report(
    snapshot: ProjectSnapshot,
    results: Iterable[CheckResult] | None,
    score: ScoreResult | None = None,
    *,
    generated_at: datetime | None = None,
) -> Report:
    """Build a versioned JSON-safe report from one scan and its check results.

    ``generated_at`` is useful for deterministic callers and is interpreted as
    UTC when naive.  The report contains references to files, never file text.
    """

    if results is None:
        supplied: tuple[CheckResult, ...] = ()
    else:
        try:
            supplied = tuple(result for result in results if isinstance(result, CheckResult))
        except TypeError:
            supplied = ()
    calculated = score if score is not None else calculate_score(supplied)
    recommendations = generate_recommendations(supplied)
    timestamp = _timestamp(generated_at or datetime.now(timezone.utc))
    score_data: dict[str, JsonValue] = {
        "overall_score": calculated.overall_score,
        "maximum": calculated.maximum,
        "earned_weight": calculated.earned_weight,
        "applicable_weight": calculated.applicable_weight,
        "evaluable_weight": calculated.evaluable_weight,
        "coverage": calculated.coverage,
        "total_count": calculated.total_count,
        "applicable_count": calculated.applicable_count,
        "evaluable_count": calculated.evaluable_count,
        "skipped_count": calculated.skipped_count,
        "status_counts": {
            status.value: int(calculated.status_counts.get(status.value, 0))
            for status in _STATUS_ORDER
        },
        "formula": _redact(calculated.formula),
        "limitations": _safe_sequence(calculated.limitations),
    }
    scanner_data: dict[str, JsonValue] = {
        "limitations": _scanner_limitations(snapshot),
        "scan_errors": [_redact(error) for error in snapshot.scan_errors],
        "skipped_paths": [entry.as_posix() for entry in snapshot.skipped_paths],
    }
    report: Report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": __version__},
        "generated_at": timestamp,
        "scan_timestamp": timestamp,
        "project": {"path": str(snapshot.root)},
        "score": score_data,
        "summary": {"status_counts": _status_counts(supplied)},
        "checks": [_check_report(result, recommendations) for result in supplied],
        "recommendations": [_recommendation_report(item) for item in recommendations],
        "scanner": scanner_data,
    }
    return _json_safe(report)  # type: ignore[return-value]


def serialize_report(report: Mapping[str, object], *, indent: int = 2) -> str:
    """Serialize a report with Unicode preserved and supported values normalized."""

    return json.dumps(
        _json_safe(report),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
        allow_nan=False,
    )


# Friendly aliases for callers that prefer a noun-oriented serializer name.
report_to_json = serialize_report
to_json = serialize_report


def write_json_report(report: Mapping[str, object], output_path: str | os.PathLike[str]) -> Path:
    """Atomically write *report* to an existing destination directory.

    No parent directories are created.  A temporary file is created beside the
    destination and replaced into place only after the complete JSON is flushed.
    """

    destination = Path(output_path)
    parent = destination.parent
    if not parent.exists():
        raise ReportWriteError(f"report parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise ReportWriteError(f"report parent path is not a directory: {parent}")
    try:
        payload = serialize_report(report) + "\n"
    except Exception as exc:
        raise ReportWriteError(f"could not serialize report: {exc}") from exc

    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        return destination
    except Exception as exc:
        cleanup_error: Exception | None = None
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except Exception as cleanup_exc:  # make cleanup failures explicit
                cleanup_error = cleanup_exc
        detail = f"could not atomically write report to {destination}: {exc}"
        if cleanup_error is not None:
            detail += f"; temporary-file cleanup failed: {cleanup_error}"
        raise ReportWriteError(detail) from exc


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "ReportWriteError",
    "build_report",
    "serialize_report",
    "report_to_json",
    "to_json",
    "write_json_report",
]
