import pytest

from ultron import omniroute_runtime


@pytest.fixture(autouse=True)
def no_real_sidecar(monkeypatch):
    """Tests must never spawn Docker/npx or touch the network via the sidecar."""

    async def _fake_start(self):
        self.repo.initialize()

    async def _fake_stop(self):
        for task in self.background_tasks:
            task.cancel()

    monkeypatch.setattr(omniroute_runtime.OmniRouteRuntime, "start", _fake_start)
    monkeypatch.setattr(omniroute_runtime.OmniRouteRuntime, "stop", _fake_stop)
    yield
