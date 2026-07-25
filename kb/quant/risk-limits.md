# Risk limits — quant desk (research thresholds)

These are the acceptance thresholds the Tester enforces. They are deliberately strict:
most ideas should fail here, and that is the system working.

| Limit | Threshold |
|---|---|
| Minimum out-of-sample Sharpe | 0.5 |
| Maximum out-of-sample drawdown | 35% |
| Minimum out-of-sample bars | 200 |
| Maximum gross exposure | 100% (long-only by default, no leverage) |

## Rules
- A breach of any limit rejects the research note, regardless of headline return.
- Drawdown is measured on the out-of-sample equity curve, peak to trough.
- Leverage is not available to the research desk. Any proposal implying it escalates to
  a human.
- A strategy whose entire return comes from a single regime or a single month is flagged
  by the macro seat even if it passes the numeric limits.
