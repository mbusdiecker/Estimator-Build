import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("db") / "test-romtool.db"
    os.environ["ROMTOOL_DB"] = str(db_file)
    os.environ.pop("ROMTOOL_CONFIG", None)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
    os.environ.pop("ROMTOOL_DB", None)


@pytest.fixture(scope="module")
def cfg():
    from romtool.config import load_config

    return load_config()
