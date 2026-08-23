/**
 * Controlador del dashboard.
 *
 * No usamos un framework deliberadamente: el estado es pequeño y queremos que
 * un estudiante pueda seguir el flujo completo desde `fetch` hasta el DOM.
 */

const $ = (selector) => document.querySelector(selector);

let models = [];
let selectedLogModel = "__litellm__";
let selectedTestModel = null;
let gatewayProcessAlive = false;
const remoteLatencies = new Map();

/** Escapar texto antes de insertarlo como HTML evita XSS desde prompts o logs. */
const escapeHtml = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character],
);

/** Único punto de acceso HTTP: normaliza tanto errores JSON como texto plano. */
async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.headers.get("content-type")?.includes("json")
    ? response.json()
    : response.text();
}

function showError(exception) {
  const notice = $("#notice");
  notice.textContent = exception.message;
  notice.hidden = false;
  window.setTimeout(() => { notice.hidden = true; }, 7000);
}

/** Salud y proceso son conceptos distintos: la UI conserva esa distinción. */
async function loadStatus() {
  const status = await api("/api/status");
  gatewayProcessAlive = status.process_alive;

  $("#status").classList.toggle("online", status.running);
  $("#status").classList.toggle("unhealthy", status.process_alive && !status.running);
  $("#status span").textContent = status.running
    ? `Activo · ${status.host}:${status.port} · PID ${status.pid}`
    : status.process_alive
      ? `Sin servicio · puerto ${status.port} · PID ${status.pid}`
      : `Detenido · puerto ${status.port}`;

  $("#resources").textContent = status.process_alive
    ? `CPU ${status.cpu_percent ?? "—"}% del total (${status.cores} cores) · RAM ${status.memory_gb ?? "—"} GB`
    : "CPU — · RAM —";

  const button = $("#gateway-button");
  button.disabled = false;
  button.textContent = status.process_alive ? "Detener" : "Arrancar";
  button.classList.toggle("stop", status.process_alive);
}

function modelCard(model) {
  return `
    <article class="model-card ${model.enabled ? "" : "disabled"}">
      <div class="model-head">
        <div>
          <span class="model-kicker">${model.mode === "embedding" ? "VECTOR" : "CHAT"}</span>
          <h3>${escapeHtml(model.name)}</h3>
        </div>
        <span class="badge">${model.enabled ? "ACTIVO" : "INACTIVO"}</span>
      </div>
      <p class="provider">${escapeHtml(model.provider_model)}</p>
      <div class="model-resources" data-resource-name="${escapeHtml(model.name)}">
        Calculando recursos…
      </div>
      <div class="meta">
        <span>${escapeHtml(model.api_base || "API por defecto")}</span>
        <button
          data-model="${escapeHtml(model.name)}"
          data-enabled="${!model.enabled}"
          class="${model.enabled ? "danger" : "enable"}"
        >${model.enabled ? "Desactivar" : "Activar"}</button>
      </div>
    </article>`;
}

async function loadModels() {
  models = await api("/api/models");
  $("#model-count").textContent = `${models.filter((model) => model.enabled).length} activos / ${models.length}`;
  $("#models").innerHTML = models.map(modelCard).join("");
  $("#models").querySelectorAll("button").forEach((button) => {
    button.onclick = () => toggleModel(button);
  });

  renderTestTabs();
  renderLogTabs();
  await Promise.all([loadLogs(), loadModelResources()]);
}

/** Las métricas remotas se etiquetan, nunca se simulan con datos locales. */
function metricStatus(kind, value) {
  if (value === null || value === undefined) return { level: "unknown", label: "Sin datos" };
  if (kind === "cpu") {
    if (value >= 80) return { level: "danger", label: "Uso crítico" };
    if (value > 60) return { level: "warning", label: "Uso elevado" };
    return { level: "healthy", label: "Uso bajo" };
  }
  if (kind === "memory-free") {
    if (value < 20) return { level: "danger", label: "Memoria crítica" };
    if (value < 40) return { level: "warning", label: "Memoria limitada" };
    return { level: "healthy", label: "Memoria suficiente" };
  }
  if (kind === "latency") {
    if (value > 5000) return { level: "danger", label: "Respuesta lenta" };
    if (value >= 2000) return { level: "warning", label: "Respuesta moderada" };
    return { level: "healthy", label: "Respuesta rápida" };
  }
  return { level: "unknown", label: "Informativo" };
}

function metricRow(label, value, status = null) {
  const indicator = status
    ? `<span class="metric-dot ${status.level}" role="img" aria-label="${status.label}" title="${status.label}"></span>`
    : "";
  return `
    <div class="metric-row">
      <span class="metric-label">${label}</span>
      <span class="metric-value">${value}${indicator}</span>
    </div>`;
}

