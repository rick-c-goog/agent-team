"""The eleven-node factor graph, end to end. Run: python -m teleraft.factor_demo

Nodes 1–7 construct factors in parallel; 8 validates; 9 audits regimes; 10 combines the
survivors; 11 asks whether anything is left after known factors are removed.

Runs offline on deterministic synthetic prices. What you see is the machinery working —
not a claim about markets.
"""

from __future__ import annotations

from .pipeline import PipelineEngine
from .pipeline.selection import assess
from .quant.data import SyntheticLoader
from .quant.factor_pipeline import FactorGateConfig, build_factor_pipeline
from .quant.factors import FACTORS, construct
from .storage import Storage

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF",
            "GGG", "HHH", "III", "JJJ", "KKK", "LLL"]
BENCHMARK_UNIVERSE = ["ZZ1", "ZZ2", "ZZ3", "ZZ4", "ZZ5", "ZZ6", "ZZ7", "ZZ8", "ZZ9"]
RULE = "-" * 78
MARK = {"passed": "✅", "killed": "❌", "blocked": "⛔"}


def main() -> None:
    loader = SyntheticLoader()
    universe = {s: loader.load(s) for s in UNIVERSE}

    print("=" * 78)
    print("The eleven-node factor graph")
    print("=" * 78)
    print(f"\nUniverse: {len(universe)} symbols · synthetic prices (NOT real market data)")

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
    bench_built, _ = construct({s: loader.load(s) for s in BENCHMARK_UNIVERSE})
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
