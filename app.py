"""Capa HTTP del panel de control.

Piensa en este módulo como el *adaptador de entrada* de la arquitectura: traduce
acciones humanas y peticiones HTTP a operaciones del dominio (`GatewayManager`).
No conoce cómo se lanza un proceso ni cómo se filtra YAML; delegar esas decisiones
mantiene los endpoints pequeños, comprobables y fáciles de leer.
"""

from __future__ import annotations

# Este bloque de arranque usa exclusivamente la biblioteca estándar. Cloudera
# puede ejecutar directamente ``python app.py`` sobre una sesión nueva, sin una
# fase previa de construcción; por eso materializamos primero las dependencias
# declaradas y sólo después importamos FastAPI, LiteLLM y el código del gateway.
import os
from pathlib import Path
import subprocess
import sys


# Un script normal dispone de ``__file__``; una Cloudera Session que evalúa el
# código como celda no. En ese segundo caso, Cloudera sitúa el proceso en el
# directorio del proyecto, de modo que ``cwd`` es la referencia correcta.
SOURCE_FILE = globals().get("__file__")
BOOTSTRAP_ROOT = (
    Path(SOURCE_FILE).resolve().parent
    if SOURCE_FILE
    else Path.cwd().resolve()
)
REQUIREMENTS_FILE = BOOTSTRAP_ROOT / "requirements.txt"
VENV_DIR = BOOTSTRAP_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_runtime_requirements(python: Path) -> None:
    """Instala el contrato de dependencias con el intérprete privado indicado.

    La lista de argumentos evita tanto el shell como preguntas interactivas.
    Ante cualquier fallo se aborta: arrancar un panel parcialmente instalado
    produciría después errores de importación mucho menos explicativos.
    """
    if not REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"No se encuentra el fichero de dependencias: {REQUIREMENTS_FILE}")
    pip_environment = os.environ.copy()
    # Algunas imágenes CML exportan rutas, constraints y PIP_USER=1 para
    # proteger/cohesionar su Python base. Dentro de nuestro venv provocarían
    # que pip viera MLflow como instalado o intentara respetar sus versiones.
    # Conservamos las variables de índice/proxy que dan acceso al repositorio.
    for inherited_name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PIP_CONSTRAINT",
        "PIP_BUILD_CONSTRAINT",
        "PIP_PREFIX",
        "PIP_TARGET",
    ):
        pip_environment.pop(inherited_name, None)
    pip_environment["PIP_USER"] = "0"
    pip_environment["PYTHONNOUSERSITE"] = "1"
    pip_environment["VIRTUAL_ENV"] = str(VENV_DIR)
    pip_environment["PATH"] = (
        str(python.parent) + os.pathsep + pip_environment.get("PATH", "")
    )
    try:
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-user",
                "--quiet",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                str(REQUIREMENTS_FILE),
            ],
            cwd=BOOTSTRAP_ROOT,
            env=pip_environment,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return_code = getattr(exc, "returncode", "no disponible")
        raise RuntimeError(
            f"No se pudieron instalar las dependencias de {REQUIREMENTS_FILE} "
            f"(código de salida: {return_code})"
        ) from exc


