# Reconciliation task

Use `orders.csv` and `rates.json` to build a candidate containing orders that
meet both published rules:

1. `units` is odd.
2. The discounted subtotal is greater than 70.00.

For each selected order, compute:

`line_total = units * unit_price * (1 - discount) + regional_shipping`

Round monetary values to two decimals. The candidate JSON must contain
`selected_ids` in CSV order, `grand_total`, and `signature`. The signature is
the first 12 lowercase hex characters of SHA-256 over the comma-joined selected
IDs.

Write the candidate to `result.json`, then run:

```bash
python3 verify.py result.json
```

The verifier applies one additional integrity constraint and returns a
structured hint when the first candidate violates it. Use that hint to correct
the candidate and rerun the verifier until it succeeds. Do not finish before
the verifier prints `VERIFIED`.
