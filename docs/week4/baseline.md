# Baseline Measurement (W4.1)

## Environment
- Adapter: MOCK (deterministic, offline)
- N: 5 runs per case
- Case: clipped-checkout (2828 bytes, 390x844)

## Results
- Runs: 5
- Latency (ms): min 0.18, median 0.22, max 0.63
- Image bytes: 2828
- Findings per run: 1
- Parse status: ok (all runs)

## Limitations
- MOCK adapter: no real model latency or token usage. Token usage = null.
- Single case measured. Real adapter numbers require Gemini key + network.
- Latency reflects local parsing only, not network/provider.

## Raw data
- evaluation/week4/baseline_results.json (per-run records)
- evaluation/week4/baseline_summary.json (summary stats)
```
