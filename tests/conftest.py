import os

import pytest

# Skip ~/.config/harnh3x/harnh3x.conf so the suite uses built-in defaults.
os.environ["HARNH3X_CONFIG"] = ""


@pytest.fixture(autouse=True)
def _isolate_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.config.BASE_DIR", str(tmp_path))
    monkeypatch.setattr("harnh3x.config.API_KEY", "test-key")
    monkeypatch.setattr("harnh3x.config.API_URL", "http://127.0.0.1:9/v1")
