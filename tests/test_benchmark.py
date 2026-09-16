import asyncio

from gateway.benchmark import BenchmarkRunner, percentile


def test_percentile_interpolates_and_handles_empty_series():
    assert percentile([], .95) is None
    assert percentile([10, 20, 30, 40], .5) == 25
    assert percentile([10, 20, 30, 40], .95) == 38.5


def test_request_benchmark_reaches_every_concurrency_level():
    class DeterministicRunner(BenchmarkRunner):
        async def _request(self, name, mode, client, base_url, headers, sequence):
            await asyncio.sleep(0)
            return {"ok": True, "correct": True, "latency_ms": 20 + sequence,
                    "ttft_ms": 10 + sequence, "output_chars": 8,
                    "case": "test", "error": None}

    async def exercise():
        runner = DeterministicRunner()
        runner.start({
            "targets": [{"model": "chat", "max_concurrency": 3}],
            "limit_mode": "requests", "requests_per_model": 12,
            "duration_seconds": 60, "strategy": "parallel",
            "request_timeout_seconds": 30, "warmup": True,
        }, [{"name": "chat", "mode": "chat", "provider_model": "openai/demo"}],
            "http://gateway", {})
        await runner._task
        return runner.snapshot()

    result = asyncio.run(exercise())
    model = result["models"]["chat"]
    assert result["status"] == "completed"
    assert result["progress_percent"] == 100
    assert model["completed"] == 12
    assert list(model["levels"]) == ["1", "2", "3"]
    assert all(model["levels"][level]["completed"] >= int(level) for level in model["levels"])
    assert model["correct_rate"] == 100
    assert model["sustainable_concurrency"] == 3
    assert model["warmup"]["ok"] is True


def test_duration_benchmark_can_be_cancelled_cooperatively():
    class SlowRunner(BenchmarkRunner):
        async def _request(self, name, mode, client, base_url, headers, sequence):
            await asyncio.sleep(.01)
            return {"ok": True, "correct": True, "latency_ms": 10,
                    "ttft_ms": 5, "output_chars": 2, "case": "test", "error": None}

    async def exercise():
        runner = SlowRunner()
        runner.start({
            "targets": [{"model": "chat", "max_concurrency": 2}],
            "limit_mode": "duration", "requests_per_model": 50,
            "duration_seconds": 20, "strategy": "parallel",
            "request_timeout_seconds": 30, "warmup": False,
        }, [{"name": "chat", "mode": "chat", "provider_model": "openai/demo"}],
            "http://gateway", {})
        await asyncio.sleep(.03)
        runner.cancel()
        await runner._task
        return runner.snapshot()

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled"
    assert result["models"]["chat"]["completed"] > 0

