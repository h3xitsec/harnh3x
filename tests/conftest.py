import os

import pytest

# Skip ~/.config/harnh3x/harnh3x.yaml so the suite uses built-in defaults.
os.environ["HARNH3X_CONFIG"] = ""

from harnh3x.config import Provider

TEST_PROVIDER = Provider(
    name="local", kind="openai", api_url="http://127.0.0.1:9/v1", api_key="test-key"
)


@pytest.fixture(autouse=True)
def _isolate_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.config.BASE_DIR", str(tmp_path))
    monkeypatch.setattr("harnh3x.config.PROVIDERS", (TEST_PROVIDER,))
    monkeypatch.setattr("harnh3x.config.PROVIDER_WARNINGS", ())
