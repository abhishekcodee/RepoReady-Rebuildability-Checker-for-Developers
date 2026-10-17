import pathlib
import sys
import unittest

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from repoready.checks import CheckResult, Status
from repoready.scoring import (
    CHECK_WEIGHTS,
    RecommendationPriority,
    calculate_score,
    generate_recommendations,
)


def result(name, status, recommendation="", **kwargs):
    return CheckResult(name, status, f"{name} explanation", recommendation=recommendation, **kwargs)


class ScoringTests(unittest.TestCase):
    def test_policy_weights_and_all_statuses(self):
        self.assertEqual(sum(CHECK_WEIGHTS.values()), 100)
        values = [
            result("git_repository", Status.PASS),
            result("dependency_lockfile", Status.WARNING),
            result("pinned_runtime", Status.FAIL),
            result("dockerfile", Status.UNKNOWN),
            result("automated_tests", Status.NOT_APPLICABLE, applicable=False),
        ]
        score = calculate_score(values)
        self.assertEqual(score.status_counts["PASS"], 1)
        self.assertEqual(score.status_counts["WARNING"], 1)
        self.assertEqual(score.status_counts["FAIL"], 1)
        self.assertEqual(score.status_counts["UNKNOWN"], 1)
        self.assertEqual(score.status_counts["NOT_APPLICABLE"], 1)
        self.assertEqual(score.applicable_weight, 55)
        self.assertEqual(score.evaluable_weight, 40)
        self.assertEqual(score.coverage, round(100 * 40 / 55, 2))
        self.assertEqual(score.overall_score, round(100 * 17.5 / 55, 2))

    def test_all_applicable_pass_is_perfect_and_preserves_identity(self):
        values = tuple(result(name, Status.PASS) for name in CHECK_WEIGHTS)
        score = calculate_score(values)
        self.assertEqual(score.overall_score, 100.0)
        self.assertEqual(score.maximum, 100.0)
        self.assertEqual(score.coverage, 100.0)
        self.assertIs(score.results[0], values[0])
        self.assertEqual(score.applicable_count, 8)
        self.assertEqual(score.evaluable_count, 8)

    def test_na_is_excluded_and_empty_input_is_safe(self):
        na = tuple(result(name, Status.NOT_APPLICABLE, applicable=False) for name in CHECK_WEIGHTS)
        score = calculate_score(na)
        self.assertEqual(score.overall_score, 0.0)
        self.assertEqual(score.applicable_weight, 0.0)
        self.assertEqual(score.coverage, 0.0)
        empty = calculate_score([])
        self.assertEqual(empty.total_count, 0)
        self.assertTrue(empty.limitations)
        self.assertEqual(generate_recommendations(None), ())

    def test_unknown_reduces_coverage_but_not_score_denominator(self):
        score = calculate_score([
            result("git_repository", "PASS"),
            result("dependency_lockfile", "unknown"),
        ])
        self.assertEqual(score.overall_score, round(100 * 10 / 25, 2))
        self.assertEqual(score.coverage, round(100 * 10 / 25, 2))
        self.assertEqual(score.evaluable_weight, 10)

    def test_malformed_results_and_deterministic_bounds(self):
        score = calculate_score([object(), result("git_repository", "PASS"),
                                 result("unexpected", "PASS"), result("readme", "bogus")])
        self.assertEqual(score.overall_score, 100.0)
        self.assertGreaterEqual(score.overall_score, 0.0)
        self.assertLessEqual(score.overall_score, 100.0)
        self.assertEqual(score.results[0].name, "git_repository")
        self.assertGreaterEqual(len(score.limitations), 2)
        self.assertEqual(calculate_score(reversed(score.results)).results, score.results)

    def test_recommendations_fallback_dedupe_priorities_and_context(self):
        path = pathlib.Path("src/app.py")
        values = [
            result("readme", Status.FAIL),
            result("readme", Status.FAIL),
            result("absolute_paths", Status.WARNING, "Use a relative path.", path=path, line_number=7),
            result("dockerfile", Status.PASS, "should not appear", path=path, line_number=8),
        ]
        recommendations = generate_recommendations(values)
        self.assertEqual(len(recommendations), 2)
        self.assertEqual(recommendations[0].priority, RecommendationPriority.HIGH)
        self.assertEqual(recommendations[0].severity, Status.FAIL)
        self.assertIn("README", recommendations[0].message)
        self.assertEqual(recommendations[1].priority, RecommendationPriority.MEDIUM)
        self.assertEqual(recommendations[1].path, path)
        self.assertEqual(recommendations[1].line_number, 7)
        self.assertNotIn("source", recommendations[1].message.lower())


if __name__ == "__main__":
    unittest.main()