def bootstrap_private_environment() -> None:
    """Aísla la aplicación del Python administrado por Cloudera.

    Las imágenes de Cloudera incluyen MLflow y versiones fijadas de bibliotecas
    comunes. Instalar LiteLLM sobre ese entorno puede dejar, por ejemplo,
    ``pydantic_core`` y ``typing_extensions`` en ubicaciones incompatibles. La
    primera ejecución crea ``.venv``, instala el contrato dentro y reemplaza el
    proceso actual por ``.venv/bin/python app.py``. Dentro del entorno privado
    sólo se verifica/actualiza el contrato y el arranque continúa normalmente.
    """
    if sys.version_info < (3, 11):
        raise RuntimeError(
            f"IA Gateway requiere Python 3.11 o superior; se está usando {sys.version.split()[0]}"
        )

    running_inside_private_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    if running_inside_private_venv:
        install_runtime_requirements(Path(sys.executable))
        return

    if not VENV_PYTHON.is_file():
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(VENV_DIR)],
                cwd=BOOTSTRAP_ROOT,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return_code = getattr(exc, "returncode", "no disponible")
            raise RuntimeError(
                f"No se pudo crear el entorno privado {VENV_DIR} "
                f"(código de salida: {return_code})"
            ) from exc

    install_runtime_requirements(VENV_PYTHON)
    application_file = BOOTSTRAP_ROOT / "app.py"
    if not application_file.is_file():
        raise RuntimeError(
            "Cloudera ejecutó el código como celda, pero no se encuentra app.py "
            f"en el directorio del proyecto: {BOOTSTRAP_ROOT}"
        )
    # Cloudera puede inyectar rutas del runtime base. No deben adelantarse a las
    # del venv recién creado, pues reproducirían la mezcla que queremos evitar.
    runtime_environment = os.environ.copy()
    runtime_environment.pop("PYTHONHOME", None)
    runtime_environment.pop("PYTHONPATH", None)
    runtime_environment["PYTHONNOUSERSITE"] = "1"
    runtime_environment["VIRTUAL_ENV"] = str(VENV_DIR)
    runtime_environment["PATH"] = (
        str(VENV_PYTHON.parent)
        + os.pathsep
        + runtime_environment.get("PATH", "")
    )
    # Sustituir, en vez de crear otro hijo, conserva señales y código de salida.
    os.execve(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), str(application_file)],
        runtime_environment,
    )


bootstrap_private_environment()

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import hmac
import re
import secrets
import time
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gateway.core import GatewayManager, environment_port
from gateway.cloudera import ClouderaCatalog
from gateway.excel_export import build_logs_xlsx
from gateway.log_store import available_days, log_kpis, parse_day, read_day_logs
from gateway.auth import AuthStore, AuthenticationError, LoginRateLimited
from gateway.tls import ensure_self_signed_certificate


ROOT = BOOTSTRAP_ROOT
# Las claves permanecen fuera del YAML y de Git, pero se heredan al proxy hijo.
load_dotenv(ROOT / ".env")
manager = GatewayManager(ROOT)
cloudera = ClouderaCatalog(ROOT / "runtime")
auth = AuthStore(ROOT / "runtime")


def refresh_cloudera_tokens() -> None:
    """Renueva secretos y reinicia el proxy sólo cuando cambió alguno."""
    results = cloudera.renew_due_tokens()
    if any(item.get("renewed") for item in results) and manager.process_alive():
        manager.stop()
        manager.start()


async def cloudera_token_supervisor() -> None:
    """Reintenta cada minuto; una incidencia aislada no detiene el supervisor."""
    while True:
        try:
            await asyncio.to_thread(refresh_cloudera_tokens)
        except Exception:
            # El detalle queda persistido por ClouderaCatalog y visible en UI.
            pass
        await asyncio.sleep(60)


