# Robustness sweep

Cohort: 200 customers, 700 invoices, 90-day horizon (smaller than the headline benchmark, for turnaround time -- 8 rows each regenerate a cohort, exploration log and hazard model from scratch). `learned` vs `razorpay_default`, both evaluated on held-out cohort B only, same world.yaml perturbation on both sides.

| dimension | severity | razorpay_default recovered | learned recovered | lift |
|---|---|---|---|---|
| payday_shifted | 30% | ₹40,568.48 | ₹120,276.26 | +196.5% |
| payday_shifted | 50% | ₹43,187.00 | ₹123,768.41 | +186.6% |
| downtime_doubled | 30% | ₹51,176.72 | ₹132,598.61 | +159.1% |
| downtime_doubled | 50% | ₹49,724.48 | ₹169,849.59 | +241.6% |
| hard_decline_tripled | 30% | ₹36,464.30 | ₹134,501.66 | +268.9% |
| hard_decline_tripled | 50% | ₹36,119.42 | ₹126,973.63 | +251.5% |
| engagement_halved | 30% | ₹28,036.15 | ₹117,948.56 | +320.7% |
| engagement_halved | 50% | ₹43,774.46 | ₹129,031.27 | +194.8% |
