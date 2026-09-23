import asyncio
import json

import httpx

from gateway.benchmark import BenchmarkRunner, _prompt, percentile


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


def test_chat_scores_only_visible_final_text_and_uses_reported_output_tokens():
    async def exercise():
        runner = BenchmarkRunner()
        runner._state = {"id": "visible-answer"}
        expected = _prompt("visible-answer", 0)[1]

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            events = [
                {"choices": [{"delta": {"reasoning_content": "This is deliberately not the answer."}}]},
                {"choices": [{"delta": {"content": expected}}]},
                {"choices": [], "usage": {"completion_tokens": 7}},
            ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._request("chat", "chat", client, "http://gateway", {}, 0)

    result = asyncio.run(exercise())
    assert result["ok"] is True
    assert result["correct"] is True
    assert result["processed_tokens"] == 7
    assert result["tokens_estimated"] is False


def test_chat_ignores_inline_thinking_and_letter_case_when_scoring():
    async def exercise():
        runner = BenchmarkRunner()
        runner._state = {"id": "inline-thinking"}
        expected = _prompt("inline-thinking", 4)[1]

        def handler(_request: httpx.Request) -> httpx.Response:
            content = f"I should transform the requested word.\n</think>\n{expected.lower()}"
            event = {"choices": [{"delta": {"content": content}}]}
            body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._request("chat", "chat", client, "http://gateway", {}, 4)

    result = asyncio.run(exercise())
    assert result["correct"] is True
    assert result["tokens_estimated"] is True


def test_ollama_chat_disables_thinking_for_short_scored_cases():
    async def exercise():
        runner = BenchmarkRunner()
        runner._state = {
            "id": "ollama-no-thinking",
            "models": {
                "qwen-local": {"provider_model": "ollama/qwen3.5:9b"},
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["reasoning_effort"] == "none"
            event = {"choices": [{"delta": {"content": _prompt("ollama-no-thinking", 0)[1]}}]}
            body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._request("qwen-local", "chat", client, "http://gateway", {}, 0)

    result = asyncio.run(exercise())
    assert result["ok"] is True
    assert result["correct"] is True


def test_workbench_chat_uses_non_streaming_contract_and_has_no_ttft():
    async def exercise():
        runner = BenchmarkRunner()
        runner._state = {
            "id": "workbench-non-streaming",
            "models": {
                "qwen38": {"provider_model": "cloudera_workbench/qwen38"},
            },
        }
        expected = _prompt("workbench-non-streaming", 0)[1]

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["stream"] is False
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": expected}}],
                "usage": {"completion_tokens": 4},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._request("qwen38", "chat", client, "http://gateway", {}, 0)

    result = asyncio.run(exercise())
    assert result["ok"] is True
    assert result["correct"] is True
    assert result["ttft_ms"] is None
    assert result["processed_tokens"] == 4
    assert result["tokens_estimated"] is False


def test_embedding_is_scored_by_its_complete_vector_and_input_usage():
    async def request(vector):
        runner = BenchmarkRunner()
        runner._state = {"id": "embedding"}

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": [{"embedding": vector}],
                "usage": {"prompt_tokens": 23},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._request("embed", "embedding", client, "http://gateway", {}, 0)

    valid = asyncio.run(request([0.1, -0.2, 0.3]))
    invalid = asyncio.run(request([0.1, "not-a-number"]))
    assert valid["correct"] is True
    assert valid["processed_tokens"] == 23
    assert valid["tokens_estimated"] is False
    assert invalid["ok"] is True
    assert invalid["correct"] is False


def test_latest_tokens_per_second_is_persisted_per_model(tmp_path):
    class DeterministicRunner(BenchmarkRunner):
        async def _request(self, name, mode, client, base_url, headers, sequence):
            await asyncio.sleep(0)
            return {"ok": True, "correct": True, "latency_ms": 2, "ttft_ms": 1,
                    "output_chars": 20, "processed_tokens": 5, "tokens_estimated": False,
                    "case": "test", "error": None}

    async def exercise():
        path = tmp_path / "latest.json"
        runner = DeterministicRunner(path)
        runner.start({
            "targets": [{"model": "chat", "max_concurrency": 1}],
            "limit_mode": "requests", "requests_per_model": 2,
            "duration_seconds": 60, "strategy": "parallel",
            "request_timeout_seconds": 30, "warmup": False,
        }, [{"name": "chat", "mode": "chat", "provider_model": "openai/demo"}],
            "http://gateway", {})
        await runner._task
        return runner.snapshot(), BenchmarkRunner(path).snapshot()

    result, restored = asyncio.run(exercise())
    assert result["models"]["chat"]["tokens_per_second"] > 0
    assert result["latest_model_kpis"]["chat"]["token_basis"] == "output"
    assert restored["latest_model_kpis"]["chat"]["tokens_per_second"] > 0
