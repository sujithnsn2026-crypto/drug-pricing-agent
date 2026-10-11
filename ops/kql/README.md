# Production monitoring queries (Application Insights / Log Analytics)

The agent exports OpenTelemetry spans to Application Insights when
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Spans land in the `dependencies`
table; span attributes are in `customDimensions`. Open the App Insights resource →
**Logs**, paste a query, and pin the chart to a dashboard.

| File | Answers |
|---|---|
| `cost_and_tokens.kql` | What does the agent cost per day, per prompt version, per answer? |
| `latency.kql` | p50 / p95 end-to-end and per-model-call latency |
| `quality_signals.kql` | Grounding failures, retries, step-limit hits, errors — the online proxy for the offline eval |
| `guardrails.kql` | Input blocks and sanitised tool outputs (attack and data-poisoning signals) |
| `tool_usage.kql` | Which tools are used, how often they fail, how often the model falls back to `run_sql` |
| `alert_grounding.kql` | Log-alert query: fires when >5% of answers in an hour have ungrounded numbers |
