"""Build the agent's evaluation fixture by running the REAL pipeline.

    python scripts/build_eval_fixture.py

1. scripts/make_eval_data.py writes a synthetic landing zone
2. the Phase 1 pipeline builds Bronze -> Silver -> Gold from it (local Parquet)
3. the three Gold tables the agent reads are copied, as single Parquet files,
   to evals/fixture/gold/ (committed, so agent tests and CI need no Spark)

Re-run it whenever the generator or the Gold logic changes, then re-check the
golden answers in evals/golden.yaml (pytest tests/test_golden_consistency.py
does that automatically).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "evals", "fixture")
TABLES = ["drug_price_current", "fact_price_change", "nadac_price_history"]


def main() -> None:
    landing = os.path.join(FIX, "landing")
    work = os.path.join(ROOT, "_local_eval")
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "make_eval_data.py"), landing], check=True)
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "jobs", "run.py"), "--local", "--local-dir", work,
         "--landing-dir", landing, "--step", "transform"],
        check=True,
    )

    import duckdb  # imported late so the pipeline step doesn't need it

    out = os.path.join(FIX, "gold")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    for t in TABLES:
        src = os.path.join(work, "gold", t, "*.parquet")
        dst = os.path.join(out, f"{t}.parquet")
        duckdb.sql(f"COPY (SELECT * FROM read_parquet('{src}') ORDER BY ALL) TO '{dst}' (FORMAT parquet)")
        n = duckdb.sql(f"SELECT count(*) FROM '{dst}'").fetchone()[0]
        print(f"{t:22s} {n:5d} rows -> {os.path.relpath(dst, ROOT)}")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(landing, ignore_errors=True)


if __name__ == "__main__":
    main()
