"""Versioned, safe JSON reports for RepoReady scans.

Reports contain check metadata and references only.  They deliberately never
copy scanned source text, including the contents of environment files.
"""

from __future__ import annotations

import json
import math
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
    r"authorization|cookie|client[ _-]?secret|password|passwd|secret|token|private[ _-]?key)\b"
    r"\s*(?:=|:)\s*)("
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\]\}\)]+)"
    r")"
)
_SECRET_PREFIX = re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")
_SECRET_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_MARKER = re.compile(
    r"(?i)\b(?:synthetic|fixture|test)(?:[ _-][A-Za-z0-9]+){0,3}"
    r"[ _-](?:secret|token|password)(?:[ _-][A-Za-z0-9]+)*\b"
)
_EXCEPTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)$")
_SCANNER_OPERATIONS: Final[frozenset[str]] = frozenset({
    "scandir failed",
    "stat failed",
    "read failed",
    "entry inspection failed",
})


def _redact(value: str) -> str:
    """Redact obvious secret assignments and well-known token prefixes."""

    redacted = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    redacted = _SECRET_PREFIX.sub("[REDACTED]", redacted)
    redacted = _SECRET_BEARER.sub("Bearer [REDACTED]", redacted)
    return _SECRET_MARKER.sub("[REDACTED]", redacted)


def _path_value(value: Path | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return _redact(value.as_posix())
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
    if isinstance(value, bytes):
        return _redact(value.decode("utf-8", errors="replace"))
    if isinstance(value, Path):
        return _redact(value.as_posix())
    if value is None or isinstance(value, (bool, int)):
        return str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "0"
    # Do not call arbitrary __str__ implementations: they can fail or return
    # source contents supplied by a caller.
    return f"<{type(value).__name__}>"


def _safe_sequence(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [_safe_text(values)]
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except Exception:
        return [_safe_text(values)]
    output: list[str] = []
    try:
        for value in iterator:
            output.append(_safe_text(value))
    except Exception:
        output.append("[unavailable sequence item]")
    return output


def _json_safe(value: object, _seen: set[int] | None = None) -> JsonValue:
    """Convert supported report values into JSON-compatible primitives."""

    if _seen is None:
        _seen = set()
    if isinstance(value, Enum):
        return _json_safe(value.value, _seen)
    if isinstance(value, (datetime,)):
        try:
            return _timestamp(value)
        except Exception:
            return "[invalid timestamp]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, bytes):
        return _redact(value.decode("utf-8", errors="replace"))
    if isinstance(value, Path):
        return value.as_posix()
    identity = id(value)
    if identity in _seen:
        return "[circular reference]"
    _seen.add(identity)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        try:
            items = value.items()
            for key, item in items:
                result[_safe_text(key)] = _json_safe(item, _seen)
        except Exception:
            result.setdefault("[unavailable mapping item]", None)
        finally:
            _seen.discard(identity)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result_list: list[JsonValue] = []
        try:
            for item in value:
                result_list.append(_json_safe(item, _seen))
        except Exception:
            result_list.append("[unavailable sequence item]")
        finally:
            _seen.discard(identity)
        return result_list
    _seen.discard(identity)
    return f"<{type(value).__name__}>"


def _scanner_error_summary(value: object) -> str:
    """Keep scanner errors useful without retaining provider-controlled text."""

    text = value if isinstance(value, str) else ""
    if text.startswith("Scanner error:"):
        text = text.removeprefix("Scanner error:").strip()
        parts = text.rsplit(": ", 2)
        if len(parts) >= 2:
            operation = parts[-2] if len(parts) == 3 else parts[-1]
            exception_name = parts[-1] if len(parts) == 3 else ""
            if operation in _SCANNER_OPERATIONS:
                suffix = f" ({exception_name})" if _EXCEPTION_NAME.fullmatch(exception_name) else ""
                return f"Scanner error: {operation}{suffix}"
        return "Scanner error: details unavailable"
    parts = text.rsplit(": ", 2)
    if len(parts) >= 2:
        operation = parts[-2] if len(parts) == 3 else parts[-1]
        exception_name = parts[-1] if len(parts) == 3 else ""
        if operation in _SCANNER_OPERATIONS:
            suffix = f" ({exception_name})" if _EXCEPTION_NAME.fullmatch(exception_name) else ""
            return f"Scanner error: {operation}{suffix}"
    return "Scanner error: details unavailable"


def _file_limitation(entry: object) -> str:
    """Describe an unreadable entry without copying its error text."""

    relative = getattr(entry, "relative_path", None)
    if isinstance(relative, Path):
        return f"{relative.as_posix()}: file could not be safely inspected."
    return "A file could not be safely inspected."


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
            limitations.append(_file_limitation(entry))
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
    def score_field(name: str, default: object = 0) -> object:
        try:
            return getattr(calculated, name)
        except Exception:
            return default

    raw_status_counts = score_field("status_counts", {})
    score_data: dict[str, JsonValue] = {
        "overall_score": _json_safe(score_field("overall_score", 0.0)),
        "maximum": _json_safe(score_field("maximum", 0.0)),
        "earned_weight": _json_safe(score_field("earned_weight", 0.0)),
        "applicable_weight": _json_safe(score_field("applicable_weight", 0.0)),
        "evaluable_weight": _json_safe(score_field("evaluable_weight", 0.0)),
        "coverage": _json_safe(score_field("coverage", 0.0)),
        "total_count": _json_safe(score_field("total_count", 0)),
        "applicable_count": _json_safe(score_field("applicable_count", 0)),
        "evaluable_count": _json_safe(score_field("evaluable_count", 0)),
        "skipped_count": _json_safe(score_field("skipped_count", 0)),
        "status_counts": {
            status.value: _json_safe(
                raw_status_counts.get(status.value, 0)
                if isinstance(raw_status_counts, Mapping) else 0
            )
            for status in _STATUS_ORDER
        },
        "formula": _safe_text(score_field("formula", "")),
        "limitations": _safe_sequence(score_field("limitations", ())),
    }
    scanner_data: dict[str, JsonValue] = {
        "limitations": _scanner_limitations(snapshot),
        "scan_errors": [_scanner_error_summary(error) for error in snapshot.scan_errors],
        "skipped_paths": [_redact(entry.as_posix()) for entry in snapshot.skipped_paths],
    }
    report: Report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": __version__},
        "generated_at": timestamp,
        "scan_timestamp": timestamp,
        "project": {"path": _redact(str(snapshot.root))},
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
        raise ReportWriteError("could not serialize report safely") from exc

    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        return destination
    except Exception as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        cleanup_error: Exception | None = None
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except Exception as cleanup_exc:  # make cleanup failures explicit
                cleanup_error = cleanup_exc
        detail = f"could not atomically write report to {destination}"
        if cleanup_error is not None:
            detail += "; temporary-file cleanup failed"
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
