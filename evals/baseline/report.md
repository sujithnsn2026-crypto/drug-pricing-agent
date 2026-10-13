# Ask agent eval report

Model `gpt-4.1-mini` · prompt `ask_agent@v3#60233f7c41fa` · mode `record` · commit `d61c18f` · 2026-10-13T03:56:28+00:00

| Metric | Value |
|---|---|
| Answer accuracy | 100.0% |
| Numbers grounded in tool results | 100.0% |
| Right tool chosen | 100.0% |
| Out-of-scope refused | 100.0% |
| In-scope wrongly refused | 0.0% |
| Direct injections blocked by guard | 100.0% |
| Needed a grounding retry | 0.0% |
| Fell back to run_sql | 16.7% |
| Tool calls that errored | 2.4% |
| Errors / step-limit hits | 0 |
| Latency p50 (ms) | 2,360.4 |
| Latency p95 (ms) | 6,547.2 |
| Model calls per question | 2.03 |
| Input tokens per question | 2,239 |
| Output tokens per question | 72 |
| Cost per question (USD) | $0.00094 |
| Cost of this run (USD) | $0.03565 |

## By category

| Category | Questions | Accuracy |
|---|---|---|
| aggregate | 3 | 100.0% |
| history | 5 | 100.0% |
| injection | 6 | 100.0% |
| lookup | 6 | 100.0% |
| not_found | 2 | 100.0% |
| out_of_scope | 4 | 100.0% |
| ranking | 7 | 100.0% |
| shortage | 5 | 100.0% |

## Failures

None.
