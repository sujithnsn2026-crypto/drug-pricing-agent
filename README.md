# Drug Pricing Intelligence Agent (Azure)

A multi-agent system for US drug prices and shortages, built on **public data only**
(CMS NADAC, FDA NDC Directory, openFDA Drug Shortages).

- **Ask agent** answers questions on demand, for example:
  > *"Which generic drugs had the biggest price jump this quarter, and is a shortage driving it?"*
- **Price-Watch team** runs every week without being asked: it finds unusual price moves, sends one
  investigator agent per signal to find the cause, verifies every number against the data, and drafts a
  ranked brief that a person approves before it is sent.

Both reach the data through one **MCP (Model Context Protocol) tool server** over a tested Databricks
medallion pipeline.

```mermaid
flowchart LR
    subgraph Sources[Public sources]
        A[CMS NADAC<br/>weekly drug acquisition cost]
        B[FDA NDC Directory]
        C[openFDA Drug Shortages]
        D[openFDA Drug Labels]
    end
    subgraph DBX[Azure Databricks + Unity Catalog]
        L[(Landing volume)] --> BR[(Bronze)] --> SV[(Silver)] --> GD[(Gold<br/>SCD2 history,<br/>price changes,<br/>current prices)]
        DQ{{DQ checks}} -.-> SV & GD
    end
    A & B & C --> L
    D --> AIS[(Azure AI Search<br/>hybrid + rerank)]
    GD --> TOOLS[Read-only tools<br/>+ SQL guard]
    TOOLS --> ASK[Ask agent<br/>Azure OpenAI tool calling]
    ASK --> OPS[[LLMOps: prompt registry · OTel traces ·<br/>cost · guardrails · eval gate]]
    OPS --> APPI[(Application Insights<br/>KQL + alerts)]
    GD & AIS --> MCP[dpa-mcp server]
    MCP --> PW[Price-Watch workflow<br/>detect → investigate ×N → verify]
    PW --> HUM{{Human approval}} --> BRIEF[Weekly brief]
    style MCP stroke-dasharray: 5 5
    style PW stroke-dasharray: 5 5
    style AIS stroke-dasharray: 5 5
    style HUM stroke-dasharray: 5 5
    style BRIEF stroke-dasharray: 5 5
```

Dashed boxes are later phases. **Built so far: the data layer (Phase 1) and the Ask agent with its
LLMOps layer (Phase 4a + 6).**

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Ingestion, Bronze/Silver/Gold, SCD2 price history, data-quality checks, CI, Azure workspace | ✅ done |
| 1b | First run on real CMS/FDA data in Databricks; unpause the weekly schedule | next |
| 4a | Ask agent: Azure OpenAI tool calling over Gold with read-only tools and a parsed SQL guard | ✅ done |
| 6 | **LLMOps:** golden eval set (38 questions incl. red-team), deterministic scoring, record/replay, eval-gated CI, prompt registry, OpenTelemetry → App Insights, cost per answer, injection + number-grounding guardrails, KQL monitoring, Bicep | ✅ done |
| 2 | Drug labels in Azure AI Search: hybrid search, semantic rerank, citations | |
| 3 | `dpa-mcp` server: the same read-only tools served over MCP (managed identity) | |
| 4b | Move the Ask agent onto Azure AI Foundry Agent Service using the MCP tools | |
| 5 | Price-Watch multi-agent workflow (Microsoft Agent Framework): SQL signal finder, parallel investigators, verifier, human approval | |
| 6b | Calibrated LLM judge for free-text quality, trajectory evals, online feedback loop | |
| 7 | Terraform, FastAPI + review UI on Container Apps, write-up and demo video | |

### Design principle for the agents

Use an LLM only where judgment is needed. Finding price moves is plain SQL (exact, cheap, repeatable);
explaining them is agent work; checking the explanation's numbers is code again. Drug label text is
treated as untrusted input, and every tool is read-only.


