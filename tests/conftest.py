import pytest


@pytest.fixture(autouse=True)
def _no_always_boards(monkeypatch, request):
    """Searches in tests don't add the big employers checked every day, unless a test asks for them."""
    if "always_boards" not in request.keywords:
        monkeypatch.setattr("jobagent.search.runner.ALWAYS", [])
