"""Capa HTTP del panel de control.

Piensa en este módulo como el *adaptador de entrada* de la arquitectura: traduce
acciones humanas y peticiones HTTP a operaciones del dominio (`GatewayManager`).
No conoce cómo se lanza un proceso ni cómo se filtra YAML; delegar esas decisiones
mantiene los endpoints pequeños, comprobables y fáciles de leer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gateway.core import GatewayManager
from gateway.log_store import read_logs


ROOT = Path(__file__).resolve().parent
# Las claves permanecen fuera del YAML y de Git, pero se heredan al proxy hijo.
load_dotenv(ROOT / ".env")
manager = GatewayManager(ROOT)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Garantiza que el proxy hijo no sobreviva accidentalmente al panel."""
    yield
    # El proxy es hijo del panel y no debe quedar huérfano al cerrar la app.
    manager.stop()


app = FastAPI(title="IA Gateway", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def disable_dashboard_cache(request, call_next):
    """Evita que el navegador mezcle HTML nuevo con JS/CSS de otra versión."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


class ModelState(BaseModel):
    enabled: bool


class TestCall(BaseModel):
    prompt: str


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    """Devuelve salud real y telemetría; un PID por sí solo no implica servicio."""
    return manager.status()


@app.get("/api/models")
def models():
    """Expone la vista segura de modelos, nunca las API keys del YAML."""
    try:
        return manager.models()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/model-resources")
def model_resources():
    """Separa métricas locales de proveedores remotos no observables."""
    return manager.model_resources()


@app.post("/api/gateway/start")
def start():
    """Arranca el gateway sólo después de validar configuración y secretos."""
    try:
        return manager.start()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/gateway/stop")
def stop():
    """Detiene LiteLLM y sus procesos descendientes."""
    return manager.stop()


@app.put("/api/models/{name}/state")
def set_model_state(name: str, state: ModelState):
    """Cambia el estado efectivo de un alias sin editar `config.yaml`."""
    try:
        return manager.set_model(name, state.enabled)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/models/{name}/test")
async def test_model(name: str, test: TestCall):
    """Ejecuta una prueba extremo a extremo por el mismo gateway que usa producción."""
    prompt = test.prompt.strip()
    if not prompt:
        raise HTTPException(422, "Escribe un texto para realizar la prueba")
    if len(prompt) > 50_000:
        raise HTTPException(422, "La prueba no puede superar los 50.000 caracteres")
    model = next((item for item in manager.models() if item["name"] == name), None)
    if model is None:
        raise HTTPException(404, f"Modelo no encontrado: {name}")
    if not model["enabled"]:
        raise HTTPException(409, f"El modelo {name} está desactivado")
    if not manager.is_running():
        raise HTTPException(409, "Arranca LiteLLM antes de realizar la prueba")

    # Un embedding no es un chat: elegir el endpoint según la capacidad evita
    # pruebas engañosas y permite mantener una única interfaz en el navegador.
    endpoint = "embeddings" if model["mode"] == "embedding" else "chat/completions"
    payload = (
        {"model": name, "input": prompt}
        if model["mode"] == "embedding"
        else {"model": name, "messages": [{"role": "user", "content": prompt}]}
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"http://127.0.0.1:4000/v1/{endpoint}", json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"No se pudo conectar con LiteLLM: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text}
    if response.is_error:
        detail = body.get("error", body) if isinstance(body, dict) else body
        raise HTTPException(response.status_code, detail)
    return {"mode": model["mode"], "result": body}


@app.get("/api/models/{name}/logs")
def logs(name: str, limit: int = Query(100, ge=1, le=500)):
    """Consulta eventos estructurados con un límite defensivo de filas."""
    return read_logs(ROOT / "runtime" / "requests.sqlite3", name, limit)


@app.get("/api/process-log", response_class=PlainTextResponse)
def process_log():
    """Muestra stdout/stderr de LiteLLM para diagnosticar fallos de arranque."""
    return manager.process_log(200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=5100, reload=False)
