"""Decision Scorer — MLP trained on (quant features) → predicted 5-day forward return.

Architecturally separate from ArticleNet (the text classifier). This model learns
from price outcomes, not text patterns, giving it signal that ArticleNet structurally
cannot learn. Trained on actual backtest BUY/SELL decisions with their real 5d outcomes.

sklearn is used when available; falls back to numpy least-squares if not installed.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

SCORER_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ml" / "decision_scorer.pkl"

SECTORS = ["tech", "energy", "financials", "healthcare", "commodities", "crypto", "other"]

SECTOR_MAP: dict[str, str] = {
    # Tech / semis
    "NVDA": "tech", "AMD": "tech", "MU": "tech", "INTC": "tech", "QCOM": "tech",
    "AAPL": "tech", "MSFT": "tech", "META": "tech", "GOOGL": "tech", "AMZN": "tech",
    "TSM": "tech", "ASML": "tech", "SMH": "tech", "SOXL": "tech", "TECL": "tech",
    "TQQQ": "tech", "QQQ": "tech", "XLK": "tech", "SHOP": "tech", "PLTR": "tech",
    "NVDU": "tech", "MSFU": "tech", "AMZU": "tech", "GOOGU": "tech", "METAU": "tech",
    "SOXS": "tech", "TECS": "tech", "FNGD": "tech", "FNGU": "tech",
    "SPY": "tech", "UPRO": "tech", "SPXL": "tech",  # broad index, treated as tech-correlated
    # Energy
    "XOM": "energy", "CVX": "energy", "XLE": "energy", "USO": "energy", "UNG": "energy",
    "BOIL": "energy", "UCO": "energy", "BP": "energy",
    # Financials
    "GS": "financials", "JPM": "financials", "BAC": "financials", "XLF": "financials",
    "FAS": "financials", "V": "financials", "MA": "financials", "UYG": "financials",
    "DPST": "financials", "HIBL": "financials",
    # Healthcare
    "LLY": "healthcare", "UNH": "healthcare", "NVO": "healthcare", "XLV": "healthcare",
    "CURE": "healthcare", "LABU": "healthcare",
    # Commodities / macro
    "GLD": "commodities", "SLV": "commodities", "TLT": "commodities", "GC=F": "commodities",
    "AGQ": "commodities", "RIO": "commodities", "BHP": "commodities",
    # Crypto
    "BTC-USD": "crypto", "COIN": "crypto", "MSTR": "crypto", "BITX": "crypto",
    "BITU": "crypto", "ETHU": "crypto", "CONL": "crypto",
}

N_FEATURES = 6 + len(SECTORS)  # 6 base + 7 sector one-hot = 13


class _LstsqScaler:
    """Pickle-safe stand-in for sklearn's StandardScaler, used in the numpy fallback."""

    def __init__(self, mean: np.ndarray, std: np.ndarray) -> None:
        self.mean_ = np.asarray(mean, dtype=np.float32)
        self.std_ = np.asarray(std, dtype=np.float32)

    def transform(self, Xin) -> np.ndarray:
        X = np.asarray(Xin, dtype=np.float32)
        return (X - self.mean_) / self.std_


class _LstsqModel:
    """Pickle-safe linear least-squares predictor used when sklearn is unavailable."""

    def __init__(self, weights: np.ndarray) -> None:
        self.w_ = np.asarray(weights, dtype=np.float32)

    def predict(self, Xin) -> np.ndarray:
        X = np.asarray(Xin, dtype=np.float32)
        Xa = np.hstack([X, np.ones((len(X), 1), dtype=np.float32)])
        return Xa @ self.w_


def _to_float(v, default: float) -> float:
    # bool is a subclass of int — exclude it so True/False don't become 1.0/0.0.
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)) and v == v:  # excludes NaN
        return float(v)
    return default


def build_features(
    ml_score: float,
    rsi: float | None,
    macd: float | None,
    mom5: float | None,
    mom20: float | None,
    regime_mult: float,
    ticker: str,
) -> list[float]:
    """Build a fixed-length feature vector for one decision."""
    rsi_v = _to_float(rsi, 50.0)
    macd_v = _to_float(macd, 0.0)
    mom5_v = _to_float(mom5, 0.0)
    mom20_v = _to_float(mom20, 0.0)
    sector = SECTOR_MAP.get(ticker, "other")
    sector_oh = [1.0 if s == sector else 0.0 for s in SECTORS]
    return [_to_float(ml_score, 0.0), rsi_v, macd_v, mom5_v, mom20_v,
            _to_float(regime_mult, 1.0)] + sector_oh


