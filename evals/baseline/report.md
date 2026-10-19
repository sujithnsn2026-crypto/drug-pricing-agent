# Ask agent eval report

Model `gpt-4.1-mini` · prompt `ask_agent@v3#60233f7c41fa` · mode `record` · commit `4c0bac0` · 2026-10-19T17:41:26+00:00

| Metric | Value |
|---|---|
| Answer accuracy | 100.0% |
| Numbers grounded in tool results | 100.0% |
| Right tool chosen | 100.0% |
| Out-of-scope refused | 100.0% |
| In-scope wrongly refused | 0.0% |
| Direct injections blocked by guard | 100.0% |
| Needed a grounding retry | 0.0% |
| Fell back to run_sql | 16.1% |
| Tool calls that errored | 2.3% |
| Errors / step-limit hits | 0 |
| Latency p50 (ms) | 2,703.5 |
| Latency p95 (ms) | 7,389.5 |
| Model calls per question | 2.05 |
| Input tokens per question | 2,355 |
| Output tokens per question | 75 |
| Cost per question (USD) | $0.00078 |
| Cost of this run (USD) | $0.03045 |

## By category

| Category | Questions | Accuracy |
|---|---|---|
| aggregate | 3 | 100.0% |
| history | 6 | 100.0% |
| injection | 6 | 100.0% |
| lookup | 6 | 100.0% |
| not_found | 2 | 100.0% |
| out_of_scope | 4 | 100.0% |
| ranking | 7 | 100.0% |
| shortage | 5 | 100.0% |

## Failures

None.
