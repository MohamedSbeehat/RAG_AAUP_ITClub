import pytest


@pytest.fixture(autouse=True)
def isolated_legacy_defaults(monkeypatch):
    # Existing unit fixtures use single-workspace mode. Tenant tests explicitly opt in.
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "false")
