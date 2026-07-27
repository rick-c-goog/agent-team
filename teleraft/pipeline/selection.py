"""The selection gate: correcting significance for how hard you searched (§5.7.4).

A pipeline that runs continuously tests thousands of items. At a 5% threshold, **1 in 20
pure-noise items passes by construction** — 10,000 trials manufacture ~500 "discoveries"
from nothing. Newey–West and bootstrap correct a *single* test and do not help here.

Because the trial ledger records every test ever run, the correction is available to this
platform and structurally unavailable to a stateless swarm: **the trial count is an input
to significance, not just an audit trail.**

Both corrections are reported (DESIGN.md §12 #11) because they answer different
questions, and reporting one invites the reader to assume the other:

* **Benjamini–Hochberg FDR** — "of the things I called significant, what share are
  false?" Standard, distribution-free, operates on the batch of p-values.
* **Deflated statistic** — "how impressive is the *best* result, given I took N shots?"
  Direct, uses the trial count, in the spirit of the deflated Sharpe ratio.

The ledger uses a **rolling window** (§12 #12): counting since inception makes the
correction monotonically punishing and eventually useless, while a window keeps it honest
about *current* search intensity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_WINDOW_DAYS = 90.0


@dataclass
class SelectionReport:
    trials: int = 0
    window_days: float = DEFAULT_WINDOW_DAYS
    epoch: str = ""
    alpha: float = 0.05
    expected_false_positives: float = 0.0
    fdr_threshold: float = 0.0
    survivors_fdr: list[str] = field(default_factory=list)
    best_subject: str = ""
    best_statistic: Optional[float] = None
    deflated_statistic: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.trials} trials in the last {self.window_days:.0f}d",
                f"~{self.expected_false_positives:.1f} false positives expected at "
                f"α={self.alpha}"]
        if self.survivors_fdr:
            bits.append(f"{len(self.survivors_fdr)} survive FDR")
        else:
            bits.append("none survive FDR")
        if self.deflated_statistic is not None:
            bits.append(f"best {self.best_statistic:.2f} → deflated "
                        f"{self.deflated_statistic:.2f}")
        return "; ".join(bits)


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> tuple[float, list[int]]:
    """Return (threshold, indices of rejected nulls) controlling FDR at `alpha`."""
    if not p_values:
        return 0.0, []
    ordered = sorted(range(len(p_values)), key=lambda i: p_values[i])
    n = len(p_values)
    threshold = 0.0
    cutoff_rank = 0
    for rank, idx in enumerate(ordered, start=1):
        if p_values[idx] <= alpha * rank / n:
            threshold = alpha * rank / n
            cutoff_rank = rank
    if cutoff_rank == 0:
        return 0.0, []
    return threshold, [ordered[i] for i in range(cutoff_rank)]


def expected_max_of_n(n: int) -> float:
    """Expected maximum of n standard normals — the bar pure luck clears.

    With n independent tries, the best t-statistic you expect from noise alone grows
    like sqrt(2 ln n). Comparing the observed best against this is the intuition behind
    the deflated Sharpe ratio.
    """
    if n <= 1:
        return 0.0
    # Standard approximation, accurate enough for a reporting threshold.
    euler = 0.5772156649
    ln_n = math.log(n)
    return ((1 - euler) * _inv_norm_cdf(1 - 1.0 / n)
            + euler * _inv_norm_cdf(1 - 1.0 / (n * math.e)))


def deflate(best_statistic: float, trials: int) -> float:
    """Subtract the bar that pure search would clear, leaving what search cannot explain."""
    return best_statistic - expected_max_of_n(max(trials, 1))


def _inv_norm_cdf(p: float) -> float:
    """Acklam's inverse normal CDF approximation — no scipy dependency."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def assess(storage, pipeline: str, *, alpha: float = 0.05,
           window_days: float = DEFAULT_WINDOW_DAYS,
           epoch: Optional[str] = None) -> SelectionReport:
    """Apply both corrections over the rolling trial window."""
    import time

    since = time.time() - window_days * 86400
    rows = storage.trials(pipeline, since=since, epoch=epoch)
    report = SelectionReport(trials=len(rows), window_days=window_days,
                             epoch=epoch or "(all)", alpha=alpha)
    if not rows:
        report.notes.append("no trials recorded in the window — nothing to correct")
        return report

    report.expected_false_positives = round(len(rows) * alpha, 4)

    p_values = [r["p_value"] for r in rows if r["p_value"] is not None]
    if p_values:
        subjects = [r["subject"] for r in rows if r["p_value"] is not None]
        threshold, rejected = benjamini_hochberg(p_values, alpha)
        report.fdr_threshold = round(threshold, 6)
        report.survivors_fdr = [subjects[i] for i in rejected]
    else:
        report.notes.append("no p-values recorded; FDR not applicable")

    stats = [(r["statistic"], r["subject"]) for r in rows if r["statistic"] is not None]
    if stats:
        best, subject = max(stats, key=lambda x: x[0])
        report.best_subject = subject
        report.best_statistic = round(best, 4)
        report.deflated_statistic = round(deflate(best, len(rows)), 4)
        if report.deflated_statistic <= 0:
            report.notes.append(
                f"the best result ({best:.2f}) does not clear what {len(rows)} trials "
                "of pure search would produce — treat it as a null result"
            )
    else:
        report.notes.append("no statistics recorded; deflation not applicable")

    return report
