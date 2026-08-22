# Benchmark report

| arm | recovery rate | recovered | attempts/recovery | mean days | messages | false dunning | violations |
|---|---|---|---|---|---|---|---|
| no_retry | 0.0% | ₹0.00 | 0.00 | — | 0 | 0.0% | 0 |
| razorpay_default | 17.9% | ₹236,427.79 | 4.91 | 1.0 | 897 | 50.1% | 0 |
| static_1_3_7 | 24.6% | ₹329,955.97 | 7.37 | 2.0 | 1830 | 32.8% | 0 |
| dunning_only | 18.4% | ₹239,660.90 | 2.63 | 0.0 | 966 | 48.4% | 0 |
| heuristic | 46.2% | ₹668,118.66 | 4.08 | 2.3 | 2000 | 49.1% | 0 |

## Payday inference validation

HeuristicPolicy scheduled 1847 SILENT_RETRY debits across the cohort, each timed from a per-customer posterior inferred from observed attempt outcomes only -- never from the simulator's hidden payday_dom. Reported honestly rather than rounded to match BUILD_DOC.md's own worked example:

- 52.5% landed in days 1-10 (vs. 32.3% under a uniform schedule) -- a real, population-level concentration in the early month, where the underlying salary-landing prior (world.yaml: 60% near the 1st) says money actually arrives.
- 9.0% landed in the 25th-31st (vs. 22.6% uniform) -- well below baseline, so the discouraged late-month range Razorpay's guidance calls out is genuinely avoided, not just under-sampled.
- 18.2% landed specifically in days 3-7 -- close to the 16.1% uniform baseline, not a strong signal on its own. BUILD_DOC.md §4.2's own heuristic rule ("next inferred payday + 1 day") only shifts the debit one day past a payday the prior places mostly on the 1st; matching Razorpay's literal 3rd-7th recommendation would need a larger buffer than the doc's own worked rule specifies. Flagged rather than tuned to fit -- see policy/payday.py and policy/heuristic.py for the full account.

See benchmarks/payday_inference.png and benchmarks/debit_day_histogram.png.