## The Ask agent and its LLMOps layer

```mermaid
flowchart LR
    Q[Question] --> IG{Input guard<br/>injection / size}
    IG -- blocked --> R1[Refusal, 0 tokens]
    IG --> LOOP
    subgraph LOOP[Agent loop, max 6 steps]
        M[Azure OpenAI<br/>prompt from registry] -- tool calls --> T[Read-only tools<br/>search · price · top changes · history · run_sql]
        T -- SQL --> G[SQL guard<br/>parsed AST, allow-list, LIMIT] --> DB[(Gold via DuckDB<br/>file + network access off)]
        T -- results --> S[Scrub instruction-like text<br/>from FDA free text] --> M
    end
    LOOP --> NG{Number grounding:<br/>every number in a tool result?}
    NG -- no, once --> M
    NG --> A[Answer + status, tokens, cost, prompt ref, trace id]
    LOOP -. spans .-> OT[OpenTelemetry GenAI spans] --> AI[(App Insights)]
```

| Piece | Where | What it does |
|---|---|---|
| Tools | `src/dpa/agent/tools.py` | Four fixed, parameterised queries for common questions, plus `run_sql` as a guarded escape hatch. Errors go back to the model as data so it can correct itself. |
| SQL guard | `src/dpa/agent/sql_guard.py` | Parses model SQL with sqlglot: one SELECT, allow-listed tables, no table functions, file paths or env access, forced LIMIT. The DuckDB connection also has external access off and its config locked, so a guard bypass still can't read files. |
| Prompt registry | `prompts/`, `src/dpa/llmops/prompts.py` | One immutable YAML per version; `registry.yaml` picks the live one. Every trace and eval result carries `id@version#sha`. |
| Tracing | `src/dpa/llmops/tracing.py` | OpenTelemetry spans using the GenAI semantic conventions (`invoke_agent`, `chat`, `execute_tool`) with tokens, cost, prompt version and guardrail outcomes. Exports to App Insights and/or JSONL. Question/answer text only with `DPA_TRACE_CONTENT=true`. |
| Cost | `src/dpa/llmops/cost.py`, `config/model_prices.yaml` | Cost per answer from token usage (cached input priced separately). Unknown price = `n/a`, never 0. |
| Guardrails | `src/dpa/llmops/guardrails.py` | Input injection block (spends no tokens); scrubbing of instruction-like text in tool results (indirect injection); number grounding with one self-correction round. |
| Golden set | `evals/golden.yaml` | 38 questions: ranking, lookup, history, aggregates, shortages, not-found, out-of-scope, and red-team (direct, indirect via a poisoned FDA field, SQL write attempt). Numeric expectations are re-derived from the data in a test. |
| Eval fixture | `evals/fixture/gold/` | Gold tables produced by running **the real pipeline** on a synthetic 26-drug, 70-week dataset (`scripts/build_eval_fixture.py`). |
| Scoring | `src/dpa/evals/scoring.py` | Deterministic checks only (substrings, numbers at the answer's precision, refusal prefix, tools used). Metrics: accuracy, grounding, tool selection, refusal recall, false refusals, injection blocks, `run_sql` fallback, p50/p95 latency, tokens, cost. |
| Record / replay | `src/dpa/agent/llm.py`, `evals/cassettes/` | A live run records every model response; CI replays them: free, deterministic, every push. Any change to prompt, tools or data changes the request hash, so CI says "re-record" instead of passing on stale answers. |
| Gate | `evals/gate.yaml`, `src/dpa/evals/gate.py` | Absolute thresholds + no regression vs the committed baseline + safety-critical questions that must pass one by one. |
| CI | `.github/workflows/ci.yml`, `eval-live.yml` | `ci`: unit tests + replayed eval + gate on every push. `eval-live`: real model, weekly and on prompt/agent PRs, uploads a fresh cassette. |
| Monitoring | `ops/kql/` | KQL for cost/day, latency, grounding failures, guardrail hits, tool failures; a scheduled alert on ungrounded answers. |
| Infra | `infra/main.bicep` | Log Analytics (daily cap), App Insights, Azure OpenAI with a pinned, capacity-capped deployment (`NoAutoUpgrade`, so evals stay valid), alert rule. |

### Run it

```bash
pip install -e ".[agent]"                       # no Spark or Java needed for the agent
pytest -q tests/test_agent_guards.py tests/test_agent_loop.py tests/test_evals.py

dpa-eval run --mode replay                      # golden set from the cassette: free, offline
dpa-eval gate evals/runs/<run>                  # PASS/FAIL vs thresholds and baseline

# With Azure OpenAI (copy .env.example to .env and fill it in):
dpa-ask "Which generic drug rose most in price over 90 days, and is it in shortage?"
dpa-eval run --mode record --model gpt-4.1-mini # live run, records a new cassette
dpa-eval run --mode record --prompt-version v2  # try a new prompt version...
dpa-eval compare evals/runs/<v1> evals/runs/<v2> # ...and see what it fixed and broke
dpa-eval promote evals/runs/<v2>                # make it the new baseline
```

### LLMOps decisions worth explaining

- **Deterministic scoring before LLM judges.** Every gate metric is computed by code: the expected numbers,
  the refusal prefix, the tools used. A judge model adds its own variance and cost; it's planned only for
  free-text quality that code can't check, and only after calibrating it against hand labels.
- **Numbers must come from tools.** The prompt forbids model arithmetic, and a code check enforces it: any
  number in the answer that isn't in a tool result (at the answer's precision) triggers one correction
  round, then a visible flag. Derived figures go through `run_sql`, so they are computed, not guessed.
