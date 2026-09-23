"""HTTP layer for the control plane.

Piensa en este módulo como el *adaptador de entrada* de la arquitectura: traduce
acciones humanas y peticiones HTTP a operaciones del dominio (`GatewayManager`).
No conoce cómo se lanza un proceso ni cómo se filtra YAML; delegar esas decisiones
mantiene los endpoints pequeños, comprobables y fáciles de leer.
"""

from __future__ import annotations

# This bootstrap block uses only the standard library. Cloudera can execute
# ``python app.py`` directly in a fresh session without a build phase, so it
# materializes declared dependencies before importing FastAPI, LiteLLM, and the
# gateway code.
import os
from pathlib import Path
import subprocess
import sys


# A normal script has ``__file__``; a Cloudera Session evaluating code as a cell
# does not. In that case Cloudera starts the process in the project directory,
# making ``cwd`` the correct reference.
SOURCE_FILE = globals().get("__file__")
BOOTSTRAP_ROOT = (
    Path(SOURCE_FILE).resolve().parent
    if SOURCE_FILE
    else Path.cwd().resolve()
)
REQUIREMENTS_FILE = BOOTSTRAP_ROOT / "requirements.txt"
VENV_DIR = BOOTSTRAP_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def bootstrap_log(message: str) -> None:
    """Write milestones that remain visible in Cloudera's engine viewer."""
    print(f"[IA Gateway · bootstrap] {message}", flush=True)


def run_visible_command(
    command: list[str], *, cwd: Path, environment: dict[str, str], label: str
) -> None:
    """Run a command while streaming output and retaining context on failure."""
    bootstrap_log(f"Ejecutando {label}: {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"No se pudo iniciar {label}: {exc}") from exc

    last_lines: list[str] = []
    if process.stdout is not None:
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            print(f"[{label}] {line}", flush=True)
            last_lines.append(line)
            last_lines = last_lines[-30:]
    return_code = process.wait()
    if return_code:
        detail = "\n".join(last_lines) or "El comando no produjo ninguna salida"
        raise RuntimeError(
            f"{label} terminó con código {return_code}. Últimas líneas:\n{detail}"
        )


def install_runtime_requirements(python: Path) -> None:
    """Install the dependency contract with the specified private interpreter.

    La lista de argumentos evita tanto el shell como preguntas interactivas.
    Ante cualquier fallo se aborta: arrancar un panel parcialmente instalado
    produciría después errores de importación mucho menos explicativos.
    """
    if not REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"No se encuentra el fichero de dependencias: {REQUIREMENTS_FILE}")
    pip_environment = os.environ.copy()
    # Some CML images export paths, constraints, and PIP_USER=1 to protect their
    # base Python. Inside our venv those values would make pip see MLflow as
    # installed or obey its pinned versions. Keep index/proxy variables that
    # provide repository access.
    for inherited_name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PIP_CONSTRAINT",
        "PIP_BUILD_CONSTRAINT",
        "PIP_PREFIX",
        "PIP_TARGET",
        "PIP_USER",
    ):
        pip_environment.pop(inherited_name, None)
    # A runtime pip.conf may force `user = true` even when this project does not
    # request it. /dev/null (or NUL on Windows) disables those files for this
    # child only and leaves Cloudera's system configuration untouched.
    pip_environment["PIP_CONFIG_FILE"] = os.devnull
    pip_environment["PYTHONNOUSERSITE"] = "1"
    pip_environment["VIRTUAL_ENV"] = str(VENV_DIR)
    pip_environment["PATH"] = (
        str(python.parent) + os.pathsep + pip_environment.get("PATH", "")
    )
    bootstrap_log(f"Instalando dependencias con: {python}")
    bootstrap_log(f"Fichero de dependencias: {REQUIREMENTS_FILE}")
    try:
        run_visible_command(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                str(REQUIREMENTS_FILE),
            ],
            cwd=BOOTSTRAP_ROOT,
            environment=pip_environment,
            label="pip install",
        )
        bootstrap_log("Validando coherencia de dependencias con pip check")
        run_visible_command(
            [str(python), "-I", "-m", "pip", "check"],
            cwd=BOOTSTRAP_ROOT,
            environment=pip_environment,
            label="pip check",
        )
    except RuntimeError as exc:
        bootstrap_log(f"ERROR: {exc}")
        raise RuntimeError(
            f"No se pudieron preparar las dependencias de {REQUIREMENTS_FILE}. {exc}"
        ) from exc


