"""ml_signal_scorer.py — sklearn-based ML signal scorer for NSE/BSE algo trading."""
from __future__ import annotations

import pickle
import threading
from pathlib import Path

import numpy as np
from loguru import logger

from config import settings

_MODEL_DIR = Path(__file__).parent / "data" / "ml_models"


class MLSignalScorer:
    def __init__(self) -> None:
        self._models: dict[str, object] = {}   # symbol -> Pipeline(scaler+clf)
        self._loaded: set[str] = set()
        self._lock   = threading.Lock()
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def _features_from_snap(self, snap) -> np.ndarray:
        """Extract 12 features from a MarketSnapshot; return shape (12,) clamped to [-5, 5]."""
        ind = snap.indicators
        ltp = float(snap.tick.ltp)

        ema9 = float(ind.ema9 or ltp)
        ema21 = float(ind.ema21 or ltp)
        vwap = float(ind.vwap or ltp) or 1.0
        rsi_14 = float(ind.rsi_14 or 50)
        rsi_7 = float(ind.rsi_7 or 50)
        macd = float(ind.macd or 0)
        macd_hist = float(ind.macd_hist or 0)
        bb_upper = float(ind.bb_upper or ltp * 1.02)
        bb_lower = float(ind.bb_lower or ltp * 0.98)
        atr_14 = float(ind.atr_14 or 1) or 1.0
        stoch_k = float(ind.stoch_rsi_k or 50)
        stoch_d = float(ind.stoch_rsi_d or 50)

        bb_range = max(bb_upper - bb_lower, 1.0)

        feats = np.array([
            ema9 / max(ema21, 1e-6),                          # 1. EMA9/EMA21 ratio
            (ltp - vwap) / vwap,                              # 2. price vs VWAP
            rsi_14 / 100.0,                                   # 3. RSI-14 normalised
            np.clip(macd_hist / atr_14, -3, 3),               # 4. MACD hist / ATR (clamped)
            (ltp - bb_lower) / bb_range,                      # 5. BB position 0-1
            atr_14 / max(ltp, 1e-6),                          # 6. volatility ratio
            stoch_k / 100.0,                                  # 7. Stoch RSI K
            stoch_d / 100.0,                                  # 8. Stoch RSI D
            (bb_upper - ltp) / atr_14,                        # 9. distance to BB upper (matches training)
            (ltp - bb_lower) / atr_14,                        # 10. distance to BB lower (matches training)
            rsi_7 / 100.0,                                    # 11. RSI-7 normalised
            np.clip(macd / atr_14, -3, 3),                   # 12. MACD / ATR (clamped, matches feature 4)
        ], dtype=float)

        feats = np.nan_to_num(feats, nan=0.0, posinf=5.0, neginf=-5.0)
        return np.clip(feats, -5.0, 5.0)

    def _features_from_hist(self, df) -> tuple[np.ndarray, np.ndarray]:
        """Build training features+labels from an OHLCV DataFrame."""
        import ta

        close = df["Close"] if "Close" in df.columns else df["close"]
        high = df["High"] if "High" in df.columns else df["high"]
        low = df["Low"] if "Low" in df.columns else df["low"]
        volume = df["Volume"] if "Volume" in df.columns else df["volume"]

        ema9 = ta.trend.EMAIndicator(close=close, window=9).ema_indicator()
        ema21 = ta.trend.EMAIndicator(close=close, window=21).ema_indicator()
        rsi14 = ta.momentum.RSIIndicator(close=close, window=14).rsi()
        macd_obj = ta.trend.MACD(close=close)
        macd_line = macd_obj.macd()
        macd_hist = macd_obj.macd_diff()
        bb_obj = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
        bb_upper = bb_obj.bollinger_hband()
        bb_lower = bb_obj.bollinger_lband()
        atr14 = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()

        # Label: 1 if next-day close > today * 1.002 (keep as bool so NaN at last row
        # propagates through to feat_df and dropna() removes it correctly)
        label = close.shift(-1) > close * 1.002

        # Features must match _features_from_snap (same 12, same order):
        #  1. EMA9/EMA21 ratio  2. price vs VWAP  3. RSI-14  4. MACD-hist/ATR
        #  5. BB position       6. ATR/price       7. StochRSI-K  8. StochRSI-D
        #  9. upper-band dist  10. lower-band dist 11. RSI-7  12. MACD/ATR
        rsi7 = ta.momentum.RSIIndicator(close=close, window=7).rsi()
        stoch = ta.momentum.StochRSIIndicator(close=close)
        stoch_k = stoch.stochrsi_k()
        stoch_d = stoch.stochrsi_d()
        vwap_proxy = close.rolling(20).mean()  # rolling-VWAP proxy from daily OHLCV
        bb_range = (bb_upper - bb_lower).replace(0, np.nan)
        import pandas as pd
        feat_df = pd.DataFrame({
            "f01_ema_ratio":        ema9 / ema21.replace(0, np.nan),
            "f02_price_vwap":       (close - vwap_proxy) / vwap_proxy.replace(0, np.nan),
            "f03_rsi14":            rsi14 / 100.0,
            "f04_macd_hist_atr":    macd_hist / atr14.replace(0, np.nan),
            "f05_bb_pos":           (close - bb_lower) / bb_range,
            "f06_atr_ratio":        atr14 / close.replace(0, np.nan),
            "f07_stoch_k":          stoch_k / 100.0,
            "f08_stoch_d":          stoch_d / 100.0,
            "f09_upper_dist":       (bb_upper - close) / atr14.replace(0, np.nan),
            "f10_lower_dist":       (close - bb_lower) / atr14.replace(0, np.nan),
            "f11_rsi7":             rsi7 / 100.0,
            "f12_macd_atr":         macd_line / atr14.replace(0, np.nan),
        })

        feat_df["label"] = label
        # dropna() now removes both NaN features AND the last-row NaN label (from shift(-1))
        feat_df = feat_df.replace([np.inf, -np.inf], np.nan).dropna()
        # No iloc[:-1] needed: dropna already removed the last row's NaN label

        X = feat_df.drop(columns=["label"]).values.astype(float)
        y = feat_df["label"].values.astype(int)
        return X, y

    # ── Public API ────────────────────────────────────────────────────

    def score(self, snap, action: str) -> float:
        """Return a 0.0–1.0 confidence score. Never raises."""
        try:
            if not settings.use_ml_signals:
                return 1.0

            symbol = snap.symbol
            with self._lock:
                if symbol not in self._loaded:
                    self.load(symbol)
                model = self._models.get(symbol)
            if model is None:
                return 0.5

            feats = self._features_from_snap(snap).reshape(1, -1)
            proba = model.predict_proba(feats)[0]  # [prob_class0, prob_class1]
            prob_up = float(proba[1])
            return prob_up if action.upper() == "BUY" else 1.0 - prob_up

        except Exception as exc:  # noqa: BLE001
            logger.debug("MLSignalScorer.score error for {}: {}", getattr(snap, "symbol", "?"), exc)
            return 0.5

    def train(self, symbol: str) -> bool:
        """Fetch history, fit Pipeline(StandardScaler + LogisticRegression), save to disk."""
        try:
            from market_data import YFinanceClient
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler

            df = YFinanceClient().historical(symbol, interval="1d", period="730d")
            if df.empty or len(df) < 60:
                logger.warning("MLSignalScorer.train: insufficient history for {}", symbol)
                return False

            X, y = self._features_from_hist(df)
            if len(X) < 30 or y.sum() < 5:
                logger.warning("MLSignalScorer.train: too few samples for {}", symbol)
                return False

            pipe = Pipeline([
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=500, C=1.0, solver="lbfgs")),
            ])
            pipe.fit(X, y)

            _MODEL_DIR.mkdir(parents=True, exist_ok=True)
            model_path = _MODEL_DIR / f"{symbol}.pkl"
            with open(model_path, "wb") as fh:
                pickle.dump(pipe, fh)

            self._models[symbol] = pipe
            self._loaded.add(symbol)
            logger.info("MLSignalScorer: trained and saved model for {}", symbol)
            return True

        except Exception as exc:
            logger.warning("MLSignalScorer.train failed for {}: {}", symbol, exc)
            return False

    def load(self, symbol: str) -> bool:
        """Try to load a saved model from disk. Returns True on success."""
        self._loaded.add(symbol)  # mark attempted regardless
        model_path = _MODEL_DIR / f"{symbol}.pkl"
        if not model_path.exists():
            return False
        try:
            with open(model_path, "rb") as fh:
                self._models[symbol] = pickle.load(fh)
            logger.debug("MLSignalScorer: loaded model for {}", symbol)
            return True
        except Exception as exc:
            logger.warning("MLSignalScorer.load failed for {}: {}", symbol, exc)
            return False

    def status(self) -> dict:
        """Return current scorer status."""
        return {
            "loaded_models": sorted(self._models.keys()),
            "use_ml_signals": settings.use_ml_signals,
            "min_confidence": settings.ml_signal_min_confidence,
        }


ml_signal_scorer = MLSignalScorer()
