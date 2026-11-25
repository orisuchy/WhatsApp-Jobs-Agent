import pytest

import os

import main
import settings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never read the owner's real data/ during tests."""
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "JD_TEXT", "")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Settings overrides write into os.environ: give each test its own copy."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(settings, "_orig", {})


@pytest.fixture(autouse=True)
def reset_ui_state(monkeypatch):
    """/state pushes mutate a module global; restore it after every test."""
    monkeypatch.setattr(main, "ui_state", dict(main.ui_state))


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    """Model metadata lookups hit the network; tests opt in by patching these again."""
    monkeypatch.setattr(main, "model_context", lambda env=None: (None, None))
    monkeypatch.setattr(main, "list_models", lambda env=None: ([], None))
