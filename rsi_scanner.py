#!/usr/bin/env python3
"""Compatibility entry point. All indicators now share scanner.py and its state.

Use the single v4 workflow; do not schedule this wrapper separately.
"""
from scanner import main
from signals import calc_rsi, divergences_at, events_at

if __name__ == '__main__':
    raise SystemExit(main())
