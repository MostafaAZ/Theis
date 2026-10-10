"""Statistical forecasting (FR-43, FR-44).

Seasonal exponential smoothing (additive Holt-Winters, 12-month season) where
24 months of history exist, a 6-month moving average otherwise. No machine
learning, as Section 5.2 requires.
"""
from itertools import product as grid

SEASON = 12
MIN_SEASONAL_HISTORY = 24
MIN_HISTORY = 6


def moving_average(series, horizon, window=6):
    window = min(window, len(series))
    level = sum(series[-window:]) / window if window else 0.0
    return [max(0.0, level)] * horizon


def _holt_winters(series, alpha, beta, gamma, horizon):
    m = SEASON
    level = sum(series[:m]) / m
    trend = (sum(series[m:2 * m]) - sum(series[:m])) / (m * m)
    seasonal = [series[i] - level for i in range(m)]
    sse = 0.0
    for t in range(len(series)):
        s = seasonal[t % m]
        if t >= m:
            fitted = level + trend + s
            sse += (series[t] - fitted) ** 2
        prev_level = level
        level = alpha * (series[t] - s) + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        seasonal[t % m] = gamma * (series[t] - level) + (1 - gamma) * s
    n = len(series)
    fc = [max(0.0, level + (h + 1) * trend + seasonal[(n + h) % m]) for h in range(horizon)]
    return fc, sse


def holt_winters(series, horizon):
    """Additive Holt-Winters with a small grid search on the smoothing
    parameters (minimum in-sample one-step squared error)."""
    best = None
    for alpha, beta, gamma in grid((0.2, 0.4, 0.6), (0.0, 0.05, 0.15), (0.1, 0.3)):
        fc, sse = _holt_winters(series, alpha, beta, gamma, horizon)
        if best is None or sse < best[1]:
            best = (fc, sse)
    return best[0]


def choose_method(n_months):
    if n_months >= MIN_SEASONAL_HISTORY:
        return "seasonal_es"
    if n_months >= MIN_HISTORY:
        return "moving_avg"
    return "manual"


def forecast(series, horizon):
    method = choose_method(len(series))
    if method == "seasonal_es":
        return method, holt_winters(series, horizon)
    if method == "moving_avg":
        return method, moving_average(series, horizon)
    return method, [0.0] * horizon


def mape(actual, predicted):
    pairs = [(a, p) for a, p in zip(actual, predicted) if a > 0]
    if not pairs:
        return None
    return 100.0 * sum(abs(a - p) / a for a, p in pairs) / len(pairs)


def backtest_mape(series, holdout=6):
    """Accuracy of the method on the most recent months it did not see."""
    if len(series) < MIN_HISTORY + holdout:
        return None
    train, test = series[:-holdout], series[-holdout:]
    _method, fc = forecast(train, holdout)
    return mape(test, fc)
