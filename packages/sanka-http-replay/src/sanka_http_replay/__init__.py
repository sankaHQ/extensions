# SPDX-License-Identifier: Apache-2.0
"""Versioned HTTP scenario and observation contract shared by Sanka code extensions.

Every migration extension that replays an HTTP backend runs the same ordered
scenario list against the source application and the generated candidate, records
one observation per step, and compares the two observation lists. This package
owns the two documents (``sanka.http-scenarios/v1`` and
``sanka.http-observations/v1``), the default scenario generator for captured
single-table contracts, and the comparison. Runners (Node, Go, Rust probes) are
owned by each extension and only have to honour the runner contract in the README.
"""

from .observations import (
    OBSERVATION_SCHEMA,
    ObservationError,
    canonical,
    compare,
    difference,
    validate_observations,
)
from .scenarios import (
    MAX_SCENARIOS,
    SCENARIO_SCHEMA,
    ScenarioError,
    cases_document,
    default_scenarios,
    load_scenarios,
    validate_scenarios,
)

__all__ = [
    "MAX_SCENARIOS",
    "OBSERVATION_SCHEMA",
    "SCENARIO_SCHEMA",
    "ObservationError",
    "ScenarioError",
    "canonical",
    "cases_document",
    "compare",
    "default_scenarios",
    "difference",
    "load_scenarios",
    "validate_observations",
    "validate_scenarios",
]
