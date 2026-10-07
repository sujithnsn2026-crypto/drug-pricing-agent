import pytest

from dpa.ndc import ndc11_col, to_ndc11

CASES = [
    ("0002-1433-80", "00002143380"),   # 4-4-2
    ("12345-678-90", "12345067890"),   # 5-3-2
    ("12345-6789-0", "12345678900"),   # 5-4-1
    ("12345-6789-01", "12345678901"),  # already 5-4-2
    ("00002143380", "00002143380"),    # already 11 digits
    (" 0002-1433-80 ", "00002143380"), # whitespace
    ("0002143380", None),              # bare 10 digits is ambiguous
    ("123-4567-89", None),             # invalid layout
    ("12a45-6789-01", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_to_ndc11(raw, expected):
    assert to_ndc11(raw) == expected


def test_spark_matches_python(spark):
    df = spark.createDataFrame([(r,) for r, _ in CASES], "raw string")
    got = {r.raw: r.ndc11 for r in df.select("raw", ndc11_col("raw").alias("ndc11")).collect()}
    for raw, expected in CASES:
        assert got[raw] == to_ndc11(raw) == expected, raw