def gateway_auth_headers() -> dict[str, str]:
    """Autentica el salto panel→LiteLLM sin revelar la clave al navegador."""
    master_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if not master_key:
        raise RuntimeError("Falta LITELLM_MASTER_KEY en el fichero .env")
    return {"Authorization": f"Bearer {master_key}"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Garantiza que el proxy hijo no sobreviva accidentalmente al panel."""
    supervisor = asyncio.create_task(cloudera_token_supervisor())
    try:
        yield
    finally:
        supervisor.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor
        # El proxy es hijo del panel y no debe quedar huérfano al cerrar la app.
        manager.stop()


app = FastAPI(title="IA Gateway", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def _security_headers(response: Response) -> Response:
    """Aplica las mismas defensas incluso a respuestas 401/403 tempranas."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.middleware("http")
async def secure_dashboard(request: Request, call_next):
    """Protege la API, valida CSRF y añade cabeceras defensivas al navegador."""
    path = request.url.path
    public_api = path in {"/api/auth/status", "/api/auth/login"}
    if path.startswith("/api/") and not public_api:
        session = auth.session(request.cookies.get(auth.cookie_name))
        if not session:
            return _security_headers(JSONResponse({"detail": "Autenticación requerida"}, status_code=401))
        if auth.public_status(session)["must_change_password"] and path != "/api/auth/change-password":
            return _security_headers(JSONResponse({"detail": "Debes cambiar la contraseña inicial"}, status_code=403))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied and request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
                # Algunos proxies administrados eliminan cabeceras X-* no
                # registradas. El mismo token sincronizado puede viajar en el
                # cuerpo JSON sin aparecer en URLs ni logs de acceso.
                try:
                    payload = await request.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    supplied = str(payload.get("_csrf_token", ""))
            if not hmac.compare_digest(supplied, str(session.get("csrf_token", ""))):
                return _security_headers(JSONResponse({"detail": "Token de seguridad CSRF no válido"}, status_code=403))
        request.state.auth_session = session
    response = await call_next(request)
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return _security_headers(response)


class LoginRequest(BaseModel):
    """Credenciales efímeras recibidas exclusivamente a través de HTTPS."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    """Cambio autenticado: exige conocer la contraseña vigente."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _set_session_cookie(response: Response, token: str) -> None:
    """La cookie no es visible a JavaScript ni viaja nunca por HTTP."""
    response.set_cookie(
        auth.cookie_name, token, max_age=auth.session_seconds, secure=True,
        httponly=True, samesite="strict", path="/",
    )


@app.get("/api/auth/status")
def authentication_status(request: Request):
    """Permite a la SPA decidir entre login y panel sin exponer la cookie."""
    return auth.public_status(auth.session(request.cookies.get(auth.cookie_name)))


@app.post("/api/auth/login")
def login(credentials: LoginRequest, request: Request):
    """Abre una sesión opaca y limita intentos repetidos por dirección origen."""
    address = request.client.host if request.client else "unknown"
    try:
        token, status = auth.login(credentials.username, credentials.password, address)
    except LoginRateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "900"}) from exc
    except AuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    response = JSONResponse(status)
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/change-password")
def change_password(update: PasswordChange, request: Request):
    """Rota contraseña, versión de credenciales, sesión y token CSRF."""
    session = request.state.auth_session
    try:
        auth.change_password(session, update.current_password, update.new_password)
    except AuthenticationError as exc:
        raise HTTPException(422, str(exc)) from exc
    session["csrf_token"] = secrets.token_urlsafe(32)
    token = auth.replace_session_after_password_change(session)
    response = JSONResponse(auth.public_status(session))
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request):
    """Revoca la sesión en servidor y elimina la cookie del navegador."""
    auth.logout(request.cookies.get(auth.cookie_name))
    response = Response(status_code=204)
    response.delete_cookie(auth.cookie_name, path="/", secure=True, httponly=True, samesite="strict")
    return response


class ModelState(BaseModel):
    """Contrato mínimo para activar o desactivar un alias desde la interfaz."""

    enabled: bool


class TestCall(BaseModel):
    """Texto introducido en «Prueba rápida»; el backend decide chat o embedding."""

    prompt: str


class ConfigUpdate(BaseModel):
    """Edición completa del YAML y decisión de aplicarla inmediatamente."""

    content: str
    restart: bool = True


class ModelCreate(BaseModel):
    """Representación segura del formulario CRUD de modelos.

    ``api_key_env`` contiene el *nombre* de una variable, nunca el secreto. Los
    campos vacíos se omiten al serializar para no imponer opciones innecesarias
    a LiteLLM. ``restart`` permite guardar varios cambios y aplicarlos juntos.
    """

    model_name: str
    model: str
    api_base: str = ""
    api_key_env: str = ""
    reasoning_effort: str = ""
    keep_alive: str = ""
    timeout: Optional[float] = None
    fallback_model: str = ""
    drop_params: bool = True
    source: str = ""
    cloudera_kind: str = ""
    restart: bool = True


