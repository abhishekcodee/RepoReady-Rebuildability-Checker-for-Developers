"""Weighted scoring and actionable recommendations for RepoReady checks.

The scoring policy is deliberately small and explicit.  A PASS earns all of
its check's weight, a WARNING earns half, and FAIL or UNKNOWN earns none.
NOT_APPLICABLE is omitted from the denominator.  The resulting score is:

``100 * earned_weight / applicable_weight``

where ``earned_weight`` applies the status factors above.  Coverage is the
percentage of applicable weight represented by PASS, WARNING, or FAIL
results; UNKNOWN results remain in the denominator and therefore reduce
coverage.  A score is a static quality signal, not a build guarantee.

This module intentionally does not serialize its dataclasses.  Consumers such
as ``report.py`` can use the preserved :class:`~repoready.checks.CheckResult`
objects directly.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .checks import CheckResult, Status


# Keep this order stable: it is also the order used in score and recommendation
# output.  The values are intentionally easy to audit and total exactly 100.
CHECK_WEIGHTS: Final[Mapping[str, int]] = MappingProxyType({
    "git_repository": 10,
    "dependency_lockfile": 15,
    "pinned_runtime": 15,
    "dockerfile": 15,
    "automated_tests": 15,
    "readme": 10,
    "environment_example": 10,
    "absolute_paths": 10,
})
TOTAL_WEIGHT: Final[int] = sum(CHECK_WEIGHTS.values())

_STATUS_FACTORS: Final[Mapping[Status, float]] = MappingProxyType({
    Status.PASS: 1.0,
    Status.WARNING: 0.5,
    Status.FAIL: 0.0,
    Status.UNKNOWN: 0.0,
})
_EVALUABLE_STATUSES: Final[frozenset[Status]] = frozenset({
    Status.PASS,
    Status.WARNING,
    Status.FAIL,
})
_STATUS_ORDER: Final[tuple[Status, ...]] = (
    Status.PASS,
    Status.WARNING,
    Status.FAIL,
    Status.UNKNOWN,
    Status.NOT_APPLICABLE,
)


class RecommendationPriority(str, Enum):
    """Action urgency assigned to a recommendation."""

    HIGH = "high"
    MEDIUM = "medium"


@dataclass(frozen=True)
class Recommendation:
    """A safe, actionable recommendation derived from one check result.

    ``path`` and ``line_number`` are references only.  No source line or
    evidence text is copied into this object, so it is safe for a report to
    display the context without exposing scanned contents.
    """

    priority: RecommendationPriority
    severity: Status
    check_name: str
    message: str
    path: Path | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class ScoreResult:
    """Typed weighted score and the inputs needed by a future report.

    ``maximum`` is always 100 for the normalized scale.  With no applicable
    checks there is no meaningful denominator, so ``overall_score`` and
    ``coverage`` are 0.  ``results`` contains the original valid
    :class:`CheckResult` instances in deterministic order; no result is
    reconstructed or mutated.
    """

    overall_score: float
    maximum: float
    earned_weight: float
    applicable_weight: float
    evaluable_weight: float
    coverage: float
    total_count: int
    applicable_count: int
    evaluable_count: int
    skipped_count: int
    status_counts: Mapping[str, int]
    results: tuple[CheckResult, ...]
    formula: str
    limitations: tuple[str, ...] = ()

    @property
    def check_results(self) -> tuple[CheckResult, ...]:
        """Return preserved check results under a report-friendly name."""

        return self.results


_FALLBACK_RECOMMENDATIONS: Final[Mapping[str, str]] = MappingProxyType({
    "git_repository": "Initialize Git and create a commit.",
    "dependency_lockfile": "Add and commit the appropriate dependency lockfile.",
    "pinned_runtime": "Pin an exact runtime version or Docker base digest.",
    "dockerfile": "Add or improve the Dockerfile with a pinned base image and runtime command.",
    "automated_tests": "Add tests and document one offline test command.",
    "readme": "Document setup/install, run/usage, and test steps in README.md.",
    "environment_example": "Add a sanitized .env.example for the environment variables used by the project.",
    "absolute_paths": "Replace machine-specific paths with relative or configurable paths.",
})


def _coerce_status(value: object) -> Status | None:
    """Return a supported status for an enum or case-insensitive string."""

    if isinstance(value, Status):
        return value
    if isinstance(value, str):
        candidate = value.strip().upper()
        try:
            return Status(candidate)
        except ValueError:
            return None
    return None


def _stable_result_order(result: CheckResult) -> tuple[int, str]:
    """Sort known checks by policy order and unexpected names afterward."""

    name = result.name if isinstance(result.name, str) else ""
    try:
        index = tuple(CHECK_WEIGHTS).index(name)
    except ValueError:
        index = len(CHECK_WEIGHTS)
    return index, name


def _iter_results(results: Iterable[object] | None) -> tuple[list[CheckResult], list[str]]:
    """Collect CheckResult values without allowing malformed iterables to crash."""

    limitations: list[str] = []
    if results is None:
        return [], ["No check results were supplied."]
    try:
        iterator = iter(results)
    except TypeError:
        return [], ["Check results input was not iterable; no checks were scored."]

    valid: list[CheckResult] = []
    while True:
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception as exc:  # defensive boundary for caller-owned iterators
            limitations.append(f"Check results iteration stopped: {type(exc).__name__}.")
            break
        if isinstance(item, CheckResult):
            valid.append(item)
        else:
            limitations.append("Skipped an input that was not a CheckResult.")
    return valid, limitations


def calculate_score(results: Iterable[object] | None) -> ScoreResult:
    """Calculate a bounded normalized score from check results.

    The eight stable check names in :data:`CHECK_WEIGHTS` are scored.  Unknown
    names, invalid statuses, duplicate stable names, and malformed values are
    skipped safely and described in ``limitations``.  The first occurrence of
    a duplicate stable name is the scored occurrence, while every valid
    ``CheckResult`` remains available in ``results`` for reporting.
    """

    supplied, limitations = _iter_results(results)
    ordered = tuple(sorted(supplied, key=_stable_result_order))
    selected: dict[str, CheckResult] = {}
    scored: list[tuple[CheckResult, Status, int]] = []
    counts = {status.value: 0 for status in _STATUS_ORDER}
    for result in ordered:
        name = result.name if isinstance(result.name, str) else ""
        weight = CHECK_WEIGHTS.get(name)
        status = _coerce_status(result.status)
        if weight is None:
            limitations.append(f"Skipped unsupported check name {name!r}.")
            continue
        if name in selected:
            limitations.append(f"Skipped duplicate result for {name!r}; first result was scored.")
            continue
        selected[name] = result
        if status is None:
            limitations.append(f"Skipped {name!r}: status was not recognized.")
            continue
        counts[status.value] += 1
        applicable = status is not Status.NOT_APPLICABLE and result.applicable is not False
        if not applicable:
            continue
        scored.append((result, status, weight))

    earned = 0.0
    applicable_weight = 0.0
    evaluable_weight = 0.0
    applicable_count = 0
    evaluable_count = 0
    for _, status, weight in scored:
        applicable_weight += weight
        applicable_count += 1
        factor = _STATUS_FACTORS.get(status)
        if factor is not None and status in _EVALUABLE_STATUSES:
            earned += weight * factor
            evaluable_weight += weight
            evaluable_count += 1

    if applicable_weight:
        overall = 100.0 * earned / applicable_weight
        coverage = 100.0 * evaluable_weight / applicable_weight
    else:
        overall = 0.0
        coverage = 0.0
        limitations.append("No applicable checks supplied; normalized score has no denominator.")

    # Round only at the boundary, then clamp to guarantee the documented range.
    overall = min(100.0, max(0.0, round(overall, 2)))
    coverage = min(100.0, max(0.0, round(coverage, 2)))
    return ScoreResult(
        overall_score=overall,
        maximum=100.0,
        earned_weight=round(earned, 2),
        applicable_weight=applicable_weight,
        evaluable_weight=evaluable_weight,
        coverage=coverage,
        total_count=len(supplied),
        applicable_count=applicable_count,
        evaluable_count=evaluable_count,
        skipped_count=len(supplied) - len(scored),
        status_counts=MappingProxyType(counts),
        results=ordered,
        formula=("overall_score = 100 * earned_weight / applicable_weight; "
                 "PASS=100% of weight, WARNING=50%, FAIL=0%, UNKNOWN=0%; "
                 "NOT_APPLICABLE is excluded. coverage = 100 * evaluable_weight / "
                 "applicable_weight."),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def score_checks(results: Iterable[object] | None) -> ScoreResult:
    """Compatibility alias for callers that describe scoring as ``score_checks``."""

    return calculate_score(results)


def _recommendation_context(result: CheckResult) -> tuple[Path | None, int | None]:
    """Extract only safe path and positive line references from a result."""

    path = result.path if isinstance(result.path, Path) else None
    line = result.line_number if isinstance(result.line_number, int) and result.line_number > 0 else None
    return path, line


def generate_recommendations(results: Iterable[object] | None) -> tuple[Recommendation, ...]:
    """Create deterministic, deduplicated recommendations for FAIL/WARNING.

    Existing non-empty ``CheckResult.recommendation`` text takes precedence;
    otherwise a beginner-friendly check-name fallback is used.  Only the
    recommendation text and optional path/line reference are retained—source
    contents, evidence, and secrets are never copied.
    """

    supplied, _ = _iter_results(results)
    ordered = sorted(supplied, key=_stable_result_order)
    recommendations: list[Recommendation] = []
    seen: set[tuple[str, str, str, str | None, int | None]] = set()
    for result in ordered:
        status = _coerce_status(result.status)
        if status not in (Status.FAIL, Status.WARNING):
            continue
        name = result.name if isinstance(result.name, str) else "unknown"
        custom = result.recommendation.strip() if isinstance(result.recommendation, str) else ""
        message = custom or _FALLBACK_RECOMMENDATIONS.get(
            name, f"Review the {name} check and address the reported issue."
        )
        path, line = _recommendation_context(result)
        key = (name, status.value, message, path.as_posix() if path else None, line)
        if key in seen:
            continue
        seen.add(key)
        recommendations.append(Recommendation(
            priority=(RecommendationPriority.HIGH if status is Status.FAIL
                      else RecommendationPriority.MEDIUM),
            severity=status,
            check_name=name,
            message=message,
            path=path,
            line_number=line,
        ))
    return tuple(recommendations)


__all__ = [
    "CHECK_WEIGHTS",
    "TOTAL_WEIGHT",
    "Recommendation",
    "RecommendationPriority",
    "ScoreResult",
    "calculate_score",
    "generate_recommendations",
    "score_checks",
]
