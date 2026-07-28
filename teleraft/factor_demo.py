"""The eleven-node factor graph, end to end. Run: python -m teleraft.factor_demo

Nodes 1–7 construct factors in parallel; 8 validates; 9 audits regimes; 10 combines the
survivors; 11 asks whether anything is left after known factors are removed.

Defaults to deterministic synthetic prices, offline: what you see is the machinery
working, not a claim about markets. Pass ``--source yfinance`` to run the identical
graph over real adjusted closes — the gates do not change, only the data does, and a
real universe is where most factors start dying for real reasons.
"""

from __future__ import annotations

import argparse
import logging

from .pipeline import PipelineEngine
from .pipeline.selection import assess
from .quant.data import SyntheticLoader
from .quant.factor_pipeline import FactorGateConfig, build_factor_pipeline
from .quant.factors import FACTORS, construct
from .storage import Storage

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF",
            "GGG", "HHH", "III", "JJJ", "KKK", "LLL"]
BENCHMARK_UNIVERSE = ["ZZ1", "ZZ2", "ZZ3", "ZZ4", "ZZ5", "ZZ6", "ZZ7", "ZZ8", "ZZ9"]

# Real tickers for --source yfinance. Large, liquid, long-listed: the point is to
# exercise the graph on real data, not to propose this as a research universe. It is
# survivorship-biased by construction — every name here is one that survived.
REAL_UNIVERSE = ["AAPL", "MSFT", "JNJ", "XOM", "JPM", "PG",
                 "KO", "WMT", "CVX", "MRK", "PEP", "CSCO"]
REAL_BENCHMARK = ["IBM", "INTC", "T", "VZ", "GE", "F", "MMM", "CAT", "BA"]
RULE = "-" * 78
MARK = {"passed": "✅", "killed": "❌", "blocked": "⛔"}


def _load(source: str) -> tuple[dict, dict, str]:
    """Return (universe, benchmark universe, provenance line).

    Node 11 regresses against a benchmark built from *different* symbols — an
    overlapping benchmark would explain the alpha with itself.
    """
    if source == "synthetic":
        loader = SyntheticLoader()
        return ({s: loader.load(s) for s in UNIVERSE},
                {s: loader.load(s) for s in BENCHMARK_UNIVERSE},
                "synthetic prices (NOT real market data)")

    from .quant.providers import build_loader

    loader = build_loader(source)
    universe, failed = loader.load_universe(REAL_UNIVERSE)
    benchmark, _ = loader.load_universe(REAL_BENCHMARK)
    if failed:
        print("  skipped " + ", ".join(f"{k} ({v[:40]}…)" for k, v in failed.items()))
    if len(universe) < 6:
        raise SystemExit(
            f"only {len(universe)} symbols loaded — the cross-sectional factors need at "
            "least 6. Check your network, or run with --source synthetic.")
    return (universe, benchmark,
            f"{source} adjusted closes · REAL prices · survivorship-biased universe")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="synthetic",
                        choices=["synthetic", "yfinance", "csv"],
                        help="price source (default: synthetic, offline)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="  ⚠ %(message)s")

    universe, benchmark_universe, provenance = _load(args.source)

    print("=" * 78)
    print("The eleven-node factor graph")
    print("=" * 78)
    print(f"\nUniverse: {len(universe)} symbols · {provenance}")

    print("\n" + RULE)
    print("NODES 1–7 · factor construction (one parameterised producer)\n")
    for spec in FACTORS:
        need = ", ".join(spec.requires) or "prices only"
        flag = "buildable" if spec.computable_from_prices else f"NEEDS {need}"
        print(f"  node {spec.node}  {spec.name:5} {spec.description:52} {flag}")

    built, blocked = construct(universe)
    print(f"\n  built: {', '.join(sorted(built))}")
    print(f"  blocked: {', '.join(sorted(blocked))} — four of seven need data a price "
          "feed cannot give")

    # ---------------------------------------------------------------- #
    storage = Storage(":memory:")
    engine = PipelineEngine(storage)

    print("\n" + RULE)
    print("RUN 1 · the desk's real bar, no benchmark configured\n")
    result = engine.run(build_factor_pipeline(universe))
    for item in result.items:
        print(f"  {MARK.get(item.status, '•')} {item.subject:5} {item.status:8} "
              f"{item.kill_reason[:88]}")
    print(f"\n  {result.summary()}")
    for reason in result.reasons:
        print(f"  → {reason[:150]}")

    # ---------------------------------------------------------------- #
    print("\n" + RULE)
    print("RUN 2 · with an INDEPENDENT benchmark, gates relaxed to reach node 11\n")
    bench_built, _ = construct(benchmark_universe)
    benchmark = {f"BM_{k}": v.returns for k, v in bench_built.items()}
    print(f"  benchmark series (built from different symbols): {', '.join(benchmark)}")

    lenient = FactorGateConfig(min_tstat=0.0, max_p_value=1.0, max_degradation=99.0,
                               min_regimes_working=1, bootstrap_iterations=400)
    result2 = engine.run(build_factor_pipeline(universe, benchmark=benchmark,
                                               config=lenient))
    if result2.aggregate:
        agg = result2.aggregate
        print(f"\n  node 10 · risk-parity weights: {agg['weights']}")
        for key, value in agg["neutrality"].items():
            print(f"           {key:7}: {value}")
        print(f"\n  node 11 · residual alpha {agg['residual_alpha']:+.6f} "
              f"(t = {agg['alpha_tstat']:.2f}), R² {agg['r_squared']:.3f}")
        print(f"           loadings: {agg['loadings']}")
    for reason in result2.reasons:
        print(f"  → {reason[:150]}")

    # ---------------------------------------------------------------- #
    print("\n" + RULE)
    print("RUN 3 · a benchmark built from the SAME factors — must be refused\n")
    result3 = engine.run(build_factor_pipeline(
        universe, benchmark={k: v.returns for k, v in built.items()}, config=lenient))
    for reason in result3.reasons:
        print(f"  ⛔ {reason[:170]}")
    print("\n  Regressing a portfolio on the factors it was built from returns alpha ≈ 0")
    print("  as arithmetic — the same answer for a brilliant portfolio and a worthless")
    print("  one. The gate refuses rather than reporting that zero.")

    # ---------------------------------------------------------------- #
    print("\n" + RULE)
    print("THE SELECTION GATE · how hard did we search?\n")
    report = assess(storage, "eleven-node-factor-graph")
    print(f"  {report.summary()}")
    for note in report.notes:
        print(f"  → {note}")

    print("\n" + RULE)
    print("Research output only. Not investment advice. This system places no orders.")
    storage.close()


if __name__ == "__main__":
    main()