class GuardrailUpdate(BaseModel):
    """Política transversal: avisar y continuar (`warn`) o bloquear (`block`)."""

    enabled: bool
    model: str = ""
    policy: str = "warn"
    restart: bool = True


class ClouderaConnection(BaseModel):
    """Datos de conexión y renovación para una instalación Cloudera.

    El modelo admite Public Cloud y Private Cloud/on-premise. Los campos de
    contraseña, clave privada y token sólo viajan navegador→servidor y jamás se
    devuelven en las respuestas de lectura.
    """

    name: str = ""
    kind: str
    url: str
    token: str = ""
    platform: str = "cloud"
    probe_interval_minutes: int = 5
    workload_user: str = ""
    workload_password: str = ""
    cdp_access_key_id: str = ""
    cdp_private_key: str = ""
    renewal_url: str = ""
    workload_name: str = "DE"


class ClouderaTokenRenewal(BaseModel):
    """Permite forzar una renovación aunque todavía queden más de diez minutos."""

    force: bool = False


class ClouderaModelToken(BaseModel):
    """Credencial opcional y específica de un endpoint descubierto."""

    token: str


class ClouderaModelProbe(BaseModel):
    """Metadatos necesarios para reproducir el contrato publicado por Cloudera."""

    external_id: str
    url: str
    protocol: str
    model_name: str = ""
    task: str = ""
    has_chat_template: bool = True


def _model_entry(model: ModelCreate) -> dict[str, object]:
    """Traduce el formulario a YAML sin aceptar claves secretas en claro."""
    name = model.model_name.strip()
    provider_model = model.model.strip()
    if not name or not provider_model:
        raise RuntimeError("Alias y modelo LiteLLM son obligatorios")
    if model.api_key_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", model.api_key_env.strip()):
        raise RuntimeError("La variable API key debe tener formato MAYUSCULAS_CON_GUIONES_BAJOS")
    params: dict[str, object] = {"model": provider_model, "drop_params": model.drop_params}
    if model.api_base.strip(): params["api_base"] = model.api_base.strip()
    if model.api_key_env.strip(): params["api_key"] = f"os.environ/{model.api_key_env.strip()}"
    if model.reasoning_effort: params["reasoning_effort"] = model.reasoning_effort
    if model.keep_alive.strip(): params["keep_alive"] = model.keep_alive.strip()
    if model.timeout is not None: params["timeout"] = model.timeout
    entry: dict[str, object] = {"model_name": name, "litellm_params": params}
    if model.source == "cloudera":
        entry["model_info"] = {
            "dashboard_source": "cloudera",
            "dashboard_cloudera_kind": "workbench" if model.cloudera_kind == "workbench" else "inference",
        }
    return entry


@app.get("/", include_in_schema=False)
def home():
    """Entrega la SPA estática; el resto de recursos cuelgan de ``/static``."""

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


@app.get("/api/config")
def get_config():
    """Entrega el YAML fuente; nunca expande variables de entorno ni secretos."""
    return {"content": manager.config_text(), "dashboard_settings": manager.dashboard_settings()}


@app.get("/api/cloudera/connections")
def cloudera_connections():
    """Lista conexiones saneadas: muestra presencia/caducidad, nunca secretos."""

    return cloudera.connections()


@app.post("/api/cloudera/connections")
def save_cloudera_connection(connection: ClouderaConnection):
    """Valida, normaliza y persiste una nueva conexión Cloudera."""

    try:
        return cloudera.save_connection(connection.name, connection.kind, connection.url, connection.token,
            connection.platform, connection.probe_interval_minutes, connection.workload_user,
            connection.workload_password, connection.cdp_access_key_id, connection.cdp_private_key,
            connection.renewal_url, connection.workload_name)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/cloudera/connections/{connection_id}")
