"""Thin launcher used by the Databricks job and for local runs.

Adds ../src to the import path so the job runs straight from the synced
bundle files, with no wheel to build.
"""
import os
import sys

try:
    here = os.path.dirname(os.path.abspath(__file__))
except NameError:  # some notebook-style launchers don't define __file__
    here = os.getcwd()
sys.path.insert(0, os.path.join(here, "..", "src"))

from dpa.pipeline import main  # noqa: E402

if __name__ == "__main__":
    main()