async function loadModelResources() {
  const resources = await api("/api/model-resources");
  for (const item of resources) {
    const target = [...document.querySelectorAll(".model-resources")]
      .find((node) => node.dataset.resourceName === item.name);
    if (!target) continue;

    if (item.source === "remote") {
      const latency = remoteLatencies.get(item.name);
      const latencyRow = latency?.latency_ms !== undefined
        ? metricRow("Latencia (sonda)", `${latency.latency_ms} ms`, metricStatus("latency", latency.latency_ms))
        : metricRow("Latencia (sonda)", latency?.error || (gatewayProcessAlive ? "Pendiente" : "Gateway detenido"));
      target.innerHTML = [
        metricRow("Proveedor", "OpenAI · remoto"),
        metricRow("Saldo / tokens", "No disponible vía API"),
        latencyRow,
      ].join("");
      target.className = "model-resources remote";
    } else if (!item.available) {
      target.innerHTML = metricRow("Estado", "Ollama no disponible");
      target.className = "model-resources unavailable";
    } else {
      const memoryFree = item.server_memory_free_percent;
      const cpu = item.cpu_percent;
      const rows = item.loaded
        ? [
            metricRow("Memoria modelo", `${item.memory_gb} GB`),
            metricRow("VRAM modelo", `${item.vram_gb} GB`),
          ]
        : [metricRow("Estado modelo", "No cargado")];
      rows.push(
        metricRow("Memoria libre", `${memoryFree ?? "—"}%`, metricStatus("memory-free", memoryFree)),
        metricRow("CPU compartida", `${cpu ?? "—"}%`, metricStatus("cpu", cpu)),
      );
      target.innerHTML = rows.join("");
      target.className = "model-resources";
    }
  }
}

/** Una sola llamada mínima por modelo remoto y carga completa de la página. */
async function loadRemoteLatencies() {
  const remoteModels = models.filter((model) => (
    model.enabled && !model.provider_model.startsWith("ollama/")
  ));
  for (const model of remoteModels) {
    try {
      remoteLatencies.set(
        model.name,
        await api(`/api/models/${encodeURIComponent(model.name)}/latency`, { method: "POST" }),
      );
    } catch {
      remoteLatencies.set(model.name, { error: "No disponible" });
    }
    await loadModelResources();
  }
}

function renderTestTabs() {
  if (!models.some((model) => model.name === selectedTestModel && model.enabled)) {
    selectedTestModel = models.find((model) => model.enabled)?.name ?? null;
  }

  $("#test-tabs").innerHTML = models.map((model) => `
    <button
      class="tab ${model.name === selectedTestModel ? "active" : ""}"
      data-name="${escapeHtml(model.name)}"
      ${model.enabled ? "" : "disabled"}
    >${escapeHtml(model.name)}</button>`).join("");

  $("#test-tabs").querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      selectedTestModel = button.dataset.name;
      renderTestTabs();
    };
  });
  updateTestHelp();
  $("#test-button").disabled = !selectedTestModel;
}

function updateTestHelp() {
  const model = models.find((item) => item.name === selectedTestModel);
  const embedding = model?.mode === "embedding";
  $("#test-mode").textContent = embedding ? "EMBEDDING" : "CHAT";
  $("#test-help").textContent = embedding
    ? "El texto se enviará al endpoint de embeddings."
    : "Se enviará como un mensaje de usuario.";
  $("#test-prompt").placeholder = embedding
    ? "Texto que quieres convertir en un vector…"
    : "Escribe aquí el mensaje de prueba…";
}

/** LiteLLM ocupa siempre la primera pestaña para separar infraestructura/modelos. */
function renderLogTabs() {
  const tabs = [
    { name: "__litellm__", label: "LiteLLM" },
    ...models.map((model) => ({ name: model.name, label: model.name })),
  ];
  $("#tabs").innerHTML = tabs.map((tab) => `
    <button class="tab ${tab.name === selectedLogModel ? "active" : ""}" data-name="${escapeHtml(tab.name)}">
      ${escapeHtml(tab.label)}
    </button>`).join("");
  $("#tabs").querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      selectedLogModel = button.dataset.name;
      renderLogTabs();
      loadLogs().catch(showError);
    };
  });
}

async function toggleModel(button) {
  button.disabled = true;
  try {
    await api(`/api/models/${encodeURIComponent(button.dataset.model)}/state`, {
      method: "PUT",
      body: JSON.stringify({ enabled: button.dataset.enabled === "true" }),
    });
    await Promise.all([loadModels(), loadStatus()]);
  } catch (exception) {
    showError(exception);
    button.disabled = false;
  }
}

