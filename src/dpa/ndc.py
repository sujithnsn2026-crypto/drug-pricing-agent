"""NDC normalization.

FDA publishes 10-digit NDCs with hyphens in three layouts
(4-4-2, 5-3-2, 5-4-1). CMS NADAC and most pricing/claims systems use the
11-digit 5-4-2 layout with no hyphens. To join the sources reliably we
convert everything to 11 digits by left-padding each segment with zeros:

    4-4-2  0002-1433-80   -> 00002143380
    5-3-2  12345-678-90   -> 12345067890
    5-4-1  12345-6789-0   -> 12345678900

A bare 10-digit NDC without hyphens is ambiguous (we cannot tell where the
padding goes), so it is rejected rather than guessed.
"""
from __future__ import annotations

import re

_VALID_LAYOUTS = {(4, 4, 2), (5, 3, 2), (5, 4, 1), (5, 4, 2)}


def to_ndc11(raw: str | None) -> str | None:
    """Python version, used in ingestion code and unit tests."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "-" in s:
        parts = s.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return None
        if tuple(len(p) for p in parts) not in _VALID_LAYOUTS:
            return None
        return parts[0].zfill(5) + parts[1].zfill(4) + parts[2].zfill(2)
    digits = re.sub(r"\s", "", s)
    if digits.isdigit() and len(digits) == 11:
        return digits
    return None


def ndc11_col(col):
    """Spark column version of to_ndc11 (no Python UDF, so it stays fast).

    Accepts a Column or a column name.
    """
    from pyspark.sql import functions as F

    c = F.trim(F.col(col) if isinstance(col, str) else col)
    parts = F.split(c, "-")
    layout = F.concat_ws(
        "-",
        F.length(parts.getItem(0)).cast("string"),
        F.length(parts.getItem(1)).cast("string"),
        F.length(parts.getItem(2)).cast("string"),
    )
    hyphenated_ok = (
        (F.size(parts) == 3)
        & layout.isin("4-4-2", "5-3-2", "5-4-1", "5-4-2")
        & c.rlike(r"^\d+-\d+-\d+$")
    )
    padded = F.concat(
        F.lpad(parts.getItem(0), 5, "0"),
        F.lpad(parts.getItem(1), 4, "0"),
        F.lpad(parts.getItem(2), 2, "0"),
    )
    return (
        F.when(c.contains("-") & hyphenated_ok, padded)
        .when(~c.contains("-") & c.rlike(r"^\d{11}$"), c)
        .otherwise(F.lit(None).cast("string"))
    )
