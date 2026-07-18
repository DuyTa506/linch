"""Offline eval-suite contribution pack."""

from __future__ import annotations

import yaml  # type: ignore[reportMissingModuleSource]

from ..contributions import FileContribution
from ..ir import CompilerIR
from .common import file


class EvalPack:
    capability_id = "evals.offline"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        config = ir.capabilities()["evals"]
        if not config["enabled"]:
            return ()
        cases = [
            {
                "id": case["id"],
                "prompt": case["prompt"],
                "expected": case["expectedContains"],
            }
            for case in config["cases"]
        ]
        suite = {
            "name": f"{ir.project_name}-offline",
            "cases": cases,
            "scorers": [{"type": "run_completed"}],
        }
        return (
            file(
                "evals/suite.yaml",
                yaml.safe_dump(suite, allow_unicode=True, sort_keys=False),
                "evals.suite",
            ),
            file(
                f"src/{ir.package}/evals.py",
                _eval_module(ir, cases),
                "evals.runner",
            ),
            file("tests/test_evals.py", _eval_test(ir), "tests.evals"),
        )


def _eval_module(ir: CompilerIR, cases: list[dict[str, object]]) -> str:
    case_exprs = []
    for case in cases:
        case_exprs.append(
            f"EvalCase(prompt={case['prompt']!r}, expected={case['expected']!r}, "
            f"metadata={{'id': {case['id']!r}}})"
        )
    return f'''\
"""Deterministic offline eval suite definition."""

from linch import EvalCase, EvalSuite, run_completed


SUITE = EvalSuite(
    name={ir.project_name + "-offline"!r},
    cases=[{", ".join(case_exprs)}],
    scorers=[run_completed],
)
'''


def _eval_test(ir: CompilerIR) -> str:
    return f'''\
"""Eval suite is loadable without credentials."""

from {ir.package}.evals import SUITE


def test_eval_suite_is_offline_data() -> None:
    assert SUITE.name == {ir.project_name + "-offline"!r}
    assert isinstance(SUITE.cases, list)
'''


__all__ = ["EvalPack"]
