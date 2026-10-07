import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


@pytest.fixture(scope="session")
def spark():
    from dpa.pipeline import get_spark

    s = get_spark(local=True)
    yield s
    s.stop()


@pytest.fixture(scope="session")
def landing(tmp_path_factory):
    import make_sample_data

    path = str(tmp_path_factory.mktemp("landing"))
    make_sample_data.write(path)
    return path


@pytest.fixture(scope="session")
def bronze(spark, landing):
    from dpa.bronze import build_bronze

    return {k: v.cache() for k, v in build_bronze(spark, landing).items()}


@pytest.fixture(scope="session")
def silver(bronze):
    from dpa.silver import build_silver

    return {k: v.cache() for k, v in build_silver(bronze).items()}
