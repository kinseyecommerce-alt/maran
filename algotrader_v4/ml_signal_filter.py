"""
ml_signal_filter.py
Lightweight GBM classifier that learns from trade history to predict win probability.
Uses only features available at entry time (no lookahead).

Features:
  rsi_14, adx_14, volume_ratio, macd_hist (sign), bb_width, atr_pct,
  score (pattern score), session_bucket (encoded), agent (encoded), direction (encoded)

Training: triggered after every 50 new completed trades (lazy online batch).
Inference: called per signal in base_agent._run_loop via filter_signal().

Config: use_ml_filter=True, ml_filter_min_prob=0.45
"""
from __future__ import annotations

import json
import math
import pickle
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

_MODEL_PATH  = Path(__file__).parent / "logs" / "ml_signal_model.pkl"
_RETRAIN_EVERY = 50   # retrain after this many new trades accumulate


class MLSignalFilter:
    """Thread-safe GBM classifier with lazy online retraining."""

    def __init__(self) -> None:
        self._model   = None          # sklearn GBM
        self._encoder = {}            # categorical → int mapping
        self._lock    = threading.Lock()
        self._new_since_train = 0
        self._load()
        if self._model is None:
            self._warm_from_history()

    # ── Public API ────────────────────────────────────────────────────────────

    def filter_signal(
        self,
        *,
        rsi:          float,
        adx:          float,
        volume_ratio: float,
        macd_hist:    float,
        bb_width:     float,
        atr_pct:      float,
        score:        int,
        session:      str,
        agent:        str,
        direction:    str,   # "BUY" or "SELL"
        min_prob:     float = 0.45,
    ) -> tuple[bool, float]:
        """
        Returns (allowed, probability).
        If no model is trained yet, always returns (True, 0.5).
        """
        with self._lock:
            if self._model is None:
                return True, 0.5
        try:
            features = self._build_features(
                rsi, adx, volume_ratio, macd_hist, bb_width,
                atr_pct, score, session, agent, direction,
            )
            prob = float(self._model.predict_proba([features])[0][1])
            return prob >= min_prob, prob
        except Exception as exc:
            logger.debug("[MLFilter] inference error: {}", exc)
            return True, 0.5

    def record_outcome(self, features: dict, won: bool) -> None:
        """Call after a trade closes to feed the training buffer."""
        try:
            from state_store import state_store as _ss
            _ss  # just verify import works
        except Exception:
            pass
        with self._lock:
            self._new_since_train += 1
            if self._new_since_train >= _RETRAIN_EVERY:
                self._new_since_train = 0
                # Retrain in background to avoid blocking the event loop
                t = threading.Thread(target=self._retrain, daemon=True)
                t.start()

    # ── Training ─────────────────────────────────────────────────────────────

    def _warm_from_history(self) -> None:
        """Called during __init__ if no saved model found — triggers background retrain from existing trades."""
        try:
            rows = self._load_trade_history()
            n = len(rows)
            if n >= 30:
                logger.info("[MLFilter] Warming from {} historical trades...", n)
                t = threading.Thread(target=self._retrain, daemon=True)
                t.start()
        except Exception as exc:
            logger.debug("[MLFilter] warm-up probe failed: {}", exc)

    def _retrain(self) -> None:
        try:
            rows = self._load_trade_history()
            if len(rows) < 30:
                return
            X, y = [], []
            for row in rows:
                try:
                    feat = self._build_features(
                        rsi=float(row.get("rsi_at_entry", 50)),
                        adx=float(row.get("adx_at_entry", 20)),
                        volume_ratio=float(row.get("volume_ratio_at_entry", 1.0)),
                        macd_hist=float(row.get("macd_hist_at_entry", 0)),
                        bb_width=float(row.get("bb_width_at_entry", 2.0)),
                        atr_pct=float(row.get("atr_pct_at_entry", 1.0)),
                        score=int(row.get("score", 0) or 0),
                        session=str(row.get("session", "MIDDAY_TREND")),
                        agent=str(row.get("strategy", "intraday")),
                        direction=str(row.get("side", "BUY")),
                    )
                    X.append(feat)
                    y.append(1 if float(row.get("pnl", 0)) > 0 else 0)
                except Exception:
                    continue
            if len(X) < 30:
                return
            from sklearn.ensemble import GradientBoostingClassifier
            clf = GradientBoostingClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.1,
                subsample=0.8, random_state=42,
            )
            clf.fit(X, y)
            with self._lock:
                self._model = clf
            _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_MODEL_PATH, "wb") as f:
                pickle.dump((clf, self._encoder), f)
            wins = sum(y)
            logger.info("[MLFilter] retrained on {} trades | win rate {:.0f}%",
                        len(y), 100 * wins / max(len(y), 1))
        except Exception as exc:
            logger.warning("[MLFilter] retrain failed: {}", exc)

    def _load_trade_history(self) -> list[dict]:
        """Load closed trades from state_store (last 500 trades)."""
        try:
            import sqlite3
            db_path = Path(__file__).parent / "logs" / "algotrader.db"
            if not db_path.exists():
                return []
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM trades ORDER BY exit_time DESC LIMIT 500"
            ).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _load(self) -> None:
        if not _MODEL_PATH.exists():
            return
        try:
            with open(_MODEL_PATH, "rb") as f:
                clf, enc = pickle.load(f)
            with self._lock:
                self._model   = clf
                self._encoder = enc
            logger.info("[MLFilter] loaded model from {}", _MODEL_PATH)
        except Exception as exc:
            logger.debug("[MLFilter] could not load model: {}", exc)

    # ── Feature engineering ───────────────────────────────────────────────────

    def _encode(self, category: str, value: str) -> int:
        key = f"{category}:{value}"
        if key not in self._encoder:
            self._encoder[key] = len(self._encoder)
        return self._encoder[key]

    def _build_features(
        self,
        rsi: float, adx: float, volume_ratio: float, macd_hist: float,
        bb_width: float, atr_pct: float, score: int,
        session: str, agent: str, direction: str,
    ) -> list[float]:
        return [
            min(max(rsi, 0), 100),
            min(max(adx, 0), 100),
            min(max(volume_ratio, 0), 10),
            1.0 if macd_hist > 0 else (-1.0 if macd_hist < 0 else 0.0),
            min(max(bb_width, 0), 20),
            min(max(atr_pct, 0), 10),
            min(max(score, 0), 20),
            float(self._encode("session", session)),
            float(self._encode("agent", agent)),
            1.0 if direction == "BUY" else 0.0,
        ]


ml_signal_filter = MLSignalFilter()