def bootstrap_private_environment() -> None:
    """Isolate the application from Cloudera-managed Python.

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

    execution_mode = "script" if SOURCE_FILE else "celda/engine Cloudera"
    bootstrap_log(f"Modo de ejecución: {execution_mode}")
    bootstrap_log(f"Python inicial: {sys.executable} ({sys.version.split()[0]})")
    bootstrap_log(f"Directorio del proyecto: {BOOTSTRAP_ROOT}")
    running_inside_private_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    if running_inside_private_venv:
        bootstrap_log(f"Entorno privado activo: {VENV_DIR}")
        install_runtime_requirements(Path(sys.executable))
        bootstrap_log("Dependencias preparadas; cargando IA Gateway")
        return

    if not VENV_PYTHON.is_file():
        bootstrap_log(f"Creando entorno privado: {VENV_DIR}")
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
    else:
        bootstrap_log(f"Reutilizando entorno privado existente: {VENV_DIR}")

    install_runtime_requirements(VENV_PYTHON)
    application_file = BOOTSTRAP_ROOT / "app.py"
    if not application_file.is_file():
        raise RuntimeError(
            "Cloudera ejecutó el código como celda, pero no se encuentra app.py "
            f"en el directorio del proyecto: {BOOTSTRAP_ROOT}"
        )
    # Cloudera may inject base-runtime paths. They must not precede paths from
    # the new venv, or they would recreate the environment mixing being avoided.
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
    command = [str(VENV_PYTHON), str(application_file)]
    if SOURCE_FILE:
        # For a normal script, replacing the process preserves signals and exit codes.
        bootstrap_log(f"Relanzando el script con: {VENV_PYTHON}")
        os.execve(str(VENV_PYTHON), command, runtime_environment)

    # A Cloudera Application evaluates the file inside its engine. Replacing
    # that process kills the kernel and the platform only reports "Engine
    # exited". Keeping it as the parent exposes all child server logs.
    bootstrap_log(f"Iniciando IA Gateway como proceso hijo: {VENV_PYTHON}")
    try:
        completed = subprocess.run(
            command,
            cwd=BOOTSTRAP_ROOT,
            env=runtime_environment,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"No se pudo iniciar {application_file}: {exc}") from exc
    if completed.returncode:
        raise RuntimeError(
            f"IA Gateway terminó con código {completed.returncode}; "
            "revisa las líneas inmediatamente anteriores del log"
        )
    raise SystemExit(0)


bootstrap_private_environment()

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import hmac
import hashlib
import json
import re
import secrets
import time
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gateway.core import GatewayManager, environment_port
from gateway.cloudera import ClouderaCatalog
from gateway.edge import EdgeProxy, public_gateway_port
from gateway.excel_export import build_logs_xlsx
from gateway.log_store import available_days, log_kpis, log_kpis_for_day, parse_day, read_day_logs
from gateway.auth import AuthStore, AuthenticationError, LoginRateLimited
from gateway.benchmark import BenchmarkRunner
from gateway.health_monitor import ModelHealthMonitor
from gateway.tls import ensure_self_signed_certificate


ROOT = BOOTSTRAP_ROOT
# Keys stay outside YAML and Git but are inherited by the child proxy.
load_dotenv(ROOT / ".env")
PUBLIC_GATEWAY_PORT = public_gateway_port()
INTERNAL_DASHBOARD_PORT = environment_port("IA_GATEWAY_DASHBOARD_PORT", 18080)
manager = GatewayManager(ROOT)
cloudera = ClouderaCatalog(ROOT / "runtime")
auth = AuthStore(ROOT / "runtime")
benchmarks = BenchmarkRunner(ROOT / "runtime" / "benchmark-latest.json")
model_health = ModelHealthMonitor(
    models=manager.models,
    active_names=manager.active_model_names,
    is_running=manager.is_running,
    base_url=f"http://{manager.host}:{manager.port}",
    interval_seconds=300,
)
_MODEL_VALIDATIONS: dict[str, tuple[str, float]] = {}
_LOG_KPI_CACHE: dict[tuple[str, tuple[str, ...], str], tuple[float, dict[str, Any]]] = {}
_LOG_KPI_CACHE_SECONDS = 30.0

# Locally, the edge proxy also terminates TLS. Inside Cloudera that responsibility
# belongs to the platform proxy, and our listener receives HTTP over loopback.
_behind_cloudera = bool(os.environ.get("CDSW_DOMAIN", "").strip())
_edge_certificate: Path | None = None
_edge_private_key: Path | None = None
if not _behind_cloudera:
    _edge_certificate, _edge_private_key = ensure_self_signed_certificate(ROOT / "runtime")
edge = EdgeProxy(
    ROOT,
    PUBLIC_GATEWAY_PORT,
    INTERNAL_DASHBOARD_PORT,
    manager.port,
    os.environ.get("IA_GATEWAY_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1",
    _edge_certificate,
    _edge_private_key,
)


def refresh_cloudera_tokens() -> None:
    """Renew secrets and inject them dynamically into LiteLLM."""
    results = cloudera.renew_due_tokens()
    if any(item.get("renewed") for item in results) and manager.process_alive():
        apply_cloudera_credential_changes()


def apply_cloudera_credential_changes() -> dict[str, bool]:
    """Commit changes to SQLite; the callback reads them without restarting LiteLLM."""

    if not manager.process_alive():
        return {"litellm_credentials_updated": False, "litellm_restarted": False}
    manager.sync_cloudera_credentials()
    return {"litellm_credentials_updated": True, "litellm_restarted": False}


def generate_initial_cloudera_token(connection: dict[str, Any]) -> dict[str, Any]:
    """Obtain the initial credential when saving a configured renewal profile."""

    if connection.get("has_token") or not connection.get("renewal_ready"):
        return connection
    try:
        generated = cloudera.renew_token(str(connection["id"]), force=True)
    except RuntimeError as exc:
        cloudera.record_renewal_error(str(connection["id"]), str(exc))
        return {**connection, "token_generated": False, "token_generation_error": str(exc)}
    refreshed = next(
        (item for item in cloudera.connections() if item.get("id") == connection.get("id")),
        connection,
    )
    result = {**refreshed, **generated, "token_generated": True}
    if manager.process_alive():
        result.update(apply_cloudera_credential_changes())
    return result


async def cloudera_token_supervisor() -> None:
    """Retry every minute; an isolated incident must not stop the supervisor."""
    while True:
        try:
            await asyncio.to_thread(refresh_cloudera_tokens)
        except Exception:
            # ClouderaCatalog persists the details and exposes them to the UI.
            pass
        await asyncio.sleep(60)


def gateway_auth_headers() -> dict[str, str]:
    """LiteLLM runs without its own inbound authentication in this application."""
    return {}


def client_origin_ip(request: Request) -> str:
    """Recover the client IP fixed by the edge proxy for internal calls."""

    for name in ("x-ia-gateway-client-ip", "x-envoy-external-address", "x-forwarded-for", "x-real-ip"):
        value = request.headers.get(name, "").split(",", 1)[0].strip()
        if value:
            return value
    return request.client.host if request.client else ""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Supervise the proxy, token renewal, dashboard, and LiteLLM."""
    edge.start()
    public_scheme = "http" if _behind_cloudera else "https"
    print(
        f"[IA Gateway] Entrada pública Python: "
        f"{public_scheme}://{edge.listen_host}:{PUBLIC_GATEWAY_PORT}",
        flush=True,
    )
    print(
        f"[IA Gateway] Rutas: /v1/* → LiteLLM 127.0.0.1:{manager.port}; "
        f"resto → panel 127.0.0.1:{INTERNAL_DASHBOARD_PORT}",
        flush=True,
    )
    supervisor = asyncio.create_task(cloudera_token_supervisor())
    model_health.start()
    try:
        yield
    finally:
        supervisor.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor
        await model_health.shutdown()
        await benchmarks.shutdown()
        # The proxy is a dashboard child and must not be orphaned at shutdown.
        manager.stop()
        edge.stop()