/** Intl hace la conversión temporal; la base de datos conserva timestamps neutros. */
function madridTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("es-ES", {
    timeZone: "Europe/Madrid",
    dateStyle: "short",
    timeStyle: "medium",
    hour12: false,
  }).format(date);
}

function logCard(log) {
  const timestamp = madridTime(log.started_at || log.created_at);
  return `
    <article class="log">
      <div class="log-head">
        <span class="${log.status === "success" ? "ok" : "error"}">
          ${log.status === "success" ? "● CORRECTO" : "● ERROR"}
        </span>
        <span>${escapeHtml(timestamp)} · Europe/Madrid</span>
      </div>
      <div class="request-meta">
        <span><b>Fecha:</b> ${escapeHtml(timestamp)}</span>
        <span><b>Origen:</b> ${escapeHtml(log.origin_ip || "no disponible")}</span>
        <span><b>Respuesta:</b> ${escapeHtml(log.provider_ip || "no disponible")}</span>
        <span><b>Inicio respuesta:</b> ${log.ttft_ms ?? "—"} ms</span>
        <span><b>Fin respuesta:</b> ${log.duration_ms ?? "—"} ms</span>
      </div>
      <div class="io-grid">
        <div><h4>Pregunta / entrada</h4><pre>${escapeHtml(JSON.stringify(log.request, null, 2))}</pre></div>
        <div><h4>Respuesta / salida</h4><pre>${escapeHtml(log.error || JSON.stringify(log.response, null, 2))}</pre></div>
      </div>
    </article>`;
}

async function loadLogs() {
  if (selectedLogModel === "__litellm__") {
    const raw = await api("/api/process-log");
    $("#logs").innerHTML = raw
      ? `<pre class="process-log">${escapeHtml(raw)}</pre>`
      : '<p class="empty">LiteLLM todavía no ha generado salida de proceso.</p>';
    return;
  }
  if (!selectedLogModel) return;
  const logs = await api(`/api/models/${encodeURIComponent(selectedLogModel)}/logs?limit=100`);
  $("#logs").innerHTML = logs.length
    ? logs.map(logCard).join("")
    : '<p class="empty">Todavía no hay solicitudes registradas para este modelo.</p>';
}

function readableResult(data) {
  if (data.mode === "chat") {
    return data.result?.choices?.[0]?.message?.content ?? JSON.stringify(data.result, null, 2);
  }
  const vector = data.result?.data?.[0]?.embedding;
  if (!Array.isArray(vector)) return JSON.stringify(data.result, null, 2);
  const preview = vector.slice(0, 12).join(", ");
  return `Embedding generado correctamente\n\nDimensiones: ${vector.length}\nPrimeros valores: [${preview}${vector.length > 12 ? ", …" : ""}]`;
}

async function runTest() {
  if (!selectedTestModel) return;
  const button = $("#test-button");
  const result = $("#test-result");
  button.disabled = true;
  button.textContent = "Enviando…";
  result.hidden = true;
  const started = performance.now();
  try {
    const data = await api(`/api/models/${encodeURIComponent(selectedTestModel)}/test`, {
      method: "POST",
      body: JSON.stringify({ prompt: $("#test-prompt").value }),
    });
    result.querySelector("pre").textContent = readableResult(data);
    $("#test-time").textContent = `${Math.round(performance.now() - started)} ms`;
    result.hidden = false;
    selectedLogModel = selectedTestModel;
    renderLogTabs();
    window.setTimeout(() => loadLogs().catch(showError), 500);
  } catch (exception) {
    showError(exception);
  } finally {
    button.disabled = false;
    button.textContent = "Enviar prueba";
  }
}

$("#gateway-button").onclick = async () => {
  const button = $("#gateway-button");
  button.disabled = true;
  try {
    await api(`/api/gateway/${gatewayProcessAlive ? "stop" : "start"}`, { method: "POST" });
    await loadStatus();
    if (gatewayProcessAlive && remoteLatencies.size === 0) await loadRemoteLatencies();
  } catch (exception) {
    showError(exception);
    button.disabled = false;
  }
};

$("#refresh").onclick = () => loadLogs().catch(showError);
$("#test-button").onclick = runTest;

// La inicialización secuencial garantiza que la sonda sólo se lance si el gateway está listo.
async function initialize() {
  await loadStatus();
  await loadModels();
  if (gatewayProcessAlive) await loadRemoteLatencies();
}

initialize().catch(showError);
window.setInterval(
  () => Promise.all([loadStatus(), loadModelResources(), loadLogs()]).catch(() => {}),
  3000,
);
