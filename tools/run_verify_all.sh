#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m compileall -q tests/verify

# L0 + L1
PYTHONHASHSEED=0 python3 -m pytest -q -s -x \
  tests/test_verify_l0.py \
  tests/test_verify_l1_gridlogic.py

# L2: OrderManager ↔ Exchange boundary verification
PYTHONHASHSEED=0 python3 -m pytest -q -s -x tests/test_verify_l2_order_manager.py
