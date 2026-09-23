"""Periodic availability checks for aliases loaded by LiteLLM.

The monitor lives in the control-plane process, rather than in the browser, so
checks continue when the dashboard is closed.  It deliberately sends one tiny
request through LiteLLM itself: a healthy provider reached by some other route
does not prove that the public gateway alias is operational.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any, Callable

import httpx


class ModelHealthMonitor:
    """Probe every enabled alias present in LiteLLM's active configuration."""

    def __init__(
        self,
        *,
        models: Callable[[], list[dict[str, Any]]],
        active_names: Callable[[], list[str]],
        is_running: Callable[[], bool],
        base_url: str,
        interval_seconds: int = 300,
        timeout_seconds: float = 180,
        max_concurrency: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._models = models
        self._active_names = active_names
        self._is_running = is_running
        self._base_url = base_url.rstrip("/")
        self._interval_seconds = max(1, int(interval_seconds))
        self._timeout_seconds = timeout_seconds
        self._max_concurrency = max(1, int(max_concurrency))
        self._transport = transport
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "running": False,
            "gateway_status": "unknown",
            "interval_seconds": self._interval_seconds,
            "last_cycle_started_at": None,
            "last_cycle_finished_at": None,
            "next_cycle_at": None,
            "models": {},
        }

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def snapshot(self) -> dict[str, Any]:
        """Return a copy safe for concurrent API serialization."""

        return deepcopy(self._state)

    def start(self) -> None:
        """Start one supervisor task; repeated calls are harmless."""

        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._supervise())

    def trigger(self) -> None:
        """Wake the supervisor after LiteLLM starts or its model set changes."""

        self._wake.set()

    async def shutdown(self) -> None:
        """Cancel the supervisor without delaying application shutdown."""

        if not self._task or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _supervise(self) -> None:
        """Run immediately, then keep a five-minute start-to-start cadence."""

        while True:
            cycle_started = time.monotonic()
            await self.run_once()
            delay = max(0.0, self._interval_seconds - (time.monotonic() - cycle_started))
            next_cycle = self._now() + timedelta(seconds=delay)
            self._state["next_cycle_at"] = self._iso(next_cycle)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def run_once(self) -> dict[str, Any]:
        """Execute a complete non-overlapping health cycle."""

        async with self._run_lock:
            started = self._now()
            self._state.update({
                "running": True,
                "last_cycle_started_at": self._iso(started),
                "next_cycle_at": None,
            })
            if not self._is_running():
                self._state.update({
                    "running": False,
                    "gateway_status": "stopped",
                    "last_cycle_finished_at": self._iso(self._now()),
                    "models": {},
                })
                return self.snapshot()

            active = set(self._active_names())
            targets = [
                model for model in self._models()
                if model.get("enabled") and str(model.get("name")) in active
            ]
            semaphore = asyncio.Semaphore(self._max_concurrency)
            timeout = httpx.Timeout(self._timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                async def bounded(model: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                    async with semaphore:
                        return str(model["name"]), await self._probe(client, model)

                results = await asyncio.gather(*(bounded(model) for model in targets))

            self._state.update({
                "running": False,
                "gateway_status": "running",
                "last_cycle_finished_at": self._iso(self._now()),
                "models": dict(results),
            })
            return self.snapshot()

    async def _probe(self, client: httpx.AsyncClient, model: dict[str, Any]) -> dict[str, Any]:
        """Send the smallest capability-correct request and validate its shape."""

        name = str(model["name"])
        mode = str(model.get("mode") or "chat")
        provider_model = str(model.get("provider_model") or "")
        metadata = {"dashboard_probe": "periodic"}
        if mode == "embedding":
            endpoint = "embeddings"
            payload: dict[str, Any] = {
                "model": name,
                "input": "IA Gateway periodic health check",
                "encoding_format": "float",
                "metadata": metadata,
            }
        else:
            endpoint = "chat/completions"
            payload = {
                "model": name,
                "messages": [{
                    "role": "user",
                    "content": "IA Gateway periodic health check. Reply only: OK",
                }],
                "temperature": 0,
                "max_tokens": 32,
                "stream": False,
                "metadata": metadata,
            }
            # Prevent Qwen and other Ollama reasoning models from spending the
            # entire tiny probe budget in a hidden thinking stream.
            if provider_model.startswith("ollama/"):
                payload["reasoning_effort"] = "none"

        started = time.perf_counter()
        checked_at = self._iso(self._now())
        try:
            response = await client.post(
                f"{self._base_url}/v1/{endpoint}",
                json=payload,
                headers={"X-IA-Gateway-Probe": "periodic"},
            )
            latency_ms = round((time.perf_counter() - started) * 1000)
            response.raise_for_status()
            body = response.json()
            if mode == "embedding":
                vector = (body.get("data") or [{}])[0].get("embedding", [])
                valid = bool(vector) and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in vector
                )
                if not valid:
                    raise ValueError("La respuesta no contiene un embedding numérico válido")
            elif not isinstance(body.get("choices"), list) or not body["choices"]:
                raise ValueError("La respuesta no contiene choices")
            return {
                "status": "healthy",
                "mode": mode,
                "checked_at": checked_at,
                "latency_ms": latency_ms,
                "error": None,
            }
        except Exception as exc:
            return {
                "status": "error",
                "mode": mode,
                "checked_at": checked_at,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": str(exc)[:500],
            }
