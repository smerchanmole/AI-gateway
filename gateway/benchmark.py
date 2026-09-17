"""Controlled load-test engine for models served through LiteLLM.

The test suite uses only the gateway's internal endpoint. It therefore measures
the real path through LiteLLM, callbacks, guardrails, and the provider without
exposing credentials to the browser. Each model traverses concurrency levels
1..N and retains only aggregates, recent errors, and a bounded time series.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import random
import re
import time
from typing import Any
from uuid import uuid4

import httpx


def percentile(values: list[float], percentile_value: float) -> float | None:
    """Return a stable linear percentile, or ``None`` when no samples exist."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 1)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 1)


def _normalise_answer(value: str) -> str:
    value = re.sub(r"^```(?:text|json)?\s*|\s*```$", "", value.strip(), flags=re.I)
    return re.sub(r"\s+", " ", value).strip().casefold().rstrip(".!")


def _prompt(seed: str, sequence: int) -> tuple[str, str, str]:
    """Create deterministic, self-scoring cases that need no external data."""

    rng = random.Random(f"{seed}:{sequence}")
    kind = sequence % 5
    if kind == 0:
        left, right = rng.randint(11, 89), rng.randint(11, 89)
        return f"Suma {left} + {right}. Responde únicamente con el número.", str(left + right), "suma"
    if kind == 1:
        left, right = rng.randint(3, 14), rng.randint(3, 14)
        return f"Multiplica {left} × {right}. Responde únicamente con el número.", str(left * right), "producto"
    if kind == 2:
        values = [rng.randint(10, 99) for _ in range(4)]
        expected = ",".join(str(item) for item in sorted(values))
        return f"Ordena de menor a mayor: {', '.join(map(str, values))}. Responde sólo con números separados por comas y sin espacios.", expected, "orden"
    if kind == 3:
        token = f"carga-{rng.randint(1000, 9999)}"
        return f"Copia exactamente este identificador, sin añadir nada: {token}", token, "copia"
    word = rng.choice(["gateway", "latencia", "modelo", "inferencia", "vector"])
    return f"Escribe en MAYÚSCULAS esta palabra y nada más: {word}", word.upper(), "mayúsculas"


