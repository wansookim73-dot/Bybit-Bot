from __future__ import annotations

from typing import Any, Dict

from tests.verify.scenarios_l2_spec import SCENARIOS
from tests.verify.l2_order_manager_harness import run_l2_scenario


def run_scenario_l2(sid: str) -> Dict[str, Any]:
    spec = SCENARIOS[sid]
    out = run_l2_scenario(spec)
    out["spec"] = spec
    out["sid"] = sid
    return out