class DecisionScorer:
    """Lightweight MLP: (quant_features) → predicted 5-day forward return (%)."""

    def __init__(self) -> None:
        self._model = None
        self._scaler = None
        self._trained = False
        self._n_train = 0
        if SCORER_PATH.exists():
            self._load()

    def _load(self) -> None:
        try:
            with SCORER_PATH.open("rb") as f:
                state = pickle.load(f)
            self._model = state["model"]
            self._scaler = state.get("scaler")
            self._n_train = int(state.get("n_train", 0))
            self._trained = True
            print(f"[decision_scorer] loaded n={self._n_train} from {SCORER_PATH}")
        except Exception as e:
            print(f"[decision_scorer] load failed: {e}")

    def predict(
        self,
        ml_score: float,
        rsi: float | None,
        macd: float | None,
        mom5: float | None,
        mom20: float | None,
        regime_mult: float,
        ticker: str,
    ) -> float:
        """Return predicted 5d forward return (%). Returns 0.0 if not trained."""
        if not self._trained or self._model is None:
            return 0.0
        try:
            X = np.array(
                [build_features(ml_score, rsi, macd, mom5, mom20, regime_mult, ticker)],
                dtype=np.float32,
            )
            if self._scaler is not None:
                X = self._scaler.transform(X)
            return float(self._model.predict(X)[0])
        except Exception:
            return 0.0

    @property
    def is_trained(self) -> bool:
        return self._trained

    @property
    def n_train(self) -> int:
        return self._n_train


def train_scorer(records: list[dict]) -> dict:
    """Train DecisionScorer on outcome records.

    Each record must have: ml_score, rsi, macd, mom5, mom20, regime_mult, ticker,
    forward_return_5d. Optional: action (BUY/SELL — SELL flips target sign so the
    model learns "goodness of THIS action"), return_pct (overall backtest run
    quality, used to weight samples). Returns stats dict.
    """
    if len(records) < 30:
        return {"status": "insufficient_data", "n": len(records)}

    X_raw, y, weights = [], [], []
    for r in records:
        X_raw.append(build_features(
            float(r.get("ml_score", 0.0)),
            r.get("rsi"),
            r.get("macd"),
            r.get("mom5"),
            r.get("mom20"),
            float(r.get("regime_mult", 1.0)),
            str(r.get("ticker", "")),
        ))
        fr = float(r.get("forward_return_5d", 0.0))
        action = str(r.get("action", "BUY")).upper()
        # SELL: negative forward returns were the *correct* outcome, so flip
        # sign — the model then learns one consistent meaning of "good".
        y.append(-fr if action == "SELL" else fr)
        # Sample weight from overall run quality:
        # +200% run → 2.0×, 0% → 1.0×, -100%+ → 0.5×.
        rp = float(r.get("return_pct", 0.0))
        weights.append(max(0.5, min(2.0, 1.0 + rp / 200.0)))

    X = np.array(X_raw, dtype=np.float32)
    y = np.array(y, dtype=np.float32)
    weights = np.array(weights, dtype=np.float32)

    try:
        from sklearn.neural_network import MLPRegressor
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split

        scaler = StandardScaler()
        X_s = scaler.fit_transform(X)
        X_tr, X_v, y_tr, y_v, w_tr, _ = train_test_split(
            X_s, y, weights, test_size=0.2, random_state=42
        )
        # MLPRegressor.fit doesn't accept sample_weight — emulate by deterministic
        # oversampling: weight 0.5→1× replica, 1.0→2×, 1.5→3×, 2.0→4×. Done
        # only on the training fold so val_rmse stays clean.
        rep = np.maximum(1, np.round(w_tr * 2).astype(int))
        X_tr_w = np.repeat(X_tr, rep, axis=0)
        y_tr_w = np.repeat(y_tr, rep, axis=0)

        model = MLPRegressor(
            hidden_layer_sizes=(64, 32, 16),
            activation="relu",
            max_iter=500,
            random_state=42,
            early_stopping=True,
            validation_fraction=0.15,
        )
        model.fit(X_tr_w, y_tr_w)
        y_pred = model.predict(X_v)
        val_rmse = float(np.sqrt(np.mean((y_pred - y_v) ** 2)))

    except ImportError:
        # Numpy weighted least-squares linear fallback when sklearn not installed.
        # Uses module-level _LstsqScaler / _LstsqModel so the resulting pickle
        # can be loaded later — closures cannot be pickled by name.
        scaler_mean = X.mean(axis=0)
        scaler_std = X.std(axis=0) + 1e-8
        X_s = (X - scaler_mean) / scaler_std
        X_aug = np.hstack([X_s, np.ones((len(X_s), 1), dtype=np.float32)])
        sw = np.sqrt(weights).astype(np.float32).reshape(-1, 1)
        w, _, _, _ = np.linalg.lstsq(X_aug * sw, y * sw.ravel(), rcond=None)
        scaler = _LstsqScaler(scaler_mean, scaler_std)
        model = _LstsqModel(w)
        val_rmse = float("nan")

    SCORER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SCORER_PATH.open("wb") as f:
        pickle.dump({"model": model, "scaler": scaler, "n_train": len(records)}, f)

    return {"status": "ok", "n": len(records), "val_rmse": val_rmse}
