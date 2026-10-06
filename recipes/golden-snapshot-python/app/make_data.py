"""Generates the dataset the service trains on: 200,000 rows of synthetic customer churn data."""
import numpy as np
import pandas as pd

rng = np.random.default_rng(7)
n = 200_000
df = pd.DataFrame({
    "tenure_months": rng.integers(1, 72, n),
    "monthly_spend": rng.normal(1200, 400, n).round(2),
    "support_tickets": rng.poisson(1.5, n),
    "logins_per_week": rng.gamma(2.0, 2.0, n).round(1),
})
risk = 0.04 * df.support_tickets - 0.03 * df.tenure_months - 0.2 * df.logins_per_week + rng.normal(0, 1, n)
df["churned"] = (risk > risk.quantile(0.8)).astype(int)
df.to_csv("data.csv", index=False)
print(len(df))