class BenchmarkRunner:
    """Keep only one active campaign to prevent accidental overlapping loads."""

    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None
        self._cancel: asyncio.Event | None = None

    def start(
        self,
        config: dict[str, Any],
        models: list[dict[str, Any]],
        base_url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RuntimeError("Ya hay una batería de pruebas en ejecución")
        run_id = uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        self._cancel = asyncio.Event()
        self._state = {
            "id": run_id,
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "elapsed_seconds": 0.0,
            "config": deepcopy(config),
            "models": {},
            "error": None,
        }
        for model in models:
            name = str(model["name"])
            maximum = next(item["max_concurrency"] for item in config["targets"] if item["model"] == name)
            self._state["models"][name] = {
                "name": name,
                "mode": model.get("mode", "chat"),
                "provider_model": model.get("provider_model", ""),
                "max_concurrency": maximum,
                "current_level": 0,
                "in_flight": 0,
                "attempted": 0,
                "completed": 0,
                "successes": 0,
                "errors": 0,
                "correct": 0,
                "incorrect": 0,
                "ttft_values": [],
                "latency_values": [],
                "output_chars": 0,
                "levels": {},
                "timeline": [],
                "recent_errors": [],
                "warmup": None,
                "started_elapsed": None,
                "finished_elapsed": None,
            }
        self._task = asyncio.create_task(self._run(base_url.rstrip("/"), headers))
        return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        if not self._state or self._state["status"] not in {"running", "cancelling"}:
            raise RuntimeError("No hay una batería activa que cancelar")
        self._state["status"] = "cancelling"
        if self._cancel:
            self._cancel.set()
        return self.snapshot()

    async def shutdown(self) -> None:
        """Stop the campaign immediately when the control plane shuts down."""

        if self._task and not self._task.done():
            if self._cancel:
                self._cancel.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> dict[str, Any]:
        if not self._state:
            return {"status": "idle", "models": {}, "progress_percent": 0}
        state = deepcopy(self._state)
        started = datetime.fromisoformat(state["started_at"])
        if state["status"] in {"running", "cancelling"}:
            state["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        config = state["config"]
        total_completed = sum(item["completed"] for item in state["models"].values())
        if config["limit_mode"] == "requests":
            target = config["requests_per_model"] * len(state["models"])
            progress = total_completed / max(target, 1)
        else:
            multiplier = len(state["models"]) if config["strategy"] == "sequential" else 1
            progress = state["elapsed_seconds"] / max(config["duration_seconds"] * multiplier, 1)
        state["progress_percent"] = round(min(progress, 1) * 100, 1)
        for item in state["models"].values():
            ttft = item.pop("ttft_values")
            latency = item.pop("latency_values")
            completed = item["completed"]
            elapsed = max((item["finished_elapsed"] or state["elapsed_seconds"]) - (item["started_elapsed"] or 0), .001)
            item.update({
                "error_rate": round(item["errors"] * 100 / max(completed, 1), 1),
                "correct_rate": round(item["correct"] * 100 / max(item["successes"], 1), 1),
                "requests_per_second": round(completed / elapsed, 2),
                "ttft_ms": {"p50": percentile(ttft, .5), "p95": percentile(ttft, .95), "p99": percentile(ttft, .99)},
                "latency_ms": {"p50": percentile(latency, .5), "p95": percentile(latency, .95), "p99": percentile(latency, .99)},
                "estimated_output_tokens": round(item["output_chars"] / 4),
            })
            for level in item["levels"].values():
                level_ttft = level.pop("ttft_values")
                level_latency = level.pop("latency_values")
                level["error_rate"] = round(level["errors"] * 100 / max(level["completed"], 1), 1)
                level["correct_rate"] = round(level["correct"] * 100 / max(level["successes"], 1), 1)
                level["ttft_p95_ms"] = percentile(level_ttft, .95)
                level["latency_p95_ms"] = percentile(level_latency, .95)
            item["sustainable_concurrency"] = self._sustainable_level(item)
        return state

    @staticmethod
    def _sustainable_level(item: dict[str, Any]) -> int | None:
        baseline = item["levels"].get("1", {}).get("latency_p95_ms")
        sustainable = None
        for level_text, metrics in sorted(item["levels"].items(), key=lambda pair: int(pair[0])):
            latency = metrics.get("latency_p95_ms")
            if (metrics.get("completed", 0) and metrics.get("error_rate", 100) <= 1
                    and metrics.get("correct_rate", 0) >= 95 and (
                baseline is None or latency is None or latency <= baseline * 2
            )):
                sustainable = int(level_text)
        return sustainable

    async def _run(self, base_url: str, headers: dict[str, str]) -> None:
        assert self._state is not None
        maximum_connections = sum(item["max_concurrency"] for item in self._state["config"]["targets"]) + 4
        timeout = httpx.Timeout(float(self._state["config"]["request_timeout_seconds"]))
        sampler = asyncio.create_task(self._sample_timeline())
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=maximum_connections)) as client:
                names = list(self._state["models"])
                if self._state["config"]["strategy"] == "parallel":
                    await asyncio.gather(*(self._run_model(name, client, base_url, headers) for name in names))
                else:
                    for name in names:
                        if self._cancel and self._cancel.is_set():
                            break
                        await self._run_model(name, client, base_url, headers)
            self._state["status"] = "cancelled" if self._cancel and self._cancel.is_set() else "completed"
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            raise
        except Exception as exc:  # el error se expone sin perder resultados parciales
            self._state["status"] = "failed"
            self._state["error"] = str(exc)
        finally:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
            self._state["finished_at"] = datetime.now(timezone.utc).isoformat()
            started = datetime.fromisoformat(self._state["started_at"])
            self._state["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
            self._record_timeline()

    async def _run_model(self, name: str, client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> None:
        assert self._state is not None
        item = self._state["models"][name]
        item["started_elapsed"] = self._elapsed()
        if self._state["config"]["warmup"] and not (self._cancel and self._cancel.is_set()):
            warmup = await self._request(name, item["mode"], client, base_url, headers, -1)
            item["warmup"] = {key: warmup.get(key) for key in ("ok", "latency_ms", "ttft_ms", "error")}

        maximum = item["max_concurrency"]
        if self._state["config"]["limit_mode"] == "requests":
            total = self._state["config"]["requests_per_model"]
            minimum = maximum * (maximum + 1) // 2
            extra, remainder = divmod(total - minimum, maximum)
            quotas = [level + extra + (1 if level <= remainder else 0) for level in range(1, maximum + 1)]
            for level, quota in enumerate(quotas, 1):
                if self._cancel and self._cancel.is_set():
                    break
                await self._request_stage(name, level, quota, client, base_url, headers)
        else:
            stage_seconds = self._state["config"]["duration_seconds"] / maximum
            for level in range(1, maximum + 1):
                if self._cancel and self._cancel.is_set():
                    break
                await self._duration_stage(name, level, stage_seconds, client, base_url, headers)
        item["current_level"] = 0
        item["finished_elapsed"] = self._elapsed()

    def _new_level(self, item: dict[str, Any], level: int) -> dict[str, Any]:
        item["current_level"] = level
        metrics = item["levels"].setdefault(str(level), {
            "completed": 0, "successes": 0, "errors": 0, "correct": 0,
            "ttft_values": [], "latency_values": [],
        })
        self._record_timeline()
        return metrics

    async def _request_stage(self, name: str, level: int, quota: int, client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> None:
        item = self._state["models"][name]
        self._new_level(item, level)
        queue: asyncio.Queue[int] = asyncio.Queue()
        for _ in range(quota):
            queue.put_nowait(item["attempted"] + queue.qsize())

        async def worker() -> None:
            while not queue.empty() and not (self._cancel and self._cancel.is_set()):
                try:
                    sequence = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self._execute_one(name, sequence, client, base_url, headers)

        await asyncio.gather(*(worker() for _ in range(min(level, quota))))

    async def _duration_stage(self, name: str, level: int, seconds: float, client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> None:
        item = self._state["models"][name]
        self._new_level(item, level)
        deadline = time.monotonic() + seconds

        async def worker() -> None:
            while time.monotonic() < deadline and not (self._cancel and self._cancel.is_set()):
                await self._execute_one(name, item["attempted"], client, base_url, headers)

        await asyncio.gather(*(worker() for _ in range(level)))

    async def _execute_one(self, name: str, sequence: int, client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> None:
        item = self._state["models"][name]
        item["attempted"] += 1
        item["in_flight"] += 1
        try:
            result = await self._request(name, item["mode"], client, base_url, headers, sequence)
        finally:
            item["in_flight"] -= 1
        self._record_result(item, result)

    async def _request(self, name: str, mode: str, client: httpx.AsyncClient, base_url: str, headers: dict[str, str], sequence: int) -> dict[str, Any]:
        started = time.perf_counter()
        if mode == "embedding":
            text = f"Documento sintético de carga {sequence}: la inferencia vectorial debe devolver un vector numérico."
            try:
                response = await client.post(f"{base_url}/v1/embeddings", headers=headers, json={"model": name, "input": text, "encoding_format": "float"})
                latency = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                body = response.json()
                vector = body.get("data", [{}])[0].get("embedding", [])
                valid = bool(vector) and all(isinstance(value, (int, float)) for value in vector[:20])
                return {"ok": True, "correct": valid, "latency_ms": latency, "ttft_ms": None, "output_chars": 0,
                        "case": "vector", "error": None}
            except Exception as exc:
                return {"ok": False, "correct": False, "latency_ms": (time.perf_counter() - started) * 1000,
                        "ttft_ms": None, "output_chars": 0, "case": "vector", "error": self._error_text(exc)}

        prompt, expected, case = _prompt(self._state["id"], sequence)
        payload = {"model": name, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                   "max_tokens": 32, "stream": True}
        first_token: float | None = None
        answer_parts: list[str] = []
        try:
            async with client.stream("POST", f"{base_url}/v1/chat/completions", headers=headers, json=payload) as response:
                if response.is_error:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"HTTP {response.status_code}: {body[:500]}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    raw = line[5:].strip() if line.startswith("data:") else line
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or choice.get("message") or {}
                    content = delta.get("content") or delta.get("reasoning_content") or ""
                    if content:
                        if first_token is None:
                            first_token = time.perf_counter()
                        answer_parts.append(str(content))
            finished = time.perf_counter()
            answer = "".join(answer_parts)
            return {"ok": True, "correct": _normalise_answer(answer) == _normalise_answer(expected),
                    "latency_ms": (finished - started) * 1000,
                    "ttft_ms": ((first_token or finished) - started) * 1000,
                    "output_chars": len(answer), "case": case, "error": None}
        except Exception as exc:
            return {"ok": False, "correct": False, "latency_ms": (time.perf_counter() - started) * 1000,
                    "ttft_ms": None, "output_chars": 0, "case": case, "error": self._error_text(exc)}

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
        return str(exc)[:600] or exc.__class__.__name__

    def _record_result(self, item: dict[str, Any], result: dict[str, Any]) -> None:
        item["completed"] += 1
        level = item["levels"][str(item["current_level"])]
        level["completed"] += 1
        item["latency_values"].append(result["latency_ms"])
        level["latency_values"].append(result["latency_ms"])
        if result["ttft_ms"] is not None:
            item["ttft_values"].append(result["ttft_ms"])
            level["ttft_values"].append(result["ttft_ms"])
        if result["ok"]:
            item["successes"] += 1
            level["successes"] += 1
            item["output_chars"] += result["output_chars"]
            if result["correct"]:
                item["correct"] += 1
                level["correct"] += 1
            else:
                item["incorrect"] += 1
        else:
            item["errors"] += 1
            level["errors"] += 1
            item["recent_errors"].append({"at_seconds": round(self._elapsed(), 1), "level": item["current_level"],
                                           "case": result["case"], "message": result["error"]})
            item["recent_errors"] = item["recent_errors"][-8:]

    async def _sample_timeline(self) -> None:
        while True:
            self._record_timeline()
            await asyncio.sleep(.75)

    def _record_timeline(self) -> None:
        if not self._state:
            return
        elapsed = round(self._elapsed(), 2)
        for item in self._state["models"].values():
            model_elapsed = max(elapsed - (item["started_elapsed"] or 0), .001)
            item["timeline"].append({
                "seconds": elapsed,
                "level": item["current_level"],
                "in_flight": item["in_flight"],
                "completed": item["completed"],
                "errors": item["errors"],
                "ttft_p95_ms": percentile(item["ttft_values"], .95),
                "requests_per_second": round(item["completed"] / model_elapsed, 2),
            })
            item["timeline"] = item["timeline"][-600:]

    def _elapsed(self) -> float:
        if not self._state:
            return 0
        return (datetime.now(timezone.utc) - datetime.fromisoformat(self._state["started_at"])).total_seconds()
