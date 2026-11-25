"""Regression checks for the independent, multi-sizing sequence-form LP."""

import unittest

try:
    from pilots.river_solver.lp_check import check, exact_value
except ModuleNotFoundError as exc:
    if exc.name not in {"numpy", "scipy"}:
        raise
    raise unittest.SkipTest("Install the standalone river pilot requirements") from exc

from tests.test_river_solver import exact_lp, small


class SequenceFormChecks(unittest.TestCase):
    def test_existing_independent_one_raise_reference(self):
        spec = small()
        # Run an external scipy LP first, as happens in whole-suite discovery.
        reference = exact_lp(spec)
        result = exact_value(spec)
        self.assertAlmostEqual(result["lp_oop_bb"], reference, places=10)
        self.assertAlmostEqual(result["lp_oop_bb"], result["dual_oop_bb"], places=10)

    def test_multi_size_and_short_stack_on_identical_ranges(self):
        spec = small()
        spec["effective_stack_bb"] = 8
        spec["tree"] = {
            "bet_fractions": [0.33, 0.55, 1],
            "raise_fractions": [0.5, 1],
            "all_in": True,
            "max_raises": 2,
        }
        result = check(spec)
        self.assertTrue(result["passed"], result)
        self.assertGreater(result["terminal_histories"], 9)
