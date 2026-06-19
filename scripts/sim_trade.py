#!/usr/bin/env python3
"""Disabled legacy simulation bootstrap script.

The old server-only script directly mutated positions rows, including user
portfolio rows. Keep this filename as a fail-closed compatibility guard so an
operator cannot accidentally revive that path.
"""

import sys


MESSAGE = """[DISABLED] scripts/sim_trade.py is a legacy one-off DB mutator.

Use these reviewed entry points instead:
- User holdings: python3 /app/scripts/trade_update.py ...
- User holding reads: python3 /app/scripts/read_positions.py ...
- Simulation repair: python3 /app/scripts/sim_position_reconcile.py with its reviewed hash gate
- Alert simulation/paper flow: rt_alert_bridge.py -> rt_order_intake.py

This guard intentionally performs no DB writes.
"""


def main():
    print(MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
