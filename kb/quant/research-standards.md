# Research standards — quant desk

## Evidence
- Every number in a research note must come from a backtest artifact. No number is
  quoted from memory or from prose.
- In-sample results are a hypothesis, not a finding. Only out-of-sample results support
  a claim.
- State the sample: symbol, period, number of bars, data source.

## Method
- Signals are declared as specs (type + params), never as free-form code.
- Signals computed at bar *t* are traded at *t+1*. Any result without this lag is void.
- Costs are charged on turnover. A strategy that flips daily must pay for it.
- Parameter search happens on the in-sample window only. Report how many parameter sets
  were searched — it is the denominator for any claim of significance.

## Reporting
- Report CAGR, Sharpe, max drawdown, turnover, and the buy & hold benchmark together.
  A return without its drawdown is not a result.
- Say what would falsify the finding.

## Boundaries
- The desk produces research. It does not place orders, size positions, or manage money.
- Anything touching live trading or real capital goes to a human before anything else
  happens.
