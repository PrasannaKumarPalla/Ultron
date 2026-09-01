import httpx
import pytest

from ultron.model_pool import ModelPool


class FakeClient:
    status_code = 200

    def __init__(self, fail=False):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json):
        self.url = url
        self.payload = json
        if self.fail:
            raise httpx.ConnectError("down", request=None)
        return self


def test_pool_rejects_zero_size():
    with pytest.raises(ValueError):
        ModelPool("http://x", size=0)


@pytest.mark.asyncio
async def test_warm_keeps_only_top_n_models_alive(monkeypatch):
    seen = []

    class RecordingClient(FakeClient):
        async def post(self, url, json):
            await super().post(url, json)
            seen.append((url, json))
            return self

    monkeypatch.setattr("ultron.model_pool.httpx.AsyncClient",
                        lambda timeout: RecordingClient())
    pool = ModelPool("http://127.0.0.1:11434", size=2, keep_alive="30m")

    status = await pool.warm(["model-a", "model-b", "model-c"])

    assert len(seen) == 2
    assert all(payload["keep_alive"] == "30m" for _, payload in seen)
    assert status == {"model-a": "warm", "model-b": "warm"}
    assert pool.snapshot()["models"]["model-a"] == "warm"


@pytest.mark.asyncio
async def test_warm_records_unavailable_when_ollama_down(monkeypatch):
    monkeypatch.setattr("ultron.model_pool.httpx.AsyncClient",
                        lambda timeout: FakeClient(fail=True))
    pool = ModelPool("http://127.0.0.1:11434")

    status = await pool.warm(["qwen3:30b"])

    assert "unavailable" in status["qwen3:30b"]
