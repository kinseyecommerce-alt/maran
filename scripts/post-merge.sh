#!/bin/bash
set -e

# AlgoTrader Pro v4 — post-merge setup
# Runs automatically after every task agent merge.
# Must be: idempotent, non-interactive, fast.

echo "▶ post-merge: installing frontend dependencies..."
cd algotrader_v4/frontend
npm install --prefer-offline --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
cd ../..

echo "▶ post-merge: installing Python dependencies..."
pip install -q --upgrade pip 2>/dev/null || true
pip install -q "setuptools<60" 2>/dev/null || true
pip install -q -r algotrader_v4/requirements.txt 2>/dev/null || true

echo "▶ post-merge: done."