- **Two locks on SQL.** The AST guard gives the model a useful error to fix its query; the read-only,
  no-external-access database is the backstop if the guard has a gap.
- **Red-team cases live in the golden set.** Including an *indirect* injection planted in an FDA shortage
  reason, because tool output is the realistic attack path for an agent that reads public data.
  Safety questions are "critical": they must pass individually, so easy wins can't average them away.
- **Record/replay makes evals a unit test.** Live model runs are slow and cost money, so they run weekly
  and on prompt changes; every other push replays the recording for free.
- **Pinned model, capped capacity.** `NoAutoUpgrade` stops Azure from swapping the model under a passing
  baseline; a low TPM cap bounds the worst-case bill.
- **Cost is a gated metric.** A prompt that answers 2 points better but costs 3x more fails the regression
  rule unless the trade-off is accepted explicitly.

## What Phase 1 builds

| Table | Grain | Purpose |
|---|---|---|
| `bronze.nadac`, `bronze.shortages`, `bronze.ndc_directory` | as landed | raw, all strings, `_source_file` lineage |
| `silver.nadac_prices` | NDC × weekly file | typed prices, brand/generic flag |
| `silver.drug_shortages` | shortage record | latest FDA snapshot, NDC normalized |
| `silver.ndc_products` | package NDC | product names, labeler, dosage form, ingredients |
| `gold.nadac_price_history` | NDC × price version | **SCD Type 2** history (`valid_from`, `valid_to`, `is_current`) |
| `gold.fact_price_change` | price change | old/new price and % change |
| `gold.drug_price_current` | active NDC | current price, 90-day and 1-year change, shortage flag; **the agent's main table** |
| `gold.dq_results` | check × run | data-quality results over time |

## Run it locally (no Azure needed)

Needs Python 3.10+ and Java 17+.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest -q                                              # 32 tests

