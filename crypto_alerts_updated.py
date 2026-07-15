#!/usr/bin/env python3
"""Backward-compatible executable name for Crypto Trading Alerts v2."""

from crypto_alerts.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