app = FastAPI(title="IA Gateway", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def _security_headers(response: Response) -> Response:
    """Apply the same defensive headers even to early 401/403 responses."""
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
    """Protect the API, validate CSRF, and add defensive browser headers."""
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
                # Some managed proxies remove unregistered X-* headers. The same
                # synchronized token can travel in JSON without appearing in
                # URLs or access logs.
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
    """Ephemeral credentials received exclusively through HTTPS."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    """Authenticated change that requires knowledge of the current password."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _set_session_cookie(response: Response, token: str) -> None:
    """The cookie is invisible to JavaScript and never travels over plain HTTP."""
    response.set_cookie(
        auth.cookie_name, token, max_age=auth.session_seconds, secure=True,
        httponly=True, samesite="strict", path="/",
    )


@app.get("/api/auth/status")
def authentication_status(request: Request):
    """Let the SPA choose login or dashboard without exposing the cookie."""
    return auth.public_status(auth.session(request.cookies.get(auth.cookie_name)))


@app.post("/api/auth/login")
def login(credentials: LoginRequest, request: Request):
    """Open an opaque session and rate-limit repeated attempts by source address."""
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
    """Rotate the password, credential version, session, and CSRF token."""
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
    """Revoke the server-side session and remove the browser cookie."""
    auth.logout(request.cookies.get(auth.cookie_name))
    response = Response(status_code=204)
    response.delete_cookie(auth.cookie_name, path="/", secure=True, httponly=True, samesite="strict")
    return response


class ModelState(BaseModel):
    """Minimum contract for enabling or disabling an alias from the UI."""

    enabled: bool


class TestCall(BaseModel):
    """Text entered in Quick test; the backend chooses chat or embeddings."""

    prompt: str


class BenchmarkTarget(BaseModel):
    """Model and concurrency ceiling to traverse from 1 through N."""

    model: str = Field(min_length=1, max_length=128)
    max_concurrency: int = Field(ge=1, le=32)


class BenchmarkRequest(BaseModel):
    """Bounded configuration for a comparable, cancellable campaign."""

    targets: list[BenchmarkTarget] = Field(min_length=1, max_length=8)
    limit_mode: str = "requests"
    requests_per_model: int = Field(default=50, ge=1, le=10_000)
    duration_seconds: int = Field(default=60, ge=5, le=100_000)
    strategy: str = "parallel"
    request_timeout_seconds: int = Field(default=120, ge=5, le=300)
    warmup: bool = True


class ConfigUpdate(BaseModel):
    """Complete YAML edit and a decision whether to apply it immediately."""

    content: str
    restart: bool = True


class ModelCreate(BaseModel):
    """Safe representation of the model CRUD form.

    ``api_key_env`` contiene el *nombre* de una variable, nunca el secreto. Los
    campos vacíos se omiten al serializar para no imponer opciones innecesarias
    a LiteLLM. ``restart`` permite guardar varios cambios y aplicarlos juntos.
    """

    model_name: str
    model: str
    api_base: str = ""
    api_key_env: str = ""
    backend_profile: str = "auto"
    compatibility_profile: str = "auto"
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    default_max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    seed: Optional[int] = None
    stop: list[str] = Field(default_factory=list)
    reasoning_mode: str = "auto"
    reasoning_effort: str = ""
    preserve_thinking: bool = False
    num_retries: Optional[int] = None
    max_parallel_requests: Optional[int] = None
    keep_alive: str = ""
    timeout: Optional[float] = None
    fallback_model: str = ""
    drop_params: bool = True
    parameter_policy: str = "caller_wins"
    extra_parameters: dict[str, Any] = Field(default_factory=dict)
    validation_payload: dict[str, Any] = Field(default_factory=dict)
    validation_path: str = ""
    validation_id: str = ""
    source: str = ""
    cloudera_kind: str = ""
    serving_engine: str = ""
    task: str = ""
    embedding_input_type: str = ""
    remote_model: str = ""
    restart: bool = True


class GuardrailUpdate(BaseModel):
    """Cross-cutting policy: warn and continue (``warn``) or block (``block``)."""

    enabled: bool
    model: str = ""
    policy: str = "warn"
    excluded_models: list[str] = Field(default_factory=list)
    restart: bool = True


class AdvisorUpdate(BaseModel):
    """Model that suggests parameters but never applies them without confirmation."""

    enabled: bool
    model: str = ""
    restart: bool = True


class ModelAdviceRequest(BaseModel):
    """Use case that the advisor turns into a structured proposal."""

    model_name: str
    use_case: str


def _advisor_json(content: str) -> dict[str, Any]:
    """Accept raw JSON or an object surrounded by model Markdown/thinking text."""

    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for position, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(cleaned[position:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No se encontró un objeto JSON en la respuesta")


class ClouderaConnection(BaseModel):
    """Connection and renewal data for a Cloudera installation.

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
    onpremise_version: str = "legacy"
    credential_expires_at: str = ""
    cai_version: str = "1.5.5_sp3"
    onpremise_auth_mode: str = ""
    credential_type: str = "cdp_token"
    tls_verification: str = "system"
    tls_ca_pem: str = ""
    workbench_mode: str = "catalog"
    workbench_model_type: str = "chat"
    workbench_input_type: str = "passage"
    workbench_api_key: str = ""
    workbench_app_model: str = ""
    workbench_app_auth_mode: str = "public"


class ClouderaTokenRenewal(BaseModel):
    """Allow forced renewal even when more than ten minutes remain."""

    force: bool = False


class ClouderaModelToken(BaseModel):
    """Optional credential dedicated to one discovered endpoint."""

    token: str


class ClouderaModelProbe(BaseModel):
    """Metadata required to reproduce the contract published by Cloudera."""

    external_id: str
    url: str
    protocol: str
    model_name: str = ""
    task: str = ""
    has_chat_template: bool = True
    serving_engine: str = ""
    requires_input_type: bool = False


def _model_entry(model: ModelCreate) -> dict[str, object]:
    """Translate the form to YAML without accepting plaintext secret keys."""
    name = model.model_name.strip()
    provider_model = model.model.strip()
    if model.source == "cloudera" and model.cloudera_kind != "workbench":
        # The first prefix selects the LiteLLM provider, while the remote CAI
        # identifier may also begin with `openai/`. Collapse only accidental
        # triple prefixes; two prefixes are correct for these models.
        while provider_model.lower().startswith("openai/openai/openai/"):
            provider_model = provider_model[len("openai/"):]
        remote_model = model.remote_model.strip()
        if remote_model:
            provider_model = f"openai/{remote_model}"
    if not name or not provider_model:
        raise RuntimeError("Alias y modelo LiteLLM son obligatorios")
    if model.api_key_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", model.api_key_env.strip()):
        raise RuntimeError("La variable API key debe tener formato MAYUSCULAS_CON_GUIONES_BAJOS")
    backend = model.backend_profile.strip().lower() or "auto"
    compatibility = model.compatibility_profile.strip().lower() or "auto"
    allowed_backends = {"auto", "openai", "vllm", "nim", "triton", "ollama", "workbench"}
    allowed_compatibility = {"auto", "cloudera_1_5_5_sp3", "current"}
    if backend not in allowed_backends:
        raise RuntimeError("Perfil de backend no reconocido")
    if compatibility not in allowed_compatibility:
        raise RuntimeError("Perfil de compatibilidad no reconocido")
    if model.reasoning_mode not in {"auto", "enabled", "disabled"}:
        raise RuntimeError("El modo de razonamiento debe ser automático, activado o desactivado")
    if model.reasoning_effort not in {"", "minimal", "low", "medium", "high"}:
        raise RuntimeError("Nivel de razonamiento no reconocido")
    if model.parameter_policy not in {"caller_wins", "model_wins"}:
        raise RuntimeError("La política de parámetros debe permitir al cliente o imponer el modelo")
    if len(model.extra_parameters) > 64:
        raise RuntimeError("Un modelo no puede declarar más de 64 parámetros extra")
    reserved_extra_roots = {
        "api_base", "api_key", "authorization", "headers", "input", "messages",
        "metadata", "model", "prompt", "stream",
    }
    for key in model.extra_parameters:
        parts = str(key).split(".")
        if (not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part or "") for part in parts)
                or parts[0].lower() in reserved_extra_roots):
            raise RuntimeError(f"Parámetro extra no permitido: {key}")
    if len(json.dumps(model.extra_parameters, ensure_ascii=False, default=str).encode("utf-8")) > 32_768:
        raise RuntimeError("Los parámetros extra no pueden superar 32 KB")

    effective_backend = backend
    if effective_backend == "auto":
        effective_backend = ("workbench" if model.cloudera_kind == "workbench" else
                             model.serving_engine.strip().lower() or
                             ("ollama" if provider_model.startswith("ollama/") else "openai"))

    def bounded(value: int | float | None, label: str, minimum: float,
                maximum: float | None = None) -> None:
        if value is None:
            return
        if isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
            suffix = f" y {maximum:g}" if maximum is not None else ""
            raise RuntimeError(f"{label} debe estar entre {minimum:g}{suffix}")

    bounded(model.context_window, "La ventana de contexto", 1)
    bounded(model.max_output_tokens, "La capacidad máxima de salida", 1)
    bounded(model.default_max_tokens, "La salida predeterminada", 1)
    bounded(model.temperature, "Temperature", 0, 2)
    bounded(model.top_p, "Top P", 0, 1)
    bounded(model.top_k, "Top K", -1)
    bounded(model.min_p, "Min P", 0, 1)
    bounded(model.repetition_penalty, "Repetition penalty", 0, 2)
    bounded(model.frequency_penalty, "Frequency penalty", -2, 2)
    bounded(model.presence_penalty, "Presence penalty", -2, 2)
    bounded(model.seed, "Seed", 0)
    bounded(model.num_retries, "Reintentos", 0, 10)
    bounded(model.max_parallel_requests, "Concurrencia", 1)
    if (model.context_window is not None and model.max_output_tokens is not None
            and model.max_output_tokens >= model.context_window):
        raise RuntimeError("La salida máxima debe ser menor que la ventana total de contexto")
    if (model.max_output_tokens is not None and model.default_max_tokens is not None
            and model.default_max_tokens > model.max_output_tokens):
        raise RuntimeError("La salida predeterminada no puede superar la capacidad máxima de salida")

    generation_values = (
        model.default_max_tokens, model.temperature, model.top_p, model.top_k,
        model.min_p, model.repetition_penalty, model.frequency_penalty,
        model.presence_penalty, model.seed,
    )
    if effective_backend == "triton" and (any(value is not None for value in generation_values)
                                            or model.stop or model.reasoning_mode != "auto"
                                            or model.reasoning_effort):
        raise RuntimeError(
            "Triton OIP configura generación, tensores y batching en el deployment; "
            "no admite los parámetros OpenAI de este formulario"
        )
    if (compatibility == "cloudera_1_5_5_sp3" and effective_backend == "workbench"
            and model.default_max_tokens is not None and model.default_max_tokens > 512):
        raise RuntimeError("Cloudera AI Workbench 1.5.5 SP3 limita este adaptador a 512 tokens de salida")

    params: dict[str, object] = {"model": provider_model, "drop_params": model.drop_params}
    if (model.source == "cloudera" and model.serving_engine == "nim"
            and "embed" in model.task.lower()):
        # LiteLLM 1.83.9 turns omission of this field into JSON null. Embedding
        # NIMs strictly validate the float|base64 enumeration.
        params["encoding_format"] = "float"
    if model.api_base.strip(): params["api_base"] = model.api_base.strip()
    if model.api_key_env.strip(): params["api_key"] = f"os.environ/{model.api_key_env.strip()}"
    if model.default_max_tokens is not None: params["max_tokens"] = model.default_max_tokens
    if model.temperature is not None: params["temperature"] = model.temperature
    if model.top_p is not None: params["top_p"] = model.top_p
    if model.frequency_penalty is not None: params["frequency_penalty"] = model.frequency_penalty
    if model.presence_penalty is not None: params["presence_penalty"] = model.presence_penalty
    if model.seed is not None: params["seed"] = model.seed
    cleaned_stop = [value.strip() for value in model.stop if value.strip()]
    if cleaned_stop: params["stop"] = cleaned_stop
    if model.reasoning_effort: params["reasoning_effort"] = model.reasoning_effort
    if model.num_retries is not None: params["num_retries"] = model.num_retries
    if model.max_parallel_requests is not None: params["max_parallel_requests"] = model.max_parallel_requests
    if model.keep_alive.strip():
        keep_alive = model.keep_alive.strip()
        # Ollama treats JSON strings as duration expressions. Preserve values
        # such as "5m", but send numeric controls such as -1 and 0 as numbers.
        params["keep_alive"] = int(keep_alive) if re.fullmatch(r"-?\d+", keep_alive) else keep_alive
    if model.timeout is not None: params["timeout"] = model.timeout

    if (effective_backend not in {"vllm", "nim", "workbench", "ollama"}
            and any(value is not None for value in
                    (model.top_k, model.min_p, model.repetition_penalty))):
        raise RuntimeError("Top K, Min P y repetition penalty requieren un backend vLLM, NIM, Ollama o Workbench")
    extra_body: dict[str, object] = {}
    if effective_backend in {"vllm", "nim", "workbench", "ollama"}:
        for key, value in {
            "top_k": model.top_k, "min_p": model.min_p,
            "repetition_penalty": model.repetition_penalty,
        }.items():
            if value is not None:
                extra_body[key] = value
    if model.reasoning_mode != "auto":
        thinking = model.reasoning_mode == "enabled"
        if effective_backend == "workbench":
            extra_body["enable_thinking"] = thinking
            if model.preserve_thinking:
                extra_body["preserve_thinking"] = True
        elif effective_backend in {"vllm", "nim"}:
            extra_body["chat_template_kwargs"] = {"enable_thinking": thinking}
    if (effective_backend == "workbench" and "embed" in model.task.lower()
            and model.embedding_input_type in {"query", "passage"}):
        extra_body["input_type"] = model.embedding_input_type
        extra_body["normalize"] = True
    if extra_body:
        params["extra_body"] = extra_body

    entry: dict[str, object] = {"model_name": name, "litellm_params": params}
    model_info: dict[str, object] = {}
    if backend != "auto":
        model_info["dashboard_backend_profile"] = backend
    if compatibility != "auto":
        model_info["dashboard_compatibility_profile"] = compatibility
    if model.reasoning_mode != "auto":
        model_info["dashboard_reasoning_mode"] = model.reasoning_mode
    if model.preserve_thinking:
        model_info["dashboard_preserve_thinking"] = True
    model_info["dashboard_parameter_policy"] = model.parameter_policy
    if model.extra_parameters:
        model_info["dashboard_extra_parameters"] = model.extra_parameters
    if model.context_window is not None:
        model_info["dashboard_context_window"] = model.context_window
        model_info["max_input_tokens"] = (model.context_window - model.max_output_tokens
                                          if model.max_output_tokens is not None else model.context_window)
    if model.max_output_tokens is not None:
        model_info["max_output_tokens"] = model.max_output_tokens
    if model.reasoning_mode != "auto":
        model_info["supports_reasoning"] = model.reasoning_mode == "enabled"
    if model.source == "cloudera":
        model_info.update({
            "dashboard_source": "cloudera",
            "dashboard_cloudera_kind": (model.cloudera_kind
                                         if model.cloudera_kind in {"inference", "workbench", "workbench_app"}
                                         else "inference"),
            "dashboard_serving_engine": model.serving_engine.strip(),
            "dashboard_task": model.task.strip(),
            "dashboard_embedding_input_type": model.embedding_input_type.strip(),
        })
        if model.remote_model.strip():
            model_info["dashboard_remote_model"] = model.remote_model.strip()
    if model_info:
        entry["model_info"] = model_info
    # The manager removes this marker before writing YAML. It distinguishes the
    # complete form from legacy clients that send minimal patches.
    entry["_dashboard_managed_parameters"] = True
    return entry


def _model_fingerprint(entry: dict[str, object]) -> str:
    """Identify exactly which configuration passed the live probe."""

    clean = json.loads(json.dumps(
        {key: value for key, value in entry.items() if not key.startswith("_dashboard_")},
        ensure_ascii=False, default=str,
    ))
    info = clean.get("model_info") or {}
    info.pop("dashboard_validated_at", None)
    info.pop("dashboard_validation_fingerprint", None)
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _register_model_validation(entry: dict[str, object]) -> str:
    token = secrets.token_urlsafe(24)
    _MODEL_VALIDATIONS[token] = (_model_fingerprint(entry), time.monotonic() + 900)
    return token


def _consume_model_validation(entry: dict[str, object], token: str) -> None:
    expected = _MODEL_VALIDATIONS.pop(token, None)
    if not expected or expected[1] < time.monotonic() or not hmac.compare_digest(expected[0], _model_fingerprint(entry)):
        raise RuntimeError("Prueba el modelo con todos sus parámetros antes de guardarlo; la validación dura 15 minutos")


def _resolved_model_api_key(params: dict[str, Any]) -> str:
    reference = str(params.get("api_key") or "")
    if not reference.startswith("os.environ/"):
        return reference
    name = reference.removeprefix("os.environ/")
    return os.environ.get(name, "") or cloudera.environment().get(name, "")


def _candidate_parameters(entry: dict[str, Any]) -> dict[str, Any]:
    """Reproduce callback merging so the probe uses identical values."""

    params = dict(entry.get("litellm_params") or {})
    configured = {
        key: value for key, value in params.items()
        if key not in {"api_base", "api_key", "drop_params", "max_parallel_requests",
                       "model", "num_retries", "timeout"}
    }
    for dotted, value in ((entry.get("model_info") or {}).get("dashboard_extra_parameters") or {}).items():
        target = configured
        parts = str(dotted).split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return configured


async def _probe_model_candidate(model: ModelCreate, entry: dict[str, Any]) -> dict[str, Any]:
    """Run minimal inference directly against the candidate deployment."""

    params = dict(entry.get("litellm_params") or {})
    info = dict(entry.get("model_info") or {})
    backend = str(info.get("dashboard_backend_profile") or model.serving_engine or "auto").lower()
    provider_model = str(params.get("model") or "")
    api_base = str(params.get("api_base") or "").rstrip("/")
    key = _resolved_model_api_key(params)
    configured = _candidate_parameters(entry)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    timeout = float(params.get("timeout") or 120)
    embedding = "embed" in f"{model.task} {provider_model}".lower() or "bge-" in provider_model.lower()

    if backend == "triton":
        if not model.validation_payload:
            raise RuntimeError("Triton requiere un JSON de prueba con sus inputs/tensores antes de activarlo")
        path = model.validation_path.strip() or f"/v2/models/{provider_model.rsplit('/', 1)[-1]}/infer"
        if not path.startswith("/") or ".." in path or "://" in path:
            raise RuntimeError("La ruta de prueba Triton debe ser relativa y comenzar por /")
        url, payload = f"{api_base}{path}", model.validation_payload
    elif provider_model.startswith("ollama/") or backend == "ollama":
        url = f"{api_base or 'http://localhost:11434'}/api/embed" if embedding else f"{api_base or 'http://localhost:11434'}/api/chat"
        payload = {"model": provider_model.removeprefix("ollama/"), "stream": False}
        if embedding:
            payload["input"] = "Prueba de configuración"
        else:
            payload["messages"] = [{"role": "user", "content": "Responde solamente OK"}]
        options = dict(configured.pop("extra_body", {}) or {})
        options.update(configured)
        if "max_tokens" in options:
            options["num_predict"] = options.pop("max_tokens")
        if options:
            payload["options"] = options
    elif provider_model.startswith("cloudera_workbench/") or backend == "workbench":
        from gateway.workbench_provider import _embedding_request_body, _endpoint_auth, _request_body
        url, body_access_key, bearer = _endpoint_auth(api_base, key or None)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        else:
            headers.pop("Authorization", None)
        if embedding:
            payload = _embedding_request_body(
                ["Prueba de configuración"],
                {**configured, "extra_body": configured.get("extra_body", {})},
            )
        else:
            payload = _request_body(
                [{"role": "user", "content": "Responde solamente OK"}],
                {**configured, "extra_body": configured.get("extra_body", {})},
            )
        if body_access_key:
            payload["accessKey"] = body_access_key
    else:
        base = api_base or "https://api.openai.com/v1"
        endpoint = "embeddings" if embedding else "chat/completions"
        url = base if base.endswith(f"/{endpoint}") else f"{base}/{endpoint}"
        payload = {"model": provider_model.removeprefix("openai/")}
        if embedding:
            payload["input"] = "Prueba de configuración"
        else:
            payload["messages"] = [{"role": "user", "content": "Responde solamente OK"}]
        extra_body = configured.pop("extra_body", {})
        payload.update(configured)
        if isinstance(extra_body, dict):
            payload.update(extra_body)

    if not url:
        raise RuntimeError("Falta API base para probar el deployment")
    tls_verify = cloudera.tls_verification_for(
        str(params.get("api_key") or ""), api_base, as_context=True,
    )
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=True if tls_verify is None else tls_verify,
        ) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise RuntimeError(f"No se pudo conectar con el modelo: {exc}") from exc
    if response.is_error:
        detail = response.text[:1500]
        raise RuntimeError(f"El modelo rechazó la prueba de parámetros (HTTP {response.status_code}): {detail}")
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("El modelo respondió, pero no devolvió JSON válido") from exc
    return {"status": response.status_code, "parameters": _candidate_parameters(entry),
            "response_type": type(body).__name__}


@app.get("/", include_in_schema=False)
def home():
    """Serve the static SPA; all remaining assets live under ``/static``."""

    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    """Return real health and telemetry; a PID alone does not imply service."""
    return {**manager.status(), "public_port": PUBLIC_GATEWAY_PORT}


@app.get("/api/models")
def models():
    """Expose the safe model view, never API keys from YAML."""
    try:
        return manager.models()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/model-resources")
def model_resources():
    """Separate local metrics from unobservable remote providers."""
    return manager.model_resources()


@app.get("/api/model-health")
def model_health_status():
    """Expose the five-minute checks without launching work from the browser."""
    return model_health.snapshot()


@app.post("/api/benchmarks")
async def start_benchmark(request: BenchmarkRequest):
    """Start stepped load against the gateway's own active aliases."""

    if request.limit_mode not in {"requests", "duration"}:
        raise HTTPException(422, "El límite debe ser por peticiones o por tiempo")
    if request.strategy not in {"parallel", "sequential"}:
        raise HTTPException(422, "La estrategia debe ser paralela o secuencial")
    target_names = [target.model.strip() for target in request.targets]
    if len(target_names) != len(set(target_names)):
        raise HTTPException(422, "Cada modelo sólo puede aparecer una vez")
    if sum(target.max_concurrency for target in request.targets) > 64:
        raise HTTPException(422, "La concurrencia agregada no puede superar 64")
    available = {item["name"]: item for item in manager.models()}
    unknown = [name for name in target_names if name not in available]
    if unknown:
        raise HTTPException(404, f"Modelos no encontrados: {', '.join(unknown)}")
    if not manager.is_running():
        raise HTTPException(409, "Arranca LiteLLM antes de iniciar la batería")
    active = set(manager.active_model_names())
    unavailable = [name for name in target_names if not available[name].get("enabled") or name not in active]
    if unavailable:
        raise HTTPException(409, f"Modelos inactivos o pendientes de aplicar: {', '.join(unavailable)}")
    if request.limit_mode == "requests":
        for target in request.targets:
            minimum = target.max_concurrency * (target.max_concurrency + 1) // 2
            if request.requests_per_model < minimum:
                raise HTTPException(
                    422,
                    f"{target.model} necesita al menos {minimum} peticiones para recorrer "
                    f"los niveles 1–{target.max_concurrency}",
                )
    else:
        highest = max(target.max_concurrency for target in request.targets)
        if request.duration_seconds < highest * 2:
            raise HTTPException(422, "Reserva al menos 2 segundos por nivel de concurrencia")
    config = request.model_dump()
    try:
        return benchmarks.start(
            config,
            [available[name] for name in target_names],
            f"http://{manager.host}:{manager.port}",
            gateway_auth_headers(),
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/benchmarks/current")
async def current_benchmark():
    """Return aggregates and bounded series for live refresh."""

    return benchmarks.snapshot()


@app.post("/api/benchmarks/current/cancel")
async def cancel_benchmark():
    """Request cooperative shutdown; in-flight requests may complete."""

    try:
        return benchmarks.cancel()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/benchmarks/current/report")
async def download_benchmark_report():
    """Download the complete visible state as reproducible evidence."""

    report = benchmarks.snapshot()
    if report.get("status") == "idle":
        raise HTTPException(404, "Todavía no hay resultados de una batería")
    filename = f"ia-gateway-benchmark-{report['id']}.json"
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/config")
def get_config():
    """Return source YAML without expanding environment variables or secrets."""
    return {"content": manager.config_text(), "dashboard_settings": manager.dashboard_settings()}


@app.get("/api/config/backup")
def download_config_backup():
    """Download an exact YAML copy without expanding variables or secrets."""

    filename = time.strftime("ia-gateway-config-%Y%m%d-%H%M%S.yaml")
    return Response(
        manager.config_text(),
        media_type="application/yaml; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/cloudera/connections")
def cloudera_connections():
    """List sanitized connections: expose presence/expiry, never secrets."""

    return cloudera.connections()


@app.post("/api/cloudera/connections")
def save_cloudera_connection(connection: ClouderaConnection):
    """Validate, normalize, and persist a new Cloudera connection."""

    try:
        result = cloudera.save_connection(connection.name, connection.kind, connection.url, connection.token,
            connection.platform, connection.probe_interval_minutes, connection.workload_user,
            connection.workload_password, connection.cdp_access_key_id, connection.cdp_private_key,
            connection.renewal_url, connection.workload_name, connection.onpremise_version,
            connection.credential_expires_at, connection.cai_version,
            connection.onpremise_auth_mode, connection.credential_type,
            connection.tls_verification, connection.tls_ca_pem,
            connection.workbench_mode, connection.workbench_model_type,
            connection.workbench_input_type, connection.workbench_api_key,
            connection.workbench_app_model, connection.workbench_app_auth_mode)
        result = generate_initial_cloudera_token(result)
        credential_changed = bool(
            connection.token.strip() or connection.workbench_api_key.strip()
            or connection.kind == "workbench_app"
            or (connection.kind == "workbench" and "accessKey=" in connection.url)
        )
        if manager.process_alive() and credential_changed and not result.get("token_generated"):
            result.update(apply_cloudera_credential_changes())
        return result
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/cloudera/connections/{connection_id}")
def edit_cloudera_connection(connection_id: str, connection: ClouderaConnection):
    """Update a connection while retaining secrets for fields left blank."""

    try:
        result = cloudera.update_connection(connection_id, connection.name, connection.kind, connection.url, connection.token,
            connection.platform, connection.probe_interval_minutes, connection.workload_user,
            connection.workload_password, connection.cdp_access_key_id, connection.cdp_private_key,
            connection.renewal_url, connection.workload_name, connection.onpremise_version,
            connection.credential_expires_at, connection.cai_version,
            connection.onpremise_auth_mode, connection.credential_type,
            connection.tls_verification, connection.tls_ca_pem,
            connection.workbench_mode, connection.workbench_model_type,
            connection.workbench_input_type, connection.workbench_api_key,
            connection.workbench_app_model, connection.workbench_app_auth_mode)
        result = generate_initial_cloudera_token(result)
        credential_changed = bool(
            connection.token.strip() or connection.workbench_api_key.strip()
            or connection.kind == "workbench_app"
            or (connection.kind == "workbench" and "accessKey=" in connection.url)
        )
        if manager.process_alive() and credential_changed and not result.get("token_generated"):
            result.update(apply_cloudera_credential_changes())
        return result
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/cloudera/connections/{connection_id}/renew-token")
def renew_cloudera_token(connection_id: str, request: ClouderaTokenRenewal):
    """Generate or renew a CDP token and apply it without restarting LiteLLM."""

    try:
        result = cloudera.renew_token(connection_id, force=request.force)
        if result["renewed"] and manager.process_alive():
            result.update(apply_cloudera_credential_changes())
        return result
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        cloudera.record_renewal_error(connection_id, str(exc))
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/cloudera/connections/{connection_id}", status_code=204)
def delete_cloudera_connection(connection_id: str):
    """Delete a connection and per-model tokens associated with its identifier."""

    cloudera.delete_connection(connection_id)


@app.post("/api/cloudera/connections/{connection_id}/discover")
def discover_cloudera(connection_id: str):
    """Query Cloudera APIs and normalize heterogeneous endpoints for the UI."""

    try:
        return cloudera.discover(connection_id)
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.put("/api/cloudera/connections/{connection_id}/models/{external_id}/token")
def save_cloudera_model_token(connection_id: str, external_id: str, credential: ClouderaModelToken):
    """Save a dedicated credential when the shared CDP token is insufficient."""

    if not credential.token.strip(): raise HTTPException(422, "El token del modelo está vacío")
    try:
        variable = cloudera.save_model_token(connection_id, external_id, credential.token)
        result = {"api_key_env": variable, "saved": True,
                  **cloudera.model_token_status(connection_id, external_id)}
        if manager.process_alive():
            result.update(apply_cloudera_credential_changes())
        return result
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/cloudera/connections/{connection_id}/probe-model")
def probe_cloudera_model(connection_id: str, model: ClouderaModelProbe):
    """Run a minimal probe with the discovered URL and task type."""

    try:
        return cloudera.probe_model(connection_id, model.external_id, model.url, model.protocol,
                                    model.model_name, model.task, model.has_chat_template,
                                    model.serving_engine, model.requires_input_type)
    except KeyError as exc:
        raise HTTPException(404, "Conexión Cloudera no encontrada") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config/guardrail")
def update_guardrail(update: GuardrailUpdate):
    """Change the global classifier and policy through a transactional restart."""

    try:
        return manager.set_guardrail(
            update.enabled, update.model.strip(), update.policy,
            excluded_models=update.excluded_models, restart=update.restart,
        )
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config/advisor")
def update_advisor(update: AdvisorUpdate):
    """Select the configuration advisor using the same global pattern as the guardrail."""

    try:
        return manager.set_advisor(update.enabled, update.model.strip(), restart=update.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/advisor/recommend")
async def recommend_model_parameters(advice: ModelAdviceRequest):
    """Request JSON from the advisor and filter it before display or application."""

    use_case = advice.use_case.strip()
    if not use_case or len(use_case) > 8_000:
        raise HTTPException(422, "Describe el caso de uso en un máximo de 8.000 caracteres")
    settings = manager.dashboard_settings().get("advisor") or {}
    advisor = str(settings.get("model") or "")
    available_models = manager.models()
    target = next((item for item in available_models if item["name"] == advice.model_name), None)
    advisor_details = next((item for item in available_models if item["name"] == advisor), None)
    if not settings.get("enabled") or not advisor:
        raise HTTPException(409, "Configura primero un modelo asesor")
    if target is None:
        raise HTTPException(404, f"Modelo no encontrado: {advice.model_name}")
    if not manager.is_running() or advisor not in manager.active_model_names():
        raise HTTPException(409, "El modelo asesor debe estar activo en LiteLLM")
    allowed = [
        "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
        "frequency_penalty", "presence_penalty", "seed", "max_tokens", "stop",
        "reasoning_mode", "reasoning_effort", "parameter_policy", "extra_parameters",
    ]
    system = (
        "Eres asesor de configuración de inferencia. Devuelve SOLO un objeto JSON con "
        "summary (string), rationale (array de strings) y parameters (objeto). "
        f"parameters sólo puede usar estas claves: {', '.join(allowed)}. "
        "extra_parameters debe ser un objeto cuyas claves usen notación como extra_body.guided_json. "
        "No inventes parámetros incompatibles con el backend indicado."
    )
    user = json.dumps({
        "target": {key: target.get(key) for key in (
            "name", "provider_model", "backend_profile", "serving_engine", "task",
            "context_window", "max_output_tokens", "compatibility_profile")},
        "use_case": use_case,
    }, ensure_ascii=False)
    endpoint = f"http://{manager.host}:{manager.port}/v1/chat/completions"
    primary_payload = {"model": advisor, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], "temperature": 0.2}
    workbench_advisor = bool(advisor_details and (
        advisor_details.get("cloudera_kind") == "workbench"
        or str(advisor_details.get("provider_model") or "").startswith("cloudera_workbench/")
    ))
    compatibility_retry = False
    try:
        async with httpx.AsyncClient(timeout=float(settings.get("timeout") or 120)) as client:
            response = await client.post(
                endpoint, headers=gateway_auth_headers(), json=primary_payload,
            )
            workbench_contract_rejected = (
                response.status_code == 400
                or ("StatusCode" in response.text and "400" in response.text)
                or "Contrato Workbench rechazado" in response.text
            )
            if response.is_error and workbench_advisor and workbench_contract_rejected:
                compatibility_retry = True
                response = await client.post(endpoint, headers=gateway_auth_headers(), json={
                    "model": advisor,
                    "messages": [{"role": "user", "content": f"{system}\n\nDATOS DEL CASO DE USO:\n{user}"}],
                    "max_tokens": 512,
                })
    except (httpx.RequestError, RuntimeError) as exc:
        raise HTTPException(502, f"No se pudo consultar al asesor: {exc}") from exc
    if response.is_error:
        suffix = " El reintento compatible con Workbench también fue rechazado." if compatibility_retry else ""
        raise HTTPException(response.status_code, f"El asesor rechazó la consulta:{suffix} {response.text[:1000]}")
    try:
        content = response.json()["choices"][0]["message"]["content"].strip()
        result = _advisor_json(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502, "El asesor no devolvió el JSON estructurado solicitado") from exc
    parameters = result.get("parameters") if isinstance(result, dict) else None
    if not isinstance(parameters, dict):
        raise HTTPException(502, "La recomendación no contiene un objeto parameters")
    filtered = {key: value for key, value in parameters.items() if key in allowed}
    return {"advisor": advisor, "target": advice.model_name,
            "summary": str(result.get("summary") or "Recomendación preparada"),
            "rationale": [str(item) for item in (result.get("rationale") or [])][:10],
            "parameters": filtered}


@app.put("/api/config")
def update_config(update: ConfigUpdate):
    """Validate and apply a complete edit through a transactional restart."""
    try:
        manager.validate_model_activation_changes(update.content)
        return manager.update_config(update.content, restart=update.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/validate")
def validate_config(update: ConfigUpdate):
    """Validate without writing or restarting, for the advanced editor."""
    try:
        result = manager.validate_config_text(update.content)
        manager.validate_model_activation_changes(update.content)
        return result
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/apply")
def apply_config():
    """Apply a configuration whose restart was previously deferred."""
    try:
        return manager.apply_pending_config()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/config/models/validate")
async def validate_model_candidate(model: ModelCreate):
    """Probe the deployment and every parameter before allowing it to be saved."""

    try:
        entry = _model_entry(model)
        probe = await _probe_model_candidate(model, entry)
        return {**probe, "validation_id": _register_model_validation(entry), "expires_in_seconds": 900}
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/config/models")
def create_model(model: ModelCreate):
    """Build a LiteLLM entry without accepting plaintext secrets."""
    try:
        entry = _model_entry(model)
        _consume_model_validation(entry, model.validation_id)
        info = entry.setdefault("model_info", {})
        info["dashboard_validation_fingerprint"] = _model_fingerprint(entry)
        info["dashboard_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not model.fallback_model.strip() and model.restart:
            return manager.add_model(entry)
        return manager.add_model(entry,
                                 fallback_model=model.fallback_model.strip(), restart=model.restart)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/config/models/{name}")
def edit_model(name: str, model: ModelCreate):
    """Edit known fields while preserving advanced parameters outside the form."""
    try:
        entry = _model_entry(model)
        _consume_model_validation(entry, model.validation_id)
        info = entry.setdefault("model_info", {})
        info["dashboard_validation_fingerprint"] = _model_fingerprint(entry)
        info["dashboard_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return manager.update_model(name, entry, model.fallback_model.strip(), model.restart)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/config/models/{name}")
def remove_model(name: str, restart: bool = Query(True)):
    """Delete an alias and clear fallbacks, state, and guardrail references."""

    try:
        return manager.delete_model(name, restart=restart)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/gateway/start")
def start():
    """Start the gateway only after validating configuration and secrets."""
    try:
        result = manager.start()
        model_health.trigger()
        return result
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/gateway/stop")
def stop():
    """Stop LiteLLM and its descendant processes."""
    return manager.stop()


@app.put("/api/models/{name}/state")
def set_model_state(name: str, state: ModelState):
    """Change the effective alias state without editing ``config.yaml``."""
    try:
        return manager.set_model(name, state.enabled)
    except KeyError as exc:
        raise HTTPException(404, f"Modelo no encontrado: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/models/{name}/test")
async def test_model(name: str, test: TestCall, request: Request):
    """Run an end-to-end test through the same gateway path used in production."""
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
    if name not in manager.active_model_names():
        raise HTTPException(
            409,
            f"El modelo {name} está guardado pero pendiente de aplicar. "
            "Pulsa «Aplicar cambios pendientes» para reiniciar LiteLLM y cargarlo.",
        )

    # An embedding is not chat. Choosing the endpoint by capability prevents
    # misleading tests while retaining one browser interface.
    endpoint = "embeddings" if model["mode"] == "embedding" else "chat/completions"
    payload = (
        {"model": name, "input": prompt, "encoding_format": "float"}
        if model["mode"] == "embedding"
        else {"model": name, "messages": [{"role": "user", "content": prompt}]}
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            internal_headers = gateway_auth_headers()
            if origin_ip := client_origin_ip(request):
                internal_headers["X-IA-Gateway-Client-IP"] = origin_ip
            response = await client.post(
                f"http://{manager.host}:{manager.port}/v1/{endpoint}",
                json=payload,
                headers=internal_headers,
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
    """Run one generative probe and return its end-to-end latency."""
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


def log_model_names(name: str) -> list[str]:
    """Resolve a public alias and its historical provider identifier."""
    model_names = [name]
    try:
        settings = json.loads(
            (ROOT / "runtime" / "dashboard_settings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        settings = {}
    provider_model = (settings.get("provider_models") or {}).get(name)
    if provider_model and provider_model != name:
        model_names.append(str(provider_model))
    return model_names


def read_model_day_logs(name: str, selected, limit: int) -> list[dict]:
    """Include legacy rows stored under the provider identifier."""

    rows = []
    for model_name in log_model_names(name):
        rows.extend(read_day_logs(ROOT / "runtime", model_name, selected, limit))
    rows.sort(
        key=lambda item: item.get("started_at") or item.get("created_at") or "",
        reverse=True,
    )
    return rows[:limit]


def cached_log_kpis(name: str, selected, *, fresh: bool = False) -> dict[str, Any]:
    """Reuse expensive full-day aggregates briefly during live refreshes."""

    model_names = tuple(log_model_names(name))
    key = (str(ROOT), model_names, selected.isoformat())
    now = time.monotonic()
    cached = _LOG_KPI_CACHE.get(key)
    if not fresh and cached and now - cached[0] < _LOG_KPI_CACHE_SECONDS:
        return cached[1]
    result = log_kpis_for_day(ROOT / "runtime", list(model_names), selected)
    _LOG_KPI_CACHE[key] = (now, result)
    return result


@app.get("/api/models/{name}/log-details")
def log_details(name: str, day: Optional[str] = None, limit: int = Query(250, ge=1, le=1000)):
    """Return only recent rows so the table does not wait for daily aggregates."""

    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"day": selected.isoformat(), "rows": read_model_day_logs(name, selected, limit), "detail_limit": limit}


@app.get("/api/models/{name}/log-kpis")
def log_kpis_endpoint(name: str, day: Optional[str] = None, fresh: bool = False):
    """Return complete-day aggregates independently from recent detail rows."""

    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"day": selected.isoformat(), "kpis": cached_log_kpis(name, selected, fresh=fresh)}


@app.get("/api/models/{name}/logs")
def logs(name: str, day: Optional[str] = None, limit: int = Query(250, ge=1, le=1000)):
    """Return complete-day aggregates and a bounded recent-detail window."""
    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rows = read_model_day_logs(name, selected, limit)
    kpis = cached_log_kpis(name, selected)
    return {
        "day": selected.isoformat(), "rows": rows, "kpis": kpis,
        "detail_limit": limit, "details_truncated": kpis["requests"] > len(rows),
    }


@app.get("/api/models/{name}/log-days")
def log_days(name: str):
    """List available days for an alias from newest to oldest."""

    return {"days": available_days(ROOT / "runtime", name)}


@app.get("/api/models/{name}/logs.xlsx")
def export_logs(name: str, day: Optional[str] = None):
    """Generate the operational workbook for one day and model in memory."""

    try:
        selected = parse_day(day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    rows = read_model_day_logs(name, selected, 50_000)
    content = build_logs_xlsx(name, selected.isoformat(), rows, log_kpis(rows))
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", f"logs-{name}-{selected.isoformat()}.xlsx")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/process-log", response_class=PlainTextResponse)
def process_log(day: Optional[str] = None):
    """Expose LiteLLM stdout/stderr for startup diagnostics."""
    try:
        return manager.process_log(500, parse_day(day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/process-log-days")
def process_log_days():
    """List daily stdout/stderr files from the LiteLLM process."""

    return {"days": manager.process_log_days()}


@app.delete("/api/process-log", status_code=204)
def clear_process_log(day: Optional[str] = None):
    """Clear only the selected technical log, not structured traces."""

    try:
        manager.clear_process_log(parse_day(day))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    internal_ports = {PUBLIC_GATEWAY_PORT, INTERNAL_DASHBOARD_PORT, manager.port}
    if len(internal_ports) != 3:
        raise RuntimeError(
            "IA_GATEWAY_PORT, IA_GATEWAY_DASHBOARD_PORT e IA_GATEWAY_LITELLM_PORT "
            "deben usar puertos distintos"
        )
    # Uvicorn remains private. The edge proxy is the only published listener and
    # separates /v1 (LiteLLM) from the dashboard and its administrative /api.
    # Passing the object prevents Uvicorn from importing app.py and bootstrapping twice.
    uvicorn.run(app, host="127.0.0.1", port=INTERNAL_DASHBOARD_PORT, reload=False)