python scripts/make_sample_data.py sample_data/landing # synthetic sample in the real formats
python jobs/run.py --local --landing-dir sample_data/landing --step transform
```

Output lands in `_local/` as Parquet. To pull the **real** public data to your laptop:

```bash
python jobs/run.py --local --step all       # downloads to _local/landing, then builds every layer
```

## Deploy to Azure

1. **Create the workspace** (Premium tier is needed for Unity Catalog):
   ```bash
   az extension add --name databricks
   az group create -n rg-dpa -l centralindia
   az databricks workspace create -g rg-dpa -n dbw-dpa -l centralindia --sku premium
   ```
   Set a budget alert on the resource group before anything else.
2. **Create catalog, schemas and the landing volume:** run `setup/00_unity_catalog.sql` in the workspace SQL editor.
3. **Deploy the job** with the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html):
   ```bash
   databricks auth login --host https://adb-<id>.azuredatabricks.net
   # put the same URL in databricks.yml -> targets.dev.workspace.host
   databricks bundle validate
   databricks bundle deploy -t dev
   databricks bundle run dpa_pipeline -t dev
   ```
   The first run backfills two years of NADAC (a few million rows) and builds every layer.
4. **Add table comments:** run `setup/10_gold_comments.sql`. The Phase 3 agent reads these.
5. Unpause the weekly schedule in `databricks.yml` once a manual run is green.

## Design decisions

These are the parts worth explaining in an interview.

- **One join key everywhere.** FDA uses hyphenated 10-digit NDCs in three layouts (4-4-2, 5-3-2, 5-4-1);
  CMS uses 11 digits. `dpa/ndc.py` pads each segment to 5-4-2, as a native Spark expression rather than
  a Python UDF so it stays fast. A bare 10-digit NDC is rejected instead of guessed, because the
  padding position can't be known.
- **SCD2 two ways, proven equal.** `build_price_history` rebuilds history from all weekly files with
  gaps-and-islands; `scd2_staged_updates` + `merge_staged_delta` apply one new week with a single
  atomic Delta MERGE. A test loads week 1, applies weeks 2-8 incrementally, and asserts the result is
  identical to the full rebuild, including NDCs that drop out of NADAC and come back.
- **Drop-outs are not price changes.** An NDC missing from a weekly file closes its version at the last
  date it was seen; when it returns, a new version opens, and `fact_price_change` does not count it as
  a change.
- **Bronze is all strings; Silver is ANSI-safe.** A malformed value in a CMS file becomes null and gets
  caught by a data-quality check, instead of failing the whole run (Spark 4 and newer Databricks
  runtimes have ANSI mode on by default).
- **Schema drift between years.** CMS has changed CSV column order between releases. Each NADAC year
  is read separately and unioned by column name, so values can't silently shift columns. The sample
  data reproduces this on purpose.
- **Idempotent ingestion.** Closed years download once; the current year's cumulative file replaces
  the previous copy; openFDA is snapshotted daily, keeping the last 7. Dataset IDs are discovered from
  the CMS metastore by title because CMS issues a new ID every year.
- **Storage is swappable.** Transforms take and return DataFrames. `DeltaStore` writes Unity Catalog
  tables; `LocalStore` writes Parquet, so the whole pipeline runs and is tested without a cluster.

## Known limitations (good next tasks)

- A CMS correction that changes a price **without** moving the effective date would create two
  versions with the same `valid_from`. The data-quality checks would catch it in a full rebuild, but the
  incremental MERGE would fail first. Fix idea: treat it as an in-place correction in both paths.
- Delta MERGE is exercised on Databricks only; locally the same staged rows are applied by
  `apply_staged_local`, which the equality test covers.
- Shortages are matched to prices by exact NDC. Matching by ingredient (via RxNorm) would catch more.

## Data sources and terms

- [CMS NADAC](https://data.medicaid.gov) (National Average Drug Acquisition Cost): US public domain.
- [FDA NDC Directory](https://open.fda.gov/apis/drug/ndc/) and [openFDA Drug Shortages](https://open.fda.gov/apis/drug/drugshortages/): see the [openFDA terms](https://open.fda.gov/terms/). No API key needed; set `OPENFDA_API_KEY` for higher limits.

The sample data in `sample_data/` is synthetic. This is a portfolio project, not medical or pricing advice.