def edit_cloudera_connection(connection_id: str, connection: ClouderaConnection):
    """Actualiza una conexión conservando los secretos cuyos campos estén vacíos."""

    try:
        return cloudera.update_connection(connection_id, connection.name, connection.kind, connection.url, connection.token,
            connection.platform, connection.probe_interval_minutes, connection.workload_user,
            connection.workload_password, connection.cdp_access_key_id, connection.cdp_private_key,
            connection.renewal_url, connection.workload_name)
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/cloudera/connections/{connection_id}/renew-token")
def renew_cloudera_token(connection_id: str, request: ClouderaTokenRenewal):
    """Renueva un CDP token y recarga LiteLLM si la credencial cambió."""

    try:
        result = cloudera.renew_token(connection_id, force=request.force)
        if result["renewed"] and manager.process_alive():
            manager.stop(); manager.start()
            result["litellm_restarted"] = True
        return result
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/cloudera/connections/{connection_id}", status_code=204)
def delete_cloudera_connection(connection_id: str):
    """Elimina conexión y tokens por-modelo asociados a su identificador."""

    cloudera.delete_connection(connection_id)


@app.post("/api/cloudera/connections/{connection_id}/discover")
def discover_cloudera(connection_id: str):
    """Consulta APIs Cloudera y normaliza endpoints heterogéneos para la UI."""

    try:
        return cloudera.discover(connection_id)
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.put("/api/cloudera/connections/{connection_id}/models/{external_id}/token")
def save_cloudera_model_token(connection_id: str, external_id: str, credential: ClouderaModelToken):
    """Guarda una credencial particular cuando el CDP token general no basta."""

    if not credential.token.strip(): raise HTTPException(422, "El token del modelo está vacío")
    try:
        variable = cloudera.save_model_token(connection_id, external_id, credential.token)
        return {"api_key_env": variable, "saved": True,
                **cloudera.model_token_status(connection_id, external_id)}
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/cloudera/connections/{connection_id}/probe-model")
def probe_cloudera_model(connection_id: str, model: ClouderaModelProbe):
    """Realiza una prueba mínima con la URL y el tipo de tarea descubiertos."""

    try:
        return cloudera.probe_model(connection_id, model.external_id, model.url, model.protocol,
                                    model.model_name, model.task, model.has_chat_template)
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config/guardrail")
def update_guardrail(update: GuardrailUpdate):
    """Cambia el clasificador global y su política, con reinicio transaccional."""

    try:
        return manager.set_guardrail(update.enabled, update.model.strip(), update.policy, restart=update.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config")
def update_config(update: ConfigUpdate):
    """Valida y aplica la edición completa con reinicio transaccional."""
    try:
        return manager.update_config(update.content, restart=update.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/validate")
def validate_config(update: ConfigUpdate):
    """Valida sin escribir ni reiniciar, útil para el editor avanzado."""
    try:
        return manager.validate_config_text(update.content)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/apply")
def apply_config():
    """Aplica una configuración que se guardó posponiendo el reinicio."""
    try:
        return manager.apply_pending_config()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/config/models")
def create_model(model: ModelCreate):
    """Construye una entrada LiteLLM sin aceptar secretos en claro."""
    try:
        entry = _model_entry(model)
        if not model.fallback_model.strip() and model.restart:
            return manager.add_model(entry)
        return manager.add_model(entry,
                                 fallback_model=model.fallback_model.strip(), restart=model.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config/models/{name}")
def edit_model(name: str, model: ModelCreate):
    """Edita campos conocidos conservando parámetros avanzados ajenos al formulario."""
    try:
        entry = _model_entry(model)
        return manager.update_model(name, entry, model.fallback_model.strip(), model.restart)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/config/models/{name}")
def remove_model(name: str, restart: bool = Query(True)):
    """Borra un alias y limpia fallbacks, estado y referencias de guardrail."""

    try:
        return manager.delete_model(name, restart=restart)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


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
            response = await client.post(
                f"http://{manager.host}:{manager.port}/v1/{endpoint}",
                json=payload,
                headers=gateway_auth_headers(),
            )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
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


@app.post("/api/models/{name}/latency")
async def model_latency(name: str):
    """Realiza una única sonda generativa y devuelve su latencia extremo a extremo."""
    model = next((item for item in manager.models() if item["name"] == name), None)
    if model is None:
        raise HTTPException(404, f"Modelo no encontrado: {name}")
    if model["provider_model"].startswith("ollama/"):
        raise HTTPException(400, "La sonda de latencia automática sólo se aplica a modelos remotos")
    if not model["enabled"]:
        raise HTTPException(409, f"El modelo {name} está desactivado")
    if not manager.is_running():
        raise HTTPException(409, "LiteLLM no está activo")

    payload = {
        "model": name,
        "messages": [{"role": "user", "content": "Responde únicamente: OK"}],
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"http://{manager.host}:{manager.port}/v1/chat/completions",
                json=payload,
                headers=gateway_auth_headers(),
            )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f"No se pudo medir la latencia: {exc}") from exc
    latency_ms = round((time.monotonic() - started) * 1000)
    if response.is_error:
        try:
            detail = response.json().get("error", response.json())
        except ValueError:
            detail = response.text
        raise HTTPException(response.status_code, detail)
    return {"model": name, "latency_ms": latency_ms}


@app.get("/api/models/{name}/logs")
def logs(name: str, day: Optional[str] = None, limit: int = Query(500, ge=1, le=5000)):
    """Consulta eventos estructurados con un límite defensivo de filas."""
    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rows = read_day_logs(ROOT / "runtime", name, selected, limit)
    return {"day": selected.isoformat(), "rows": rows, "kpis": log_kpis(rows)}


@app.get("/api/models/{name}/log-days")
def log_days(name: str):
    """Enumera jornadas disponibles para un alias, de más reciente a más antigua."""

    return {"days": available_days(ROOT / "runtime", name)}


@app.get("/api/models/{name}/logs.xlsx")
def export_logs(name: str, day: Optional[str] = None):
    """Genera en memoria el Excel operativo de una jornada y un modelo."""

    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rows = read_day_logs(ROOT / "runtime", name, selected, 50_000)
    content = build_logs_xlsx(name, selected.isoformat(), rows, log_kpis(rows))
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", f"logs-{name}-{selected.isoformat()}.xlsx")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/process-log", response_class=PlainTextResponse)
def process_log(day: Optional[str] = None):
    """Muestra stdout/stderr de LiteLLM para diagnosticar fallos de arranque."""
    try:
        return manager.process_log(500, parse_day(day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/process-log-days")
def process_log_days():
    """Enumera ficheros diarios de stdout/stderr del proceso LiteLLM."""

    return {"days": manager.process_log_days()}


@app.delete("/api/process-log", status_code=204)
def clear_process_log(day: Optional[str] = None):
    """Vacía sólo el log técnico elegido; no borra trazas estructuradas."""

    try:
        manager.clear_process_log(parse_day(day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    web_port = environment_port("CDSW_APP_PORT", 8081)
    if web_port == manager.port:
        raise RuntimeError("CDSW_APP_PORT y CDSW_READONLY_PORT deben usar puertos distintos")
    # Cloudera exige un servicio HTTP sobre loopback y termina TLS en su proxy.
    # En local no existe ese proxy, de modo que Uvicorn sirve HTTPS directamente.
    cloudera_proxy = bool(os.environ.get("CDSW_DOMAIN", "").strip())
    tls: dict[str, str] = {}
    if not cloudera_proxy:
        certificate, private_key = ensure_self_signed_certificate(ROOT / "runtime")
        tls = {"ssl_certfile": str(certificate), "ssl_keyfile": str(private_key)}
    uvicorn.run("app:app", host="127.0.0.1", port=web_port, reload=False, **tls)
