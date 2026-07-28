"""The estimators the factor gates need (DESIGN.md §5.7.4, Appendix A).

Pure Python, no numpy — the whole quant stack stays dependency-free and offline. These
are ordinary reviewed functions, **not** model calls: a t-statistic computed by an LLM is
strictly worse than one computed by arithmetic (DESIGN.md §5.2).

What each one is for:

* ``newey_west_tstat`` — daily strategy returns are autocorrelated, so a naive t-stat
  overstates significance. HAC standard errors correct for it.
* ``bootstrap_pvalue`` — strategy returns are emphatically not normal, so significance
  is estimated by resampling rather than assumed from a distribution.
* ``degradation`` — in-sample versus out-of-sample. The *comparison* is the tell; a large
  drop is the signature of a fitted parameter.
* ``volatility_regimes`` — a crude, transparent segmentation. See the note on its limits.
* ``ols`` — attribution: what is left after known factors are removed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


# --------------------------------------------------------------------------- #
# Descriptive helpers
# --------------------------------------------------------------------------- #
def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def variance(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def stdev(xs: Sequence[float]) -> float:
    return math.sqrt(variance(xs))


def autocovariance(xs: Sequence[float], lag: int) -> float:
    n = len(xs)
    if lag >= n:
        return 0.0
    m = mean(xs)
    return sum((xs[i] - m) * (xs[i - lag] - m) for i in range(lag, n)) / n


# --------------------------------------------------------------------------- #
# Newey–West
# --------------------------------------------------------------------------- #
def newey_west_tstat(returns: Sequence[float], lags: Optional[int] = None) -> float:
    """t-statistic of the mean, with HAC (Newey–West) standard errors.

    With positively autocorrelated returns the HAC variance is *larger* than the naive
    one, so this t-statistic is correspondingly smaller — which is the point. Reporting
    the naive figure on autocorrelated data overstates how sure you should be.
    """
    n = len(returns)
    if n < 3:
        return 0.0
    if lags is None:
        # Newey–West's rule of thumb for the truncation lag.
        lags = max(1, int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))))
    lags = min(lags, n - 1)

    long_run = autocovariance(returns, 0)
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)          # Bartlett kernel
        long_run += 2.0 * weight * autocovariance(returns, lag)
    if long_run <= 0:
        return 0.0
    standard_error = math.sqrt(long_run / n)
    return mean(returns) / standard_error if standard_error > 0 else 0.0


def naive_tstat(returns: Sequence[float]) -> float:
    """The uncorrected figure, kept so the difference can be shown rather than asserted."""
    n = len(returns)
    if n < 2:
        return 0.0
    se = stdev(returns) / math.sqrt(n)
    return mean(returns) / se if se > 0 else 0.0


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def bootstrap_pvalue(returns: Sequence[float], iterations: int = 10_000,
                     seed: int = 12345, block: int = 1) -> float:
    """Two-sided p-value for "the mean is zero", by resampling.

    Uses a deterministic LCG so a research result is reproducible — a p-value that
    changes between runs is not evidence. `block > 1` gives a moving-block bootstrap,
    which preserves autocorrelation the i.i.d. version would destroy.
    """
    n = len(returns)
    if n < 3:
        return 1.0
    observed = abs(mean(returns))
    centred = [r - mean(returns) for r in returns]     # resample under the null

    state = seed or 1
    exceed = 0
    blocks = max(1, block)
    draws = max(1, n // blocks)

    for _ in range(iterations):
        total = 0.0
        count = 0
        for _ in range(draws):
            state = (1103515245 * state + 12345) % (1 << 31)
            start = state % n
            for k in range(blocks):
                total += centred[(start + k) % n]
                count += 1
        if abs(total / count) >= observed:
            exceed += 1
    return (exceed + 1) / (iterations + 1)             # add-one keeps p > 0


# --------------------------------------------------------------------------- #
# In-sample vs out-of-sample
# --------------------------------------------------------------------------- #
def degradation(in_sample: float, out_of_sample: float) -> float:
    """Fractional drop from in-sample to out-of-sample.

    Returns 1.0 (total) when the in-sample figure is non-positive, because "degradation"
    is meaningless if there was nothing to degrade from — and a strategy with no
    in-sample edge should not pass by arithmetic accident.
    """
    if in_sample <= 0:
        return 1.0
    return max(0.0, (in_sample - out_of_sample) / abs(in_sample))


# --------------------------------------------------------------------------- #
# Regimes
# --------------------------------------------------------------------------- #
@dataclass
class Regime:
    name: str
    indices: list[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.indices)


def volatility_regimes(returns: Sequence[float], window: int = 60,
                       labels: Sequence[str] = ("calm", "normal", "stressed"),
                       ) -> list[Regime]:
    """Segment a return series into regimes by trailing realized volatility.

    **This is the crude version, deliberately** (DESIGN.md §12 #13). A hidden Markov
    model is the literature's answer; terciles of trailing volatility are transparent,
    have no fitting step of their own to overfit, and are honest about being a proxy.
    Anything concluded from them should say "volatility tercile", not "regime" as if a
    latent state had been identified.
    """
    n = len(returns)
    if n < window * 3:
        return [Regime(name="all", indices=list(range(n)))]

    trailing: list[tuple[float, int]] = []
    for i in range(window, n):
        w = returns[i - window:i]
        trailing.append((stdev(w), i))

    ordered = sorted(trailing, key=lambda x: x[0])
    third = len(ordered) // 3
    buckets = [ordered[:third], ordered[third:2 * third], ordered[2 * third:]]
    return [Regime(name=label, indices=sorted(i for _v, i in bucket))
            for label, bucket in zip(labels, buckets)]


def regime_means(returns: Sequence[float], regimes: Sequence[Regime]) -> dict[str, float]:
    return {r.name: mean([returns[i] for i in r.indices]) if r.indices else 0.0
            for r in regimes}


# --------------------------------------------------------------------------- #
# OLS, for attribution
# --------------------------------------------------------------------------- #
@dataclass
class Regression:
    alpha: float = 0.0
    alpha_tstat: float = 0.0
    betas: list[float] = field(default_factory=list)
    r_squared: float = 0.0
    n: int = 0


def ols(y: Sequence[float], xs: Sequence[Sequence[float]]) -> Regression:
    """Least squares with an intercept, solved by Gaussian elimination.

    The intercept is the residual alpha: what the regressors do not explain. Its
    t-statistic uses HAC standard errors on the residuals, for the same reason the
    factor's own t-statistic does.
    """
    n = len(y)
    k = len(xs)
    if n == 0 or any(len(x) != n for x in xs):
        return Regression(n=n)

    # Design matrix with a leading column of ones.
    columns = [[1.0] * n] + [list(x) for x in xs]
    p = k + 1

    # Normal equations: (X'X) b = X'y. Gauss–Jordan on [X'X | I | X'y] gives both the
    # coefficients and (X'X)⁻¹ — the inverse is needed for the alpha standard error.
    xtx = [[sum(columns[i][t] * columns[j][t] for t in range(n)) for j in range(p)]
           for i in range(p)]
    xty = [sum(columns[i][t] * y[t] for t in range(n)) for i in range(p)]

    aug = [xtx[i][:] + [1.0 if i == j else 0.0 for j in range(p)] + [xty[i]]
           for i in range(p)]
    width = 2 * p + 1
    for col in range(p):
        pivot = max(range(col, p), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return Regression(n=n)                 # singular: collinear regressors
        aug[col], aug[pivot] = aug[pivot], aug[col]
        divisor = aug[col][col]
        for c in range(width):
            aug[col][c] /= divisor
        for r in range(p):
            if r == col or abs(aug[r][col]) < 1e-15:
                continue
            factor = aug[r][col]
            for c in range(width):
                aug[r][c] -= factor * aug[col][c]

    beta = [aug[i][width - 1] for i in range(p)]
    xtx_inv_00 = aug[0][p]                          # (X'X)⁻¹[0][0], for the intercept

    fitted = [sum(beta[i] * columns[i][t] for i in range(p)) for t in range(n)]
    residuals = [y[t] - fitted[t] for t in range(n)]
    ss_res = sum(r * r for r in residuals)
    ss_tot = sum((v - mean(y)) ** 2 for v in y)

    # SE(alpha) = sqrt(σ² · (X'X)⁻¹[0][0]). Residuals sum to zero by construction, so a
    # t-statistic taken on the residual *mean* would always be ~0 — the standard error
    # has to come from the coefficient covariance instead.
    alpha_tstat = 0.0
    if n > p and ss_res > 0 and xtx_inv_00 > 0:
        sigma2 = ss_res / (n - p)
        se_alpha = math.sqrt(sigma2 * xtx_inv_00)
        if se_alpha > 0:
            alpha_tstat = beta[0] / se_alpha

    return Regression(
        alpha=beta[0],
        alpha_tstat=alpha_tstat,
        betas=beta[1:],
        r_squared=(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0,
        n=n,
    )
