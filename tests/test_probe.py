from __future__ import annotations

import unittest

from src.probe import ProbeDiagnostics, ProbeResult


def available_diagnostics(**overrides) -> ProbeDiagnostics:
    values = {
        "health": "healthy",
        "page_type": "booking",
        "date_control_count": 1,
        "plausible_date_option_count": 10,
        "target_found": True,
        "target_enabled": True,
        "shift_control_count": 1,
        "shift_control_found": True,
        "update_confirmed": True,
        "shift_count": 1,
    }
    values.update(overrides)
    return ProbeDiagnostics(**values)


class ProbeContractTests(unittest.TestCase):
    def test_available_requires_target_correlated_update(self) -> None:
        for field, value in (
            ("date_control_count", 0),
            ("plausible_date_option_count", 0),
            ("target_found", False),
            ("target_enabled", False),
            ("shift_control_count", 0),
            ("shift_control_found", False),
            ("update_confirmed", False),
            ("shift_count", 0),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ProbeResult(
                    "available",
                    shifts=("Mittag",),
                    diagnostics=available_diagnostics(**{field: value}),
                )

    def test_available_accepts_consistent_evidence(self) -> None:
        result = ProbeResult(
            "available",
            shifts=("Mittag",),
            diagnostics=available_diagnostics(),
        )
        self.assertEqual(result.status, "available")

    def test_unavailable_requires_valid_control_and_absent_target(self) -> None:
        valid = ProbeDiagnostics(
            health="healthy",
            page_type="booking",
            date_control_count=1,
            plausible_date_option_count=8,
            target_found=False,
        )
        self.assertEqual(
            ProbeResult("unavailable", shifts=(), diagnostics=valid).status,
            "unavailable",
        )
        for invalid in (
            ProbeDiagnostics(health="healthy"),
            ProbeDiagnostics(
                health="healthy",
                page_type="booking",
                date_control_count=1,
                plausible_date_option_count=8,
                target_found=True,
            ),
        ):
            with self.assertRaises(ValueError):
                ProbeResult("unavailable", shifts=(), diagnostics=invalid)

    def test_unavailable_accepts_confirmed_zero_capacity(self) -> None:
        diagnostics = available_diagnostics(
            shift_count=0,
            unavailable_confirmed=True,
        )
        result = ProbeResult(
            "unavailable", shifts=(), diagnostics=diagnostics
        )
        self.assertEqual(result.status, "unavailable")

        with self.assertRaises(ValueError):
            ProbeResult(
                "unavailable",
                shifts=(),
                diagnostics=available_diagnostics(shift_count=0),
            )

    def test_unavailable_accepts_confirmed_empty_relevant_feed(self) -> None:
        diagnostics = ProbeDiagnostics(
            health="healthy",
            page_type="booking",
            date_control_count=1,
            plausible_date_option_count=0,
            target_found=False,
            unavailable_confirmed=True,
        )
        result = ProbeResult(
            "unavailable", shifts=(), diagnostics=diagnostics
        )
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
