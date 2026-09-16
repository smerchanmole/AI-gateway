/**
 * Controlador del dashboard.
 *
 * No usamos un framework deliberadamente: el estado es pequeño y queremos que
 * un estudiante pueda seguir el flujo completo desde `fetch` hasta el DOM.
 */

const $ = (selector) => document.querySelector(selector);

let models = [];
let selectedLogModel = "__litellm__";
let selectedLogDay = new Date().toLocaleDateString("en-CA", {timeZone: "Europe/Madrid"});
let selectedTestModel = null;
let gatewayProcessAlive = false;
let editingModelName = null;
const remoteLatencies = new Map();
let clouderaConnections = [];
let clouderaModels = [];
let editingClouderaConnectionId = null;
let preparedClouderaSource = null;
let advisorRecommendation = null;
let advisorTargetName = null;
let advisorTrigger = null;
let guardrailExcludedModels = new Set();
let csrfToken = "";
let dashboardAuthenticated = false;
const clouderaProbeTimers = new Map();
const clouderaChecksInProgress = new Set();
let clouderaRefreshInProgress = false;

/* -------------------------------------------------------------------------
 * 1. Infraestructura de interfaz
 * -------------------------------------------------------------------------
 * Todas las llamadas pasan por `api`; todos los textos no confiables pasan por
 * `escapeHtml`. Estas dos reglas reducen errores de red y riesgos XSS.
 */

/** Escapar texto antes de insertarlo como HTML evita XSS desde prompts o logs. */
const escapeHtml = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character],
);

/** Único punto de acceso HTTP: normaliza tanto errores JSON como texto plano. */
async function api(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", "Accept-Language": window.IAGatewayI18n?.language || "es", ...(options.headers || {}) };
  let body = options.body;
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
    // Redundancia deliberada para proxies Cloudera que retiren cabeceras X-*.
    // El token permanece en el cuerpo y nunca se incorpora a la URL.
    if (headers["Content-Type"].startsWith("application/json")) {
      let payload = {};
      if (typeof body === "string" && body.length) payload = JSON.parse(body);
      if (payload && typeof payload === "object" && !Array.isArray(payload)) {
        body = JSON.stringify({...payload, _csrf_token: csrfToken});
      }
    }
  }
  const response = await fetch(url, {
    ...options,
    headers,
    body,
    credentials: "same-origin",
  });
  if (!response.ok) {
    // El cuerpo de una Response es un stream y sólo puede consumirse una vez.
    // Leemos texto primero y, si procede, interpretamos esa misma copia como JSON.
    const rawBody = await response.text();
    let detail = rawBody || `HTTP ${response.status}`;
    try {
      const payload = JSON.parse(rawBody);
      detail = payload.detail ?? payload.message ?? payload.error ?? payload;
    } catch { /* La respuesta era texto plano; conservamos rawBody. */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  // DELETE suele responder 204 o 200 con cuerpo vacío. Intentar response.json()
  // en esos casos producía "Unexpected end of JSON input" aunque el borrado
  // ya se hubiera completado correctamente.
  if (response.status === 204) return null;
  const rawBody = await response.text();
  if (!rawBody) return null;
  if (response.headers.get("content-type")?.includes("json")) {
    try { return JSON.parse(rawBody); }
    catch { throw new Error("El servidor devolvió JSON vacío o no válido"); }
  }
  return rawBody;
}

function showError(exception) {
  const notice = $("#notice");
  notice.textContent = exception.message;
  notice.hidden = false;
  window.setTimeout(() => { notice.hidden = true; }, 7000);
}

function setInlineStatus(selector, message, kind = "") {
  const target = $(selector);
  target.textContent = message;
  target.className = `inline-status ${kind}`.trim();
}

/* -------------------------------------------------------------------------
 * 2. Catálogo Cloudera
 * -------------------------------------------------------------------------
 * La UI mantiene conexiones y modelos descubiertos por separado. Una conexión
 * es persistente; la lista descubierta y los resultados de prueba viven en
 * memoria y se reconstruyen al consultar de nuevo el catálogo.
 */

function updateClouderaFormContext() {
  /** Revela sólo los campos compatibles y explica la matriz certificada. */
  const platform = $("#cloudera-platform").value;
  const kind = $("#cloudera-kind").value;
  const onpremise = platform === "onpremise";
  $("#cloudera-version-field").hidden = !onpremise;
  $("#cloudera-cai-version-field").hidden = !onpremise;
  $("#cloudera-cloud-fields").hidden = onpremise;
  $("#cloudera-onprem-inference-fields").hidden = !onpremise || kind !== "inference";
  const helpContext = `${platform}-${kind}`;
  document.querySelectorAll("[data-cloudera-help]").forEach((content) => {
    content.hidden = content.dataset.clouderaHelp !== helpContext;
  });
  $("#cloudera-url").placeholder = kind === "workbench"
    ? "https://ml-workbench.example.com"
    : "https://ml-entorno.example.com";
  if (onpremise && kind === "inference") updateClouderaOnpremCompatibility();
}

function updateClouderaOnpremCompatibility() {
  const caiVersion = $("#cloudera-cai-version").value;
  const runtime = $("#cloudera-onpremise-version").value;
  const supported = {
    "1.5.5_sp2": new Set(["7.1.9_sp1", "7.3.1"]),
    "1.5.5_sp2_chf1": new Set(["7.1.9_sp1", "7.3.1", "7.3.2"]),
    "1.5.5_sp3": new Set(["7.1.9_sp2", "7.3.1", "7.3.2"]),
  };
  const runtimeLabels = {"7.1.9_sp1": "7.1.9 SP1", "7.1.9_sp2": "7.1.9 SP2", "7.3.1": "7.3.1", "7.3.2": "7.3.2"};
  const caiLabels = {"1.5.5_sp2": "1.5.5 SP2", "1.5.5_sp2_chf1": "1.5.5 SP2 CHF1", "1.5.5_sp3": "1.5.5 SP3"};
  const valid = supported[caiVersion]?.has(runtime);
  const note = $("#cloudera-compatibility");
  note.textContent = valid
    ? `${caiLabels[caiVersion]} con Runtime ${runtimeLabels[runtime]} está en la matriz documentada. CDP_TOKEN (UMS) está disponible.`
    : `${caiLabels[caiVersion]} con Runtime ${runtimeLabels[runtime]} no figura como combinación compatible en la matriz oficial.`;
  note.classList.toggle("error", !valid);

  const knoxSupported = caiVersion === "1.5.5_sp3" && ["7.1.9_sp2", "7.3.2"].includes(runtime);
  const knoxOption = $("#cloudera-credential-type").querySelector('option[value="knox_api_key"]');
  knoxOption.disabled = !knoxSupported;
  knoxOption.textContent = knoxSupported ? "Knox API key" : "Knox API key · no certificada para esta combinación";
  if (!knoxSupported && $("#cloudera-credential-type").value === "knox_api_key") {
    $("#cloudera-credential-type").value = "cdp_token";
  }

  const mode = document.querySelector('input[name="cloudera-auth-mode"]:checked')?.value || "manual";
  [["#cloudera-manual-auth", "manual"], ["#cloudera-ums-auth", "ums_auto"]].forEach(([selector, optionMode]) => {
    const section = $(selector); const active = mode === optionMode;
    section.classList.toggle("inactive", !active);
    section.querySelectorAll("input:not([name='cloudera-auth-mode']), select, textarea").forEach((control) => { control.disabled = !active; });
  });
  updateClouderaTlsFields();
}

function updateClouderaTlsFields() {
  const automatic = document.querySelector('input[name="cloudera-auth-mode"]:checked')?.value === "ums_auto";
  const mode = $("#cloudera-onprem-tls-verification").value;
  const custom = automatic && mode === "custom_ca";
  $("#cloudera-onprem-ca-field").hidden = !custom;
  $("#cloudera-onprem-ca-pem").disabled = !custom;
  $("#cloudera-onprem-tls-warning").hidden = !(automatic && mode === "disabled");
}

function clouderaFormCredential() {
  const platform = $("#cloudera-platform").value;
  if (platform === "cloud") {
    return {token: $("#cloudera-token").value, renewalUrl: $("#cloudera-cloud-renewal-url").value,
      accessKeyId: $("#cloudera-access-key-id").value, privateKey: $("#cloudera-private-key").value,
      workloadName: $("#cloudera-workload-name").value || "DE", credentialExpiresAt: "",
      tlsVerification: "system", tlsCaPem: ""};
  }
  const mode = document.querySelector('input[name="cloudera-auth-mode"]:checked')?.value || "manual";
  return mode === "ums_auto"
    ? {token: "", renewalUrl: $("#cloudera-onprem-renewal-url").value,
      accessKeyId: $("#cloudera-onprem-access-key-id").value,
      privateKey: $("#cloudera-onprem-private-key").value,
      workloadName: $("#cloudera-onprem-workload-name").value || "DE", credentialExpiresAt: "",
      tlsVerification: $("#cloudera-onprem-tls-verification").value,
      tlsCaPem: $("#cloudera-onprem-ca-pem").value}
    : {token: $("#cloudera-onprem-token").value, renewalUrl: "", accessKeyId: "", privateKey: "",
      workloadName: "DE", credentialExpiresAt: $("#cloudera-onprem-expiry").value,
      tlsVerification: "system", tlsCaPem: ""};
}

function clouderaLifecycleLabel(item) {
  if (item.credential_lifecycle === "renewable") return "Renovación automática configurada";
  if (item.credential_lifecycle === "expiring_manual") return "Caduca · sustitución manual";
  if (item.credential_lifecycle === "long_lived_unverified") return "Clave larga · verifica vigencia en Knox";
  if (item.credential_lifecycle === "expiry_unknown") return "Caducidad no declarada por la credencial";
  return item.renewal_ready ? "Token pendiente de generar" : "Añade una credencial";
}

function clouderaCredentialSnapshot(connections) {
  /** Compara sólo estado de credenciales; evita repintados periódicos inútiles. */
  return JSON.stringify(connections.map((item) => ({
    id: item.id,
    has_token: item.has_token,
    token_expires_at: item.token_expires_at,
    renewal_due_at: item.renewal_due_at,
    rotation_due_at: item.rotation_due_at,
    token_renewed_at: item.token_renewed_at,
    renewal_state: item.renewal_state,
    renewal_message: item.renewal_message,
  })));
}

async function loadClouderaConnections({onlyIfChanged = false} = {}) {
  const previous = clouderaConnections;
  const updated = await api("/api/cloudera/connections");
  if (onlyIfChanged && clouderaCredentialSnapshot(previous) === clouderaCredentialSnapshot(updated)) return false;
  const previousById = new Map(previous.map((item) => [item.id, item]));
  const renewed = updated.find((item) => (
    item.token_renewed_at && item.token_renewed_at !== previousById.get(item.id)?.token_renewed_at
  ));
  clouderaConnections = updated;
  $("#cloudera-connections").innerHTML = clouderaConnections.length ? clouderaConnections.map((item) => `
    <article class="cloudera-connection" data-connection="${item.id}"><div><b>${escapeHtml(item.name)}</b><small>${item.kind === "inference" ? "AI Inference" : "Workbench API v2"} · ${item.platform === "onpremise" ? `On-premise ${escapeHtml(item.cai_version_label || "")} · Runtime ${escapeHtml(item.runtime_version_label || item.onpremise_version)}` : "Cloud"}</small><small>Prueba automática cada ${escapeHtml(item.probe_interval_minutes || 5)} min</small></div><div class="connection-url">${escapeHtml(item.url)}</div><span class="credential-state ${item.token_expired ? "expired" : item.has_token ? "ready" : "missing"}">${item.renewal_state === "renewing" ? (item.has_token ? "Renovando token…" : "Generando token inicial…") : item.token_expired ? "JWT caducado" : item.has_token ? "Credencial guardada" : item.renewal_ready ? "Token pendiente de generar" : "Falta credencial"}${item.token_expires_at ? `<small>Caduca: ${escapeHtml(madridTime(item.token_expires_at))}</small>` : ""}${item.renewal_due_at ? `<small>Renovación prevista: ${escapeHtml(madridTime(item.renewal_due_at))}</small>` : ""}${item.rotation_due_at ? `<small>Rotar antes de: ${escapeHtml(madridTime(item.rotation_due_at))}</small>` : ""}<small>${escapeHtml(clouderaLifecycleLabel(item))}</small></span><div class="row-actions"><button class="secondary discover-cloudera" data-id="${item.id}" ${!item.has_token || item.token_expired ? "disabled" : ""}>Buscar modelos</button><button class="secondary check-all-cloudera" data-id="${item.id}" ${!item.has_token || item.token_expired ? "disabled" : ""}>Probar todos</button>${item.renewal_ready ? `<button class="secondary renew-cloudera" data-id="${item.id}">${item.has_token ? "Renovar token" : "Generar token"}</button>` : ""}<button class="secondary edit-cloudera" data-id="${item.id}">${item.has_token && !item.renewal_ready ? "Sustituir credencial" : "Editar"}</button><button class="danger delete-cloudera" data-id="${item.id}">Borrar</button></div><div class="connection-health-summary" data-health-summary="${item.id}">Sin comprobaciones de modelos</div><p class="connection-progress ${item.renewal_state === "error" || item.token_expired ? "error" : ""}" data-progress="${item.id}" role="status">${escapeHtml(item.renewal_message || (item.token_expired ? (item.renewal_ready ? "JWT caducado; se regenerará automáticamente" : "JWT caducado; sustituye la credencial") : item.has_token ? "Lista para consultar" : item.renewal_ready ? "Completa la generación del token" : "Añade una credencial válida"))}</p><section class="connection-models" data-connection-models="${item.id}"><p class="empty compact">Pulsa «Buscar modelos» para ver los modelos de esta conexión.</p></section></article>`).join("") : '<p class="empty compact">No hay conexiones Cloudera configuradas. Añade una arriba para comenzar.</p>';
  document.querySelectorAll(".discover-cloudera").forEach((button) => button.onclick = () => discoverCloudera(button));
  document.querySelectorAll(".check-all-cloudera").forEach((button) => button.onclick = () => autoProbeConnection(button.dataset.id, true));
  document.querySelectorAll(".renew-cloudera").forEach((button) => button.onclick = () => renewClouderaToken(button.dataset.id, true));
  document.querySelectorAll(".edit-cloudera").forEach((button) => button.onclick = () => editClouderaConnection(button.dataset.id));
  document.querySelectorAll(".delete-cloudera").forEach((button) => button.onclick = () => deleteClouderaConnection(button.dataset.id));
  if (clouderaModels.length) renderClouderaModels();
  scheduleClouderaChecks();
  updateConnectionHealthSummaries();
  if (renewed && previousById.has(renewed.id)) {
    const expiry = renewed.token_expires_at ? ` Nueva caducidad: ${madridTime(renewed.token_expires_at)}.` : "";
    setInlineStatus("#cloudera-status", `CDP token actualizado automáticamente.${expiry}`, "success");
  }
  return true;
}

async function refreshClouderaConnections() {
  /** Sincroniza la vista con SQLite sin solapar peticiones ni recargar la SPA. */
  if (clouderaRefreshInProgress) return;
  clouderaRefreshInProgress = true;
  try {
    await loadClouderaConnections({onlyIfChanged: true});
  } finally {
    clouderaRefreshInProgress = false;
  }
}

function editClouderaConnection(id) {
  const item = clouderaConnections.find((connection) => connection.id === id);
  if (!item) return;
  editingClouderaConnectionId = id;
  $("#cloudera-name").value = item.name; $("#cloudera-kind").value = item.kind; $("#cloudera-platform").value = item.platform || "cloud";
  const runtime = {"legacy": "7.1.9_sp1", "7.3.2_plus": "7.3.2"}[item.onpremise_version] || item.onpremise_version || "7.3.2";
  $("#cloudera-cai-version").value = item.cai_version || "1.5.5_sp3";
  $("#cloudera-onpremise-version").value = runtime;
  const authMode = item.onpremise_auth_mode || (item.renewal_ready ? "ums_auto" : "manual");
  const authRadio = document.querySelector(`input[name="cloudera-auth-mode"][value="${authMode}"]`);
  if (authRadio) authRadio.checked = true;
  $("#cloudera-credential-type").value = item.credential_type || "cdp_token";
  updateClouderaFormContext();
  $("#cloudera-probe-interval").value = item.probe_interval_minutes || 5; $("#cloudera-url").value = item.url; $("#cloudera-token").value = "";
  $("#cloudera-onprem-token").value = "";
  $("#cloudera-onprem-expiry").value = item.token_expires_at && !item.renewal_ready ? item.token_expires_at.slice(0, 10) : "";
  $("#cloudera-access-key-id").value = item.cdp_access_key_id || ""; $("#cloudera-private-key").value = "";
  $("#cloudera-cloud-renewal-url").value = item.platform === "cloud" ? item.renewal_url || "" : "";
  $("#cloudera-workload-name").value = item.workload_name || "DE";
  $("#cloudera-onprem-renewal-url").value = item.platform === "onpremise" ? item.renewal_url || "" : "";
  $("#cloudera-onprem-workload-name").value = item.workload_name || "DE";
  $("#cloudera-onprem-access-key-id").value = item.platform === "onpremise" ? item.cdp_access_key_id || "" : "";
  $("#cloudera-onprem-private-key").value = "";
  $("#cloudera-onprem-tls-verification").value = item.tls_verification || "system";
  $("#cloudera-onprem-ca-pem").value = "";
  $("#cloudera-onprem-ca-pem").placeholder = item.has_tls_ca
    ? "Bundle guardado · déjalo vacío para conservarlo"
    : "-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----";
  updateClouderaOnpremCompatibility();
  $("#save-cloudera-connection").textContent = "Guardar cambios"; $("#cancel-cloudera-edit").hidden = false;
  setInlineStatus("#cloudera-status", `Editando conexión «${item.name}». La credencial actual se conservará si dejas el campo vacío.`);
  $("#cloudera-name").focus();
}

function cancelClouderaEdit() {
  editingClouderaConnectionId = null; $("#cloudera-connection-form").reset();
  $("#cloudera-onprem-ca-pem").placeholder = "-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----";
  $("#save-cloudera-connection").textContent = "Guardar conexión"; $("#cancel-cloudera-edit").hidden = true;
  updateClouderaFormContext();
}

async function deleteClouderaConnection(id) {
  const item = clouderaConnections.find((connection) => connection.id === id);
  const dialog = $("#delete-cloudera-dialog");
  $("#delete-cloudera-message").textContent = `Vas a eliminar «${item?.name || id}» y sus credenciales locales.`;
  dialog.showModal(); await new Promise((resolve) => dialog.addEventListener("close", resolve, {once: true}));
  if (dialog.returnValue !== "delete") return;
  try {
    await api(`/api/cloudera/connections/${id}`, {method: "DELETE"});
    if (editingClouderaConnectionId === id) cancelClouderaEdit();
    clouderaModels = clouderaModels.filter((model) => model.connection_id !== id);
    if (clouderaModels.length) renderClouderaModels();
    setInlineStatus("#cloudera-status", "Conexión y credenciales asociadas eliminadas.", "success");
    await loadClouderaConnections();
  } catch (exception) { setInlineStatus("#cloudera-status", `No se pudo eliminar: ${exception.message}`, "error"); }
}

async function saveClouderaConnection(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = form.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  setInlineStatus("#cloudera-status", "Guardando conexión y obteniendo credencial si es necesaria…");
  try {
    const credential = clouderaFormCredential();
    const endpoint = editingClouderaConnectionId ? `/api/cloudera/connections/${editingClouderaConnectionId}` : "/api/cloudera/connections";
    const result = await api(endpoint, {method: editingClouderaConnectionId ? "PUT" : "POST", body: JSON.stringify({
      name: $("#cloudera-name").value, kind: $("#cloudera-kind").value, platform: $("#cloudera-platform").value,
      probe_interval_minutes: Number($("#cloudera-probe-interval").value || 5), url: $("#cloudera-url").value,
      onpremise_version: $("#cloudera-onpremise-version").value,
      cai_version: $("#cloudera-cai-version").value,
      onpremise_auth_mode: document.querySelector('input[name="cloudera-auth-mode"]:checked')?.value || "manual",
      credential_type: $("#cloudera-credential-type").value,
      token: credential.token, workload_user: "", workload_password: "", cdp_access_key_id: credential.accessKeyId,
      cdp_private_key: credential.privateKey,
      renewal_url: credential.renewalUrl, workload_name: credential.workloadName,
      credential_expires_at: credential.credentialExpiresAt,
      tls_verification: credential.tlsVerification, tls_ca_pem: credential.tlsCaPem,
    })});
    const action = editingClouderaConnectionId ? "actualizada" : "guardada";
    cancelClouderaEdit();
    if (result.token_generation_error) {
      setInlineStatus("#cloudera-status", `Conexión ${action}, pero no se pudo generar el token: ${result.token_generation_error}`, "error");
    } else {
      const tokenMessage = result.token_generated ? " CDP token generado automáticamente." : "";
      setInlineStatus("#cloudera-status", `Conexión ${action} correctamente.${tokenMessage} URL efectiva: ${result.url}. Pulsa «Buscar modelos».`, "success");
    }
    await loadClouderaConnections();
  } catch (exception) {
    setInlineStatus("#cloudera-status", `No se pudo guardar la conexión: ${exception.message}`, "error");
  } finally { submitButton.disabled = false; }
}

async function discoverCloudera(button) {
  const progress = document.querySelector(`[data-progress="${button.dataset.id}"]`);
  button.disabled = true; button.textContent = "Buscando…";
  progress.textContent = "Conectando con Cloudera y buscando modelos…"; progress.className = "connection-progress searching";
  const modelContainer = document.querySelector(`[data-connection-models="${button.dataset.id}"]`);
  modelContainer.innerHTML = '<p class="empty compact">Consultando el catálogo. En un Workbench puede tardar mientras se recorren proyectos y deployments…</p>';
  try {
    const discovered = await api(`/api/cloudera/connections/${button.dataset.id}/discover`, {method: "POST"});
    const previousResults = new Map(clouderaModels.filter((model) => model.connection_id === button.dataset.id)
      .map((model) => [model.external_id, model.probe_result]));
    discovered.forEach((model) => { model.probe_result = previousResults.get(model.external_id); });
    clouderaModels = clouderaModels.filter((model) => model.connection_id !== button.dataset.id).concat(discovered);
    const activeStates = new Set(["loaded", "deployed", "running", "ready"]);
    const active = discovered.filter((model) => activeStates.has(String(model.state).toLowerCase())).length;
    const compatible = discovered.filter((model) => model.protocol === "openai").length;
    renderClouderaModels();
    const summary = `${discovered.length} encontrados · ${active} activos · ${compatible} OpenAI compatibles`;
    progress.textContent = `Búsqueda completada: ${summary}.`; progress.className = "connection-progress success";
    updateConnectionHealthSummaries();
  } catch (exception) {
    modelContainer.innerHTML = `<p class="empty compact error-text">No se pudieron cargar modelos: ${escapeHtml(exception.message)}</p>`;
    progress.textContent = `Error: ${exception.message}`; progress.className = "connection-progress error";
  }
  finally { button.disabled = false; button.textContent = "Buscar modelos"; }
}

function clouderaDeploymentState(model) {
  const value = String(model.state || "").toLowerCase();
  const readiness = (model.conditions || []).find((condition) => ["ready", "ingressready"].includes(String(condition.type || "").toLowerCase()));
  if (readiness && String(readiness.status).toLowerCase() === "false") return {level: "danger", label: `${readiness.type} no disponible`};
  if (["loaded", "deployed", "running", "ready"].includes(value)) return {level: "healthy", label: "Modelo activo"};
  if (["deploying", "loading", "starting", "pending", "updating"].includes(value)) return {level: "warning", label: "Modelo iniciándose"};
  if (["failed", "stopped", "stopping", "error", "unloaded"].includes(value)) return {level: "danger", label: `Modelo ${value}`};
  if (readiness && String(readiness.status).toLowerCase() === "true") return {level: "healthy", label: "Endpoint preparado"};
  return {level: "unknown", label: `Estado ${model.state || "desconocido"}`};
}

function clouderaCredentialState(model) {
  if (model.token_expired) return {level: "danger", label: "Token caducado"};
  if (model.has_model_token) return {level: "healthy", label: "Token propio disponible"};
  if (model.credential_available) return {level: "warning", label: "Usará el CDP token general"};
  return {level: "unknown", label: "Sin credencial"};
}

function stateIndicator(state) {
  return `<span class="state-indicator"><i class="metric-dot ${state.level}" aria-hidden="true"></i><span>${escapeHtml(state.label)}</span></span>`;
}

function clouderaHealthScore(model) {
  const weights = {healthy: 3, warning: 2, unknown: 1, danger: 0};
  const response = model.probe_result ? (model.probe_result.ok ? 3 : 0) : 1;
  return response * 100 + weights[clouderaDeploymentState(model).level] * 10 + weights[clouderaCredentialState(model).level];
}

function updateConnectionHealthSummaries() {
  clouderaConnections.forEach((connection) => {
    const target = document.querySelector(`[data-health-summary="${connection.id}"]`);
    if (!target) return;
    const connectionModels = clouderaModels.filter((model) => model.connection_id === connection.id);
    const counts = {ok: 0, failed: 0, pending: 0};
    connectionModels.forEach((model) => {
      if (!model.probe_result) counts.pending += 1;
      else if (model.probe_result.ok) counts.ok += 1;
      else counts.failed += 1;
    });
    target.innerHTML = connectionModels.length
      ? `${stateIndicator({level: "healthy", label: `${counts.ok} OK`})}${stateIndicator({level: "danger", label: `${counts.failed} con error`})}${stateIndicator({level: "unknown", label: `${counts.pending} sin probar`})}`
      : "Sin modelos detectados";
  });
}

function scheduleClouderaChecks() {
  clouderaProbeTimers.forEach((timer) => window.clearInterval(timer));
  clouderaProbeTimers.clear();
  clouderaConnections.forEach((connection) => {
    if (!connection.has_token || connection.token_expired) return;
    const milliseconds = Math.max(1, Number(connection.probe_interval_minutes || 5)) * 60_000;
    clouderaProbeTimers.set(connection.id, window.setInterval(() => autoProbeConnection(connection.id), milliseconds));
  });
}

async function requestClouderaProbe(model) {
  return api(`/api/cloudera/connections/${model.connection_id}/probe-model`, {method: "POST", body: JSON.stringify({
    external_id: model.external_id, url: model.url, protocol: model.protocol, model_name: model.model_name,
    task: model.task, has_chat_template: model.has_chat_template,
    serving_engine: model.serving_engine, requires_input_type: model.requires_input_type,
  })});
}

async function autoProbeConnection(connectionId, requestedByUser = false) {
  if (clouderaChecksInProgress.has(connectionId)) return;
  clouderaChecksInProgress.add(connectionId);
  const progress = document.querySelector(`[data-progress="${connectionId}"]`);
  try {
    let connectionModels = clouderaModels.filter((model) => model.connection_id === connectionId);
    if (!connectionModels.length) {
      const discovered = await api(`/api/cloudera/connections/${connectionId}/discover`, {method: "POST"});
      clouderaModels = clouderaModels.filter((model) => model.connection_id !== connectionId).concat(discovered);
      connectionModels = discovered;
    }
    if (progress) { progress.textContent = `Comprobando 0/${connectionModels.length} modelos…`; progress.className = "connection-progress searching"; }
    for (let position = 0; position < connectionModels.length; position += 1) {
      const model = connectionModels[position];
      if (!model.url) model.probe_result = {ok: false, message: "Cloudera no publicó una URL para este modelo", credential_source: model.credential_source || "—"};
      else {
        try { model.probe_result = await requestClouderaProbe(model); }
        catch (exception) { model.probe_result = {ok: false, message: exception.message, credential_source: model.credential_source || "—"}; }
      }
      if (progress) progress.textContent = `Comprobando ${position + 1}/${connectionModels.length} modelos…`;
      renderClouderaModels(false);
      updateConnectionHealthSummaries();
    }
    renderClouderaModels(true);
    updateConnectionHealthSummaries();
    if (progress) { progress.textContent = `${requestedByUser ? "Prueba manual" : "Prueba automática"} completada · ${new Date().toLocaleTimeString(window.IAGatewayI18n?.localeTag() || "es-ES")}`; progress.className = "connection-progress success"; }
  } catch (exception) {
    if (progress) { progress.textContent = `No se pudo completar la comprobación: ${exception.message}`; progress.className = "connection-progress error"; }
  } finally { clouderaChecksInProgress.delete(connectionId); }
}

async function renewClouderaToken(connectionId, force = false, refreshView = true) {
  const progress = document.querySelector(`[data-progress="${connectionId}"]`);
  try {
    if (progress) { progress.textContent = "Solicitando credencial a Cloudera…"; progress.className = "connection-progress searching"; }
    const result = await api(`/api/cloudera/connections/${connectionId}/renew-token`, {
      method: "POST", body: JSON.stringify({force}),
    });
    if (progress && (force || result.renewed)) {
      progress.textContent = result.renewed
        ? (result.generated ? "Token inicial generado · LiteLLM continúa activo" : "Token renovado · LiteLLM continúa activo")
        : result.message;
      progress.className = "connection-progress success";
    }
    if (refreshView && result.renewed) await loadClouderaConnections();
    return result;
  } catch (exception) {
    if (progress) { progress.textContent = `No se pudo obtener el token: ${exception.message}`; progress.className = "connection-progress error"; }
    return null;
  }
}

function renderClouderaModels(sortByHealth = true) {
  if (sortByHealth) clouderaModels.sort((left, right) => clouderaHealthScore(right) - clouderaHealthScore(left) || left.name.localeCompare(right.name));
  const modelMarkup = (entries) => entries.length ? entries.map(({model, index}) => {
    const compatible = model.protocol === "openai";
    const workbench = model.protocol === "workbench";
    const deployment = clouderaDeploymentState(model);
    const credential = clouderaCredentialState(model);
    const probe = model.probe_result;
    const response = probe
      ? `${stateIndicator({level: probe.ok ? "healthy" : "danger", label: probe.ok ? "Responde correctamente" : "Prueba fallida"})}<span>${escapeHtml(probe.message)} · ${escapeHtml(probe.credential_source)}${probe.http_status ? ` · HTTP ${probe.http_status}` : ""}${probe.latency_ms ? ` · ${probe.latency_ms} ms` : ""}</span>`
      : `${stateIndicator({level: "unknown", label: "Respuesta sin probar"})}<span>Se usará ${escapeHtml(model.credential_source || "la credencial disponible")}</span>`;
    const protocolName = compatible ? "OpenAI compatible" : (workbench ? "Cloudera Workbench" : "Open Inference predictivo");
    const engineNames = {nim: "NVIDIA NIM", vllm: "vLLM", triton: "NVIDIA Triton", "openai-compatible": "Motor no publicado"};
    const engineName = engineNames[model.serving_engine] || "Motor no identificado";
    const backendName = model.runtime_backend && model.runtime_backend !== model.serving_engine
      ? ` · backend ${engineNames[model.runtime_backend] || model.runtime_backend}`
      : "";
    const protocolHelp = compatible ? "" : (workbench
      ? "<small>La prueba usa el contrato request/response propio de Workbench.</small>"
      : "<small>Necesita conocer el esquema de tensores del modelo para probarlo.</small>");
    const credentialHint = workbench ? "Opcional: usa la API key de Workbench si queda vacío" : "Opcional: usa el CDP token si queda vacío";
    const prepareActions = model.requires_input_type
      ? `<button class="primary prepare-cloudera" data-index="${index}" data-input-type="query">Preparar consulta</button><button class="secondary prepare-cloudera" data-index="${index}" data-input-type="passage">Preparar documentos</button>`
      : `<button class="primary prepare-cloudera" data-index="${index}">Preparar borrador</button>`;
    const roleHelp = model.requires_input_type
      ? "<small>Embedding asimétrico: usa query para preguntas y passage para documentos.</small>"
      : "";
    return `<article class="cloudera-model"><div><b>${escapeHtml(model.name)}</b><small>${escapeHtml(model.source)}${model.project ? ` · ${escapeHtml(model.project)}` : ""}</small></div><div class="model-state-stack">${stateIndicator(deployment)}${stateIndicator(credential)}<small>Cloudera informa: ${escapeHtml(model.state)}</small>${model.replica_count !== undefined && model.replica_count !== null ? `<small>Réplicas: ${escapeHtml(model.replica_count)}</small>` : ""}${model.token_expires_at ? `<small>Caduca: ${escapeHtml(madridTime(model.token_expires_at))}</small>` : ""}</div><div><span class="protocol ${compatible ? "compatible" : "adapter"}">${protocolName}</span><small>${escapeHtml(engineName)}${escapeHtml(backendName)} · ${escapeHtml(model.task_family || model.task || "tarea no publicada")}</small>${protocolHelp}${roleHelp}</div><div class="connection-url"><span>${escapeHtml(model.url || "Endpoint no incluido por la API")}</span>${model.url ? `<small>URL obtenida mediante ${escapeHtml(model.url_source || "la API de Cloudera")}</small>` : ""}</div><label>JWT / API key del modelo<input class="cloudera-model-token" data-index="${index}" type="password" autocomplete="new-password" placeholder="${model.has_model_token ? "Escribe un token nuevo para sustituir" : credentialHint}"></label><div class="row-actions"><button class="secondary save-cloudera-token" data-index="${index}">${model.has_model_token ? "Sustituir token" : "Guardar token propio"}</button><button class="secondary probe-cloudera" data-index="${index}" ${!model.url ? "disabled" : ""}>Probar acceso</button>${prepareActions}</div><p class="model-probe-status ${probe ? (probe.ok ? "success" : "error") : ""}" data-probe="${index}" role="status">${response}</p></article>`;
  }).join("") : '<p class="empty compact">La API no devolvió modelos visibles para esta conexión.</p>';
  document.querySelectorAll("[data-connection-models]").forEach((container) => {
    const entries = clouderaModels.map((model, index) => ({model, index}))
      .filter(({model}) => model.connection_id === container.dataset.connectionModels);
    container.innerHTML = modelMarkup(entries);
  });
  document.querySelectorAll(".save-cloudera-token").forEach((button) => button.onclick = () => saveClouderaToken(Number(button.dataset.index)));
  document.querySelectorAll(".probe-cloudera").forEach((button) => button.onclick = () => probeClouderaModel(Number(button.dataset.index), button));
  document.querySelectorAll(".prepare-cloudera").forEach((button) => button.onclick = () => prepareClouderaModel(Number(button.dataset.index), button.dataset.inputType || ""));
}

async function saveClouderaToken(index) {
  const model = clouderaModels[index];
  const input = document.querySelector(`.cloudera-model-token[data-index="${index}"]`);
  try {
    const result = await api(`/api/cloudera/connections/${model.connection_id}/models/${encodeURIComponent(model.external_id)}/token`, {method: "PUT", body: JSON.stringify({token: input.value})});
    model.has_model_token = true; model.api_key_env = result.api_key_env; model.credential_source = result.credential_source;
    model.token_expires_at = result.token_expires_at; model.token_expired = result.token_expired; input.value = "";
    setInlineStatus("#cloudera-status", `Credencial de ${model.name} guardada. Pulsa «Probar acceso» para validarla.`, "success");
    renderClouderaModels();
  } catch (exception) { setInlineStatus("#cloudera-status", exception.message, "error"); }
}

async function probeClouderaModel(index, button) {
  const model = clouderaModels[index]; const status = document.querySelector(`[data-probe="${index}"]`);
  button.disabled = true; button.textContent = "Probando…"; status.innerHTML = `${stateIndicator({level: "warning", label: "Comprobando respuesta"})}<span>Validando endpoint y credencial…</span>`; status.className = "model-probe-status searching";
  try {
    const result = await requestClouderaProbe(model);
    model.probe_result = result;
    renderClouderaModels(false);
    updateConnectionHealthSummaries();
  } catch (exception) {
    model.probe_result = {ok: false, message: exception.message, credential_source: model.credential_source || "—"};
    renderClouderaModels(false); updateConnectionHealthSummaries();
  }
  finally { button.disabled = false; button.textContent = "Probar acceso"; }
}

function prepareClouderaModel(index, inputType = "") {
  const model = clouderaModels[index]; cancelModelEdit();
  const connection = clouderaConnections.find((item) => item.id === model.connection_id);
  preparedClouderaSource = {
    source: "cloudera",
    cloudera_kind: connection?.kind || (model.source?.toLowerCase().includes("workbench") ? "workbench" : "inference"),
    serving_engine: model.serving_engine || "",
    task: model.task || "",
    embedding_input_type: inputType,
  };
  const roleSuffix = inputType ? `-${inputType}` : "";
  const canonicalModel = String(model.canonical_model_name || model.model_name || model.name).replace(/-(query|passage)$/i, "");
  $("#config-name").value = `${model.name}${roleSuffix}`.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  $("#config-model").value = model.protocol === "openai"
    ? `openai/${canonicalModel}${roleSuffix}`
    : model.protocol === "workbench"
      ? `cloudera_workbench/${model.model_name || model.name}`
      : `custom/${model.model_name || model.name}`;
  $("#config-api-base").value = (model.url || "").replace(/\/(chat\/completions|completions|embeddings)\/?$/, "");
  $("#config-api-key").value = model.api_key_env || "";
  const detectedBackend = connection?.kind === "workbench" ? "workbench" : model.serving_engine;
  $("#config-backend").value = ["vllm", "nim", "triton", "ollama", "workbench"].includes(detectedBackend)
    ? detectedBackend : "openai";
  $("#config-compatibility").value = "cloudera_1_5_5_sp3";
  updateModelParameterContext();
  const supported = ["openai", "workbench"].includes(model.protocol);
  setInlineStatus("#model-form-status", supported
    ? (inputType
      ? `Borrador ${inputType} preparado. Crea también el alias ${inputType === "query" ? "passage" : "query"} para completar el flujo RAG.`
      : "Borrador Cloudera preparado. Revisa los campos antes de añadirlo.")
    : "Borrador preparado: este protocolo necesita un adaptador LiteLLM personalizado.", supported ? "success" : "error");
  $("#model-form").scrollIntoView({behavior: "smooth", block: "start"});
}

/** Salud y proceso son conceptos distintos: la UI conserva esa distinción. */
async function loadStatus() {
  /** Refresca salud del proceso; se ejecuta cada tres segundos al final. */
  const status = await api("/api/status");
  gatewayProcessAlive = status.process_alive;
  $("#gateway-address").textContent = `${status.host}:${status.public_port || status.port}`;

  $("#status").classList.toggle("online", status.running);
  $("#status").classList.toggle("unhealthy", (status.process_alive && !status.running) || status.port_conflict);
  $("#status span").textContent = status.running
    ? `Activo · ${status.host}:${status.port} · PID ${status.pid}`
    : status.process_alive
      ? `Sin servicio · puerto ${status.port} · PID ${status.pid}`
      : status.port_conflict
        ? `Conflicto · puerto ${status.port} ocupado por otro proceso`
      : `Detenido · puerto ${status.port}`;

  $("#resources").textContent = status.process_alive
    ? `CPU ${status.cpu_percent ?? "—"}% del total (${status.cores} cores) · RAM ${status.memory_gb ?? "—"} GB`
    : "CPU — · RAM —";

  const persistence = status.persistence ?? {};
  const persistenceElement = $("#persistence");
  persistenceElement.textContent = `${persistence.credential_store || "SQLite"} · tokens sin reinicio · modelos con reinicio`;
  persistenceElement.classList.toggle("dynamic", !persistence.token_restart_required);

  const button = $("#gateway-button");
  button.disabled = false;
  button.textContent = status.process_alive ? "Detener" : "Arrancar";
  button.classList.toggle("stop", status.process_alive);
  $("#apply-config-button").hidden = !status.restart_pending;
}

function shellSingleQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function modelUsageExamples(model, index) {
  const isEmbedding = model.mode === "embedding";
  const endpoint = `${window.location.origin}${isEmbedding ? "/v1/embeddings" : "/v1/chat/completions"}`;
  const payload = isEmbedding
    ? {model: model.name, input: "Texto que convertir en vector"}
    : {model: model.name, messages: [{role: "user", content: "Hola"}]};
  const curl = [
    `curl -X POST ${shellSingleQuote(endpoint)} \\`,
    "  -H 'Content-Type: application/json' \\",
    `  --data ${shellSingleQuote(JSON.stringify(payload))}`,
  ].join("\n");
  const resultExpression = isEmbedding
    ? 'response.json()["data"][0]["embedding"]'
    : 'response.json()["choices"][0]["message"]["content"]';
  const python = `import requests\n\nresponse = requests.post(\n    ${JSON.stringify(endpoint)},\n    json=${JSON.stringify(payload, null, 4)},\n    timeout=60,\n)\nresponse.raise_for_status()\nprint(${resultExpression})`;
  return `<div class="model-help">
    <button class="model-help-trigger" type="button" aria-label="Cómo llamar al modelo ${escapeHtml(model.name)}" aria-expanded="false" aria-controls="model-help-${index}">?</button>
    <aside class="model-help-popover" id="model-help-${index}" role="region" aria-label="Ejemplos de uso de ${escapeHtml(model.name)}">
      <strong>Llamar a ${escapeHtml(model.name)}</strong>
      <span>cURL</span><pre><code>${escapeHtml(curl)}</code></pre>
      <span>Python</span><pre><code>${escapeHtml(python)}</code></pre>
    </aside>
  </div>`;
}

function modelCard(model, index) {
  /** Proyecta un modelo seguro del backend a una tarjeta puramente visual. */
  const sourceBadge = model.source === "cloudera"
    ? `<span class="source-badge cloudera">CLOUDERA · ${model.cloudera_kind === "workbench" ? "WORKBENCH" : "AI INFERENCE"}</span>`
    : "";
  return `
    <article class="model-card ${model.enabled ? "" : "disabled"}">
      <div class="model-head">
        <div>
          <span class="model-kicker">${model.mode === "embedding" ? "VECTOR" : "CHAT"}</span>${sourceBadge}
          <h3>${escapeHtml(model.name)}</h3>
        </div>
        <div class="model-head-actions">
          <span class="badge">${model.enabled ? "ACTIVO" : "INACTIVO"}</span>
          ${modelUsageExamples(model, index)}
        </div>
      </div>
      <p class="provider">${escapeHtml(model.provider_model)}</p>
      <div class="model-resources" data-resource-name="${escapeHtml(model.name)}">
        Calculando recursos…
      </div>
      <div class="meta">
        <span>${escapeHtml(model.api_base || "API por defecto")}</span>
        <button type="button" class="secondary model-advisor-button" data-advisor-target="${escapeHtml(model.name)}">Hablar con el asesor</button>
        <button
          data-model="${escapeHtml(model.name)}"
          data-enabled="${!model.enabled}"
          class="${model.enabled ? "danger" : "enable"}"
        >${model.enabled ? "Desactivar" : "Activar"}</button>
      </div>
    </article>`;
}

async function loadModels() {
  /** Recarga inventario, pestañas y formularios tras cualquier cambio YAML. */
  models = await api("/api/models");
  $("#model-count").textContent = `${models.filter((model) => model.enabled).length} activos / ${models.length}`;
  $("#models").innerHTML = models.map(modelCard).join("");
  $("#models").querySelectorAll("button[data-model]").forEach((button) => {
    button.onclick = () => toggleModel(button);
  });
  $("#models").querySelectorAll("button[data-advisor-target]").forEach((button) => {
    button.onclick = () => openAdvisor(button.dataset.advisorTarget, button);
  });
  $("#models").querySelectorAll(".model-help-trigger").forEach((button) => {
    button.onclick = (event) => {
      event.stopPropagation();
      const help = button.closest(".model-help");
      const open = !help.hasAttribute("data-open");
      document.querySelectorAll(".model-help[data-open]").forEach((item) => {
        item.removeAttribute("data-open");
        item.querySelector(".model-help-trigger")?.setAttribute("aria-expanded", "false");
      });
      if (open) help.setAttribute("data-open", "");
      button.setAttribute("aria-expanded", String(open));
    };
    button.onkeydown = (event) => {
      if (event.key === "Escape") {
        button.closest(".model-help").removeAttribute("data-open");
        button.setAttribute("aria-expanded", "false");
        button.focus();
      }
    };
  });
  document.onclick = (event) => {
    if (event.target.closest?.(".model-help")) return;
    document.querySelectorAll(".model-help[data-open]").forEach((item) => {
      item.removeAttribute("data-open");
      item.querySelector(".model-help-trigger")?.setAttribute("aria-expanded", "false");
    });
  };

  renderTestTabs();
  renderLogTabs();
  renderConfiguredModels();
  await Promise.all([loadLogs(), loadModelResources()]);
}

/** Las métricas remotas se etiquetan, nunca se simulan con datos locales. */
function metricStatus(kind, value) {
  /** Traduce una cifra a semáforo; el texto sigue siendo la fuente accesible. */
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
  /** Fusiona métricas de proceso Ollama y sondas de latencia de proveedores. */
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
    } else if (item.source === "cloudera") {
      const latency = remoteLatencies.get(item.name);
      const latencyRow = latency?.latency_ms !== undefined
        ? metricRow("Latencia (sonda)", `${latency.latency_ms} ms`, metricStatus("latency", latency.latency_ms))
        : metricRow("Latencia (sonda)", latency?.error || (gatewayProcessAlive ? "Pendiente" : "Gateway detenido"));
      target.innerHTML = [
        metricRow("Proveedor", `Cloudera · ${item.cloudera_kind === "workbench" ? "Workbench" : "AI Inference"}`),
        latencyRow,
      ].join("");
      target.className = "model-resources cloudera";
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

async function loadConfig() {
  /** Sincroniza formulario, guardrail y editor avanzado con la fuente YAML. */
  const config = await api("/api/config");
  $("#yaml-editor").value = config.content;
  const guardrail = config.dashboard_settings?.guardrail ?? {};
  $("#guardrail-enabled").checked = Boolean(guardrail.enabled);
  $("#guardrail-model").value = guardrail.model ?? "";
  $("#guardrail-policy").value = guardrail.policy ?? "warn";
  guardrailExcludedModels = new Set(guardrail.excluded_models ?? []);
  renderGuardrailPolicy();
  renderGuardrailExclusions();
  const advisor = config.dashboard_settings?.advisor ?? {};
  $("#advisor-enabled").checked = Boolean(advisor.enabled);
  $("#advisor-model").value = advisor.model ?? "";
}

function renderConfiguredModels() {
  $("#configured-models").innerHTML = models.length ? models.map((model) => `
    <article class="config-model-row">
      <div><b>${escapeHtml(model.name)}</b><small>Alias</small></div>
      <div><b>${escapeHtml(model.provider_model)}</b><small>Modelo LiteLLM</small></div>
      <div><b>${escapeHtml(model.backend_profile === "auto" ? (model.serving_engine || "Auto") : model.backend_profile)}</b><small>Backend</small></div>
      <div><b>${escapeHtml(model.context_window ? `${model.context_window} tokens` : "No declarada")}</b><small>Contexto</small></div>
      <div><b>${escapeHtml(model.default_max_tokens || "Por petición")}</b><small>Salida predeterminada</small></div>
      <div><b>${escapeHtml(model.reasoning_mode === "auto" ? "Según modelo" : model.reasoning_mode === "enabled" ? `Activo · ${model.reasoning_effort || "default"}` : "Desactivado")}</b><small>Razonamiento</small></div>
      <div><b>${escapeHtml(model.parameter_policy === "model_wins" ? "Impone el modelo" : "Puede sustituir el cliente")}</b><small>Parámetros</small></div>
      <div class="row-actions"><button class="secondary advise-model" data-name="${escapeHtml(model.name)}">Hablar con el asesor</button><button class="secondary edit-model" data-name="${escapeHtml(model.name)}">Editar</button><button class="danger delete-model" data-name="${escapeHtml(model.name)}">Eliminar</button></div>
    </article>`).join("") : '<p class="empty">No hay modelos configurados.</p>';
  document.querySelectorAll(".edit-model").forEach((button) => button.onclick = () => beginModelEdit(button.dataset.name));
  document.querySelectorAll(".advise-model").forEach((button) => button.onclick = () => openAdvisor(button.dataset.name, button));
  document.querySelectorAll(".delete-model").forEach((button) => button.onclick = () => confirmDeleteModel(button.dataset.name));
  const options = models.map((model) => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`).join("");
  const fallbackValue = $("#config-fallback").value;
  $("#config-fallback").innerHTML = `<option value="">Sin fallback</option>${options}`;
  $("#config-fallback").value = fallbackValue;
  const guardrailValue = $("#guardrail-model").value;
  $("#guardrail-model").innerHTML = `<option value="">Sin guardrail · llamada directa</option>${options}`;
  $("#guardrail-model").value = guardrailValue;
  renderGuardrailExclusions();
  const advisorValue = $("#advisor-model").value;
  $("#advisor-model").innerHTML = `<option value="">Sin asesor</option>${options}`;
  $("#advisor-model").value = advisorValue;
}

function parseExtraParameters(text) {
  const result = {};
  text.split("\n").map((line) => line.trim()).filter((line) => line && !line.startsWith("#")).forEach((line, index) => {
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error(`Extra línea ${index + 1}: usa VARIABLE=VALOR`);
    const key = line.slice(0, separator).trim();
    const raw = line.slice(separator + 1).trim();
    if (Object.hasOwn(result, key)) throw new Error(`Extra repetido: ${key}`);
    try { result[key] = JSON.parse(raw); } catch { result[key] = raw; }
  });
  return result;
}

function formatExtraParameters(values) {
  return Object.entries(values || {}).map(([key, value]) =>
    `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`
  ).join("\n");
}

function optionalNumber(selector) {
  const value = $(selector).value.trim();
  return value === "" ? null : Number(value);
}

function currentModelBackend() {
  const configured = $("#config-backend").value;
  const providerModel = $("#config-model").value.toLowerCase();
  return configured !== "auto" ? configured
    : preparedClouderaSource?.cloudera_kind === "workbench" ? "workbench"
      : preparedClouderaSource?.serving_engine || (providerModel.startsWith("ollama/") ? "ollama" : "openai");
}

function updateModelParameterContext() {
  const backend = currentModelBackend();
  const triton = backend === "triton";
  const supportsProviderSampling = ["vllm", "nim", "workbench", "ollama"].includes(backend);
  const generation = $("#generation-parameter-section");
  generation.setAttribute("aria-disabled", String(triton));
  generation.querySelectorAll("input, textarea, select").forEach((control) => { control.disabled = triton; });
  ["#config-top-k", "#config-min-p", "#config-repetition-penalty"].forEach((selector) => {
    $(selector).disabled = triton || !supportsProviderSampling;
  });
  $("#config-reasoning-mode").disabled = triton;
  $("#config-reasoning").disabled = triton || $("#config-reasoning-mode").value === "disabled";
  $("#config-preserve-thinking").disabled = backend !== "workbench" || triton;
  const compatibility = $("#config-compatibility").value;
  const hints = {
    triton: "Triton OIP recibe tensores. El contexto, batching y decoding se cambian en config.pbtxt o al desplegar el modelo, no como parámetros OpenAI.",
    workbench: compatibility === "cloudera_1_5_5_sp3"
      ? "Workbench 1.5.5 SP3 envía enable_thinking al wrapper y limita la salida predeterminada a 512 tokens para evitar dejar la réplica ocupada."
      : "Workbench envuelve los parámetros dentro de request; confirma en el predictor qué opciones admite el modelo.",
    nim: "NIM usa la API OpenAI compatible. Top K, Min P y repetición viajan en extra_body; el thinking se aplica mediante chat_template_kwargs cuando el modelo lo soporta.",
    vllm: "vLLM recibe Top K, Min P y repetición en extra_body. La ventana real sigue limitada por --max-model-len del deployment.",
    ollama: "LiteLLM traduce las opciones compatibles hacia Ollama. Keep alive controla cuánto permanece cargado el modelo.",
    openai: "Sólo se envían parámetros OpenAI estándar; las opciones específicas de vLLM/NIM se omiten.",
  };
  $("#model-parameter-hint").textContent = hints[backend] || hints.openai;
}

function applyModelPreset() {
  const backend = currentModelBackend();
  const providerSampling = ["vllm", "nim", "workbench", "ollama"].includes(backend);
  const compatibility = $("#config-compatibility").value;
  const workbench155 = backend === "workbench" && compatibility === "cloudera_1_5_5_sp3";
  const presets = {
    deterministic: {temperature: 0, topP: 1, maxTokens: 512, reasoning: "disabled", effort: "", retries: 0, parallel: ""},
    balanced: {temperature: 0.7, topP: 0.9, maxTokens: 512, reasoning: "auto", effort: "", retries: 1, parallel: ""},
    creative: {temperature: 1, topP: 0.95, topK: 50, repetition: 1.05, maxTokens: 1024, reasoning: "auto", effort: "", retries: 1, parallel: ""},
    reasoning: {temperature: 0.6, topP: 0.95, topK: 40, maxTokens: workbench155 ? 512 : 2048, reasoning: "enabled", effort: "high", retries: 0, parallel: ""},
    rag: {temperature: 0.2, topP: 0.9, topK: 40, repetition: 1.05, maxTokens: 1024, reasoning: "auto", effort: "", retries: 1, parallel: ""},
    throughput: {temperature: 0.2, topP: 0.9, maxTokens: 256, reasoning: "disabled", effort: "", retries: 0, parallel: 8},
  };
  const preset = presets[$("#config-use-case").value] || presets.balanced;
  $("#config-temperature").value = preset.temperature;
  $("#config-top-p").value = preset.topP;
  $("#config-top-k").value = providerSampling && preset.topK !== undefined ? preset.topK : "";
  $("#config-min-p").value = "";
  $("#config-repetition-penalty").value = providerSampling && preset.repetition !== undefined ? preset.repetition : "";
  $("#config-frequency-penalty").value = "";
  $("#config-presence-penalty").value = "";
  $("#config-seed").value = $("#config-use-case").value === "deterministic" ? 42 : "";
  $("#config-default-max-tokens").value = workbench155 ? Math.min(preset.maxTokens, 512) : preset.maxTokens;
  $("#config-reasoning-mode").value = preset.reasoning;
  $("#config-reasoning").value = preset.effort;
  $("#config-num-retries").value = preset.retries;
  $("#config-max-parallel").value = preset.parallel;
  updateModelParameterContext();
  setInlineStatus("#model-form-status", "Perfil aplicado. Ajusta contexto y salida a los límites reales del deployment.", "success");
}

function beginModelEdit(name) {
  const model = models.find((item) => item.name === name);
  if (!model) return;
  editingModelName = name;
  preparedClouderaSource = model.source === "cloudera" ? {
    source: "cloudera", cloudera_kind: model.cloudera_kind,
    serving_engine: model.serving_engine || "", task: model.task || "",
    embedding_input_type: model.embedding_input_type || "",
  } : null;
  $("#config-name").value = model.name;
  $("#config-model").value = model.provider_model;
  $("#config-api-base").value = model.api_base || "";
  $("#config-api-key").value = model.api_key?.startsWith("os.environ/") ? model.api_key.slice(11) : "";
  $("#config-backend").value = model.backend_profile || "auto";
  $("#config-compatibility").value = model.compatibility_profile || "auto";
  $("#config-context-window").value = model.context_window ?? "";
  $("#config-max-output-tokens").value = model.max_output_tokens ?? "";
  $("#config-default-max-tokens").value = model.default_max_tokens ?? "";
  $("#config-temperature").value = model.temperature ?? "";
  $("#config-top-p").value = model.top_p ?? "";
  $("#config-top-k").value = model.top_k ?? "";
  $("#config-min-p").value = model.min_p ?? "";
  $("#config-repetition-penalty").value = model.repetition_penalty ?? "";
  $("#config-frequency-penalty").value = model.frequency_penalty ?? "";
  $("#config-presence-penalty").value = model.presence_penalty ?? "";
  $("#config-seed").value = model.seed ?? "";
  $("#config-stop").value = Array.isArray(model.stop) ? model.stop.join("\n") : "";
  $("#config-reasoning-mode").value = model.reasoning_mode || "auto";
  $("#config-reasoning").value = model.reasoning_effort || "";
  $("#config-preserve-thinking").checked = Boolean(model.preserve_thinking);
  $("#config-num-retries").value = model.num_retries ?? "";
  $("#config-max-parallel").value = model.max_parallel_requests ?? "";
  $("#config-keep-alive").value = model.keep_alive ?? "";
  $("#config-timeout").value = model.timeout ?? "";
  $("#config-fallback").value = model.fallbacks?.[0] || "";
  $("#config-drop-params").checked = model.drop_params;
  $("#config-parameter-policy").value = model.parameter_policy || "caller_wins";
  $("#config-extra-parameters").value = formatExtraParameters(model.extra_parameters);
  $("#config-validation-payload").value = "";
  $("#config-validation-path").value = "";
  updateModelParameterContext();
  $("#save-model").textContent = "Guardar cambios";
  $("#cancel-model-edit").hidden = false;
  setInlineStatus("#model-form-status", `Editando ${name}`);
  $("#config-name").focus();
}

function cancelModelEdit() {
  editingModelName = null;
  preparedClouderaSource = null;
  $("#model-form").reset();
  $("#config-drop-params").checked = true;
  $("#config-parameter-policy").value = "caller_wins";
  updateModelParameterContext();
  $("#save-model").textContent = "Añadir modelo";
  $("#cancel-model-edit").hidden = true;
  setInlineStatus("#model-form-status", "");
}

async function confirmDeleteModel(name) {
  const dialog = $("#delete-dialog");
  $("#delete-message").textContent = `Vas a eliminar el alias «${name}» de config.yaml.`;
  dialog.showModal();
  await new Promise((resolve) => dialog.addEventListener("close", resolve, {once: true}));
  if (dialog.returnValue !== "delete") return;
  const restart = await askRestart();
  if (restart === null) return;
  try {
    const result = await api(`/api/config/models/${encodeURIComponent(name)}?restart=${restart}`, {method: "DELETE"});
    if (editingModelName === name) cancelModelEdit();
    await refreshAfterConfigChange(result);
  } catch (exception) { showError(exception); }
}

function askRestart() {
  if (!gatewayProcessAlive) return Promise.resolve(false);
  const dialog = $("#restart-dialog");
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    resolve(dialog.returnValue === "restart" ? true : dialog.returnValue === "later" ? false : null);
  }, { once: true }));
}

async function refreshAfterConfigChange(result) {
  $("#yaml-editor").value = result.content;
  remoteLatencies.clear();
  await loadStatus();
  await loadModels();
  await loadConfig();
  return result.restarted
    ? "Configuración guardada y LiteLLM reiniciado."
    : "Configuración guardada. Se aplicará al arrancar LiteLLM.";
}

async function addConfiguredModel(event) {
  /** Comparte el mismo formulario para CREATE y UPDATE y pregunta por reinicio. */
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  setInlineStatus("#model-form-status", "Validando y guardando…");
  const effectiveBackend = currentModelBackend();
  const tritonProfile = effectiveBackend === "triton";
  const providerSampling = ["vllm", "nim", "workbench", "ollama"].includes(effectiveBackend);
  const reasoningMode = tritonProfile ? "auto" : $("#config-reasoning-mode").value;
  let extraParameters;
  let validationPayload = {};
  try {
    extraParameters = parseExtraParameters($("#config-extra-parameters").value);
    const rawValidation = $("#config-validation-payload").value.trim();
    if (rawValidation) validationPayload = JSON.parse(rawValidation);
  } catch (exception) {
    setInlineStatus("#model-form-status", `No se puede probar: ${exception.message}`, "error");
    button.disabled = false;
    return;
  }
  const payload = {
    model_name: $("#config-name").value,
    model: $("#config-model").value,
    api_base: $("#config-api-base").value,
    api_key_env: $("#config-api-key").value,
    backend_profile: $("#config-backend").value,
    compatibility_profile: $("#config-compatibility").value,
    context_window: optionalNumber("#config-context-window"),
    max_output_tokens: optionalNumber("#config-max-output-tokens"),
    default_max_tokens: tritonProfile ? null : optionalNumber("#config-default-max-tokens"),
    temperature: tritonProfile ? null : optionalNumber("#config-temperature"),
    top_p: tritonProfile ? null : optionalNumber("#config-top-p"),
    top_k: providerSampling ? optionalNumber("#config-top-k") : null,
    min_p: providerSampling ? optionalNumber("#config-min-p") : null,
    repetition_penalty: providerSampling ? optionalNumber("#config-repetition-penalty") : null,
    frequency_penalty: tritonProfile ? null : optionalNumber("#config-frequency-penalty"),
    presence_penalty: tritonProfile ? null : optionalNumber("#config-presence-penalty"),
    seed: tritonProfile ? null : optionalNumber("#config-seed"),
    stop: tritonProfile ? [] : $("#config-stop").value.split("\n").map((value) => value.trim()).filter(Boolean),
    reasoning_mode: reasoningMode,
    reasoning_effort: reasoningMode === "disabled" ? "" : $("#config-reasoning").value,
    preserve_thinking: reasoningMode === "enabled" && $("#config-preserve-thinking").checked,
    num_retries: optionalNumber("#config-num-retries"),
    max_parallel_requests: optionalNumber("#config-max-parallel"),
    keep_alive: $("#config-keep-alive").value,
    timeout: $("#config-timeout").value ? Number($("#config-timeout").value) : null,
    fallback_model: $("#config-fallback").value,
    drop_params: $("#config-drop-params").checked,
    parameter_policy: $("#config-parameter-policy").value,
    extra_parameters: extraParameters,
    validation_payload: validationPayload,
    validation_path: $("#config-validation-path").value,
    source: preparedClouderaSource?.source || "",
    cloudera_kind: preparedClouderaSource?.cloudera_kind || "",
    serving_engine: preparedClouderaSource?.serving_engine || "",
    task: preparedClouderaSource?.task || "",
    embedding_input_type: preparedClouderaSource?.embedding_input_type || "",
  };
  try {
    setInlineStatus("#model-form-status", "Probando el deployment con todos los parámetros…");
    const validation = await api("/api/config/models/validate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    payload.validation_id = validation.validation_id;
    setInlineStatus("#model-form-status", "Prueba correcta. Preparando el guardado…", "success");
    const restart = await askRestart();
    if (restart === null) return;
    payload.restart = restart;
    const endpoint = editingModelName ? `/api/config/models/${encodeURIComponent(editingModelName)}` : "/api/config/models";
    const result = await api(endpoint, {
      method: editingModelName ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    const wasEditing = Boolean(editingModelName);
    cancelModelEdit();
    const message = await refreshAfterConfigChange(result);
    setInlineStatus("#model-form-status", `${wasEditing ? "Modelo modificado. " : "Modelo añadido. "}${message}`, "success");
  } catch (exception) {
    setInlineStatus("#model-form-status", exception.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function saveYaml() {
  /** Valida en servidor y sólo después ofrece sustitución transaccional. */
  const button = $("#save-yaml");
  button.disabled = true;
  setInlineStatus("#yaml-status", "Validando y guardando…");
  try {
    const validation = await validateYaml();
    const restart = await askRestart();
    if (restart === null) return;
    const result = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ content: $("#yaml-editor").value, restart }),
    });
    const message = await refreshAfterConfigChange(result);
    setInlineStatus("#yaml-status", message, "success");
  } catch (exception) {
    setInlineStatus("#yaml-status", exception.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function importYamlBackup() {
  /** Restaura sólo tras validar; el servidor conserva rollback y política de reinicio. */
  const input = $("#yaml-import-file");
  const button = $("#import-yaml-backup");
  const file = input.files?.[0];
  if (!file) { setInlineStatus("#yaml-import-status", "Selecciona primero un fichero .yaml o .yml.", "error"); return; }
  if (!/\.ya?ml$/i.test(file.name)) { setInlineStatus("#yaml-import-status", "El backup debe tener extensión .yaml o .yml.", "error"); return; }
  if (!file.size) { setInlineStatus("#yaml-import-status", "El fichero seleccionado está vacío.", "error"); return; }
  if (file.size > 1_000_000) { setInlineStatus("#yaml-import-status", "El backup supera el máximo permitido de 1 MB.", "error"); return; }
  button.disabled = true;
  setInlineStatus("#yaml-import-status", `Validando ${file.name}…`);
  try {
    const content = await file.text();
    if (content.includes("\u0000")) throw new Error("El fichero no es un YAML de texto válido.");
    const validation = await api("/api/config/validate", {method: "POST", body: JSON.stringify({content})});
    const restart = await askRestart();
    if (restart === null) { setInlineStatus("#yaml-import-status", "Importación cancelada; config.yaml no ha cambiado."); return; }
    const result = await api("/api/config", {method: "PUT", body: JSON.stringify({content, restart})});
    const message = await refreshAfterConfigChange(result);
    input.value = "";
    setInlineStatus("#yaml-import-status", `Backup importado: ${validation.model_count} modelos. ${message}`, "success");
  } catch (exception) {
    setInlineStatus("#yaml-import-status", `No se pudo importar: ${exception.message}`, "error");
  } finally {
    button.disabled = !input.files?.length;
  }
}

async function validateYaml() {
  setInlineStatus("#yaml-status", "Validando…");
  try {
    const result = await api("/api/config/validate", {method: "POST", body: JSON.stringify({content: $("#yaml-editor").value})});
    const missing = result.missing_environment_variables.length ? ` · faltan: ${result.missing_environment_variables.join(", ")}` : " · variables disponibles";
    setInlineStatus("#yaml-status", `YAML válido · ${result.model_count} modelos${missing}`, result.missing_environment_variables.length ? "error" : "success");
    return result;
  } catch (exception) {
    setInlineStatus("#yaml-status", exception.message, "error");
    throw exception;
  }
}

async function updateGuardrail() {
  if ($("#guardrail-enabled").checked && !$("#guardrail-model").value) {
    $("#guardrail-enabled").checked = false;
    showError(new Error("Selecciona primero el modelo que actuará como guardrail."));
    return;
  }
  const restart = await askRestart();
  if (restart === null) return;
  try {
    const selectedExclusions = [...document.querySelectorAll(".guardrail-exclusion:checked:not(:disabled)")]
      .map((input) => input.value);
    const result = await api("/api/config/guardrail", {method: "PUT", body: JSON.stringify({enabled: $("#guardrail-enabled").checked, model: $("#guardrail-model").value, policy: $("#guardrail-policy").value, excluded_models: selectedExclusions, restart})});
    guardrailExcludedModels = new Set(selectedExclusions);
    $("#yaml-editor").value = result.content;
    const message = !$("#guardrail-enabled").checked
      ? "Guardrail desactivado: las llamadas irán directamente al modelo elegido."
      : $("#guardrail-policy").value === "block"
        ? "Guardrail actualizado en política restringida."
        : "Guardrail actualizado en política permisiva.";
    setInlineStatus("#yaml-status", message, "success");
    await Promise.all([loadStatus(), loadModels()]);
    await loadConfig();
  } catch (exception) { showError(exception); }
}

function renderGuardrailExclusions() {
  const target = $("#guardrail-excluded-models");
  if (!target) return;
  const guardrailModel = $("#guardrail-model").value;
  const candidates = models.filter((model) => model.name !== guardrailModel);
  target.innerHTML = candidates.length ? candidates.map((model) => {
    const automatic = model.mode === "embedding";
    const checked = automatic || guardrailExcludedModels.has(model.name);
    return `<label><input class="guardrail-exclusion" type="checkbox" value="${escapeHtml(model.name)}" ${checked ? "checked" : ""} ${automatic ? "disabled" : ""}> <span>${escapeHtml(model.name)}</span>${automatic ? " <small>Embedding · automático</small>" : ""}</label>`;
  }).join("") : '<span class="empty compact">No hay otros modelos configurados.</span>';
}

function renderGuardrailPolicy() {
  if (!$("#guardrail-enabled").checked || !$("#guardrail-model").value) {
    $("#policy-chip").textContent = "Guardrail desactivado";
    $("#policy-chip").classList.remove("restricted");
    $("#guardrail-policy").disabled = true;
    return;
  }
  $("#guardrail-policy").disabled = false;
  const restricted = $("#guardrail-policy").value === "block";
  $("#policy-chip").textContent = restricted ? "Política restringida · bloquea" : "Política permisiva · avisa y continúa";
  $("#policy-chip").classList.toggle("restricted", restricted);
}

async function updateAdvisor() {
  if ($("#advisor-enabled").checked && !$("#advisor-model").value) {
    $("#advisor-enabled").checked = false;
    showError(new Error("Selecciona primero el modelo que actuará como asesor."));
    return;
  }
  const restart = await askRestart();
  if (restart === null) return;
  try {
    const result = await api("/api/config/advisor", {
      method: "PUT",
      body: JSON.stringify({enabled: $("#advisor-enabled").checked, model: $("#advisor-model").value, restart}),
    });
    $("#yaml-editor").value = result.content;
    setInlineStatus("#advisor-status", $("#advisor-enabled").checked ? "Asesor disponible." : "Asesor desactivado.", "success");
    await Promise.all([loadStatus(), loadModels()]);
  } catch (exception) { setInlineStatus("#advisor-status", exception.message, "error"); }
}

function openAdvisor(name, trigger) {
  if (!$("#advisor-enabled").checked || !$("#advisor-model").value) {
    showError(new Error("Habilita y selecciona primero el modelo asesor en Configuración."));
    return;
  }
  advisorTargetName = name;
  advisorTrigger = trigger;
  advisorRecommendation = null;
  $("#advisor-target").textContent = `Modelo objetivo: ${name}`;
  $("#advisor-use-case").value = "";
  $("#advisor-recommendation").hidden = true;
  $("#advisor-apply").hidden = true;
  setInlineStatus("#advisor-dialog-status", "");
  $("#advisor-dialog").showModal();
  $("#advisor-use-case").focus();
}

function closeAdvisor() {
  $("#advisor-dialog").close();
  advisorTrigger?.focus();
}

async function askAdvisor(event) {
  event.preventDefault();
  const button = $("#advisor-ask");
  button.disabled = true;
  setInlineStatus("#advisor-dialog-status", "Analizando el caso de uso…");
  try {
    advisorRecommendation = await api("/api/config/advisor/recommend", {
      method: "POST",
      body: JSON.stringify({model_name: advisorTargetName, use_case: $("#advisor-use-case").value}),
    });
    $("#advisor-summary").textContent = advisorRecommendation.summary;
    $("#advisor-rationale").innerHTML = advisorRecommendation.rationale.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    $("#advisor-parameters").textContent = JSON.stringify(advisorRecommendation.parameters, null, 2);
    $("#advisor-recommendation").hidden = false;
    $("#advisor-apply").hidden = false;
    setInlineStatus("#advisor-dialog-status", "Recomendación lista. Revísala antes de aplicarla.", "success");
  } catch (exception) { setInlineStatus("#advisor-dialog-status", exception.message, "error"); }
  finally { button.disabled = false; }
}

function applyAdvisorRecommendation() {
  if (!advisorRecommendation) return;
  beginModelEdit(advisorTargetName);
  const parameters = advisorRecommendation.parameters || {};
  const mapping = {
    temperature: "#config-temperature", top_p: "#config-top-p", top_k: "#config-top-k",
    min_p: "#config-min-p", repetition_penalty: "#config-repetition-penalty",
    frequency_penalty: "#config-frequency-penalty", presence_penalty: "#config-presence-penalty",
    seed: "#config-seed", max_tokens: "#config-default-max-tokens",
    reasoning_mode: "#config-reasoning-mode", reasoning_effort: "#config-reasoning",
    parameter_policy: "#config-parameter-policy",
  };
  Object.entries(mapping).forEach(([key, selector]) => {
    if (parameters[key] !== undefined && parameters[key] !== null) $(selector).value = parameters[key];
  });
  if (Array.isArray(parameters.stop)) $("#config-stop").value = parameters.stop.join("\n");
  if (parameters.extra_parameters && typeof parameters.extra_parameters === "object") {
    $("#config-extra-parameters").value = formatExtraParameters(parameters.extra_parameters);
  }
  updateModelParameterContext();
  setInlineStatus("#model-form-status", `Recomendación del asesor aplicada a ${advisorTargetName}. Debes guardarla y superar la prueba real.`, "success");
  closeAdvisor();
  $("#model-form").scrollIntoView({behavior: "smooth", block: "start"});
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
  /** Evita un selector opaco: cada alias activo queda visible como pestaña. */
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
      loadLogDays().then(loadLogs).catch(showError);
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
  return new Intl.DateTimeFormat(window.IAGatewayI18n?.localeTag() || "es-ES", {
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
      ${log.guardrail_status ? `<div class="guardrail-result ${escapeHtml(log.guardrail_status)}"><b>Guardrail:</b> ${log.guardrail_status === "warning" ? "⚠ Riesgo detectado; la petición continuó" : log.guardrail_status === "safe" ? "✓ Contenido clasificado como seguro" : "⚠ No se pudo evaluar; la petición continuó"}<span>${escapeHtml(log.guardrail_reason || "")}</span></div>` : ""}
      <details class="parameter-log"><summary>Parámetros efectivos enviados al proveedor</summary><pre>${escapeHtml(JSON.stringify(log.parameters || {}, null, 2))}</pre></details>
      <div class="io-grid">
        <div><h4>Pregunta / entrada</h4><pre>${escapeHtml(JSON.stringify(log.request, null, 2))}</pre></div>
        <div><h4>Respuesta / salida</h4><pre>${escapeHtml(log.error || JSON.stringify(log.response, null, 2))}</pre></div>
      </div>
    </article>`;
}

function displayPayload(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  const messages = value.messages;
  if (Array.isArray(messages)) return messages.map((message) => `${message.role || "mensaje"}: ${typeof message.content === "string" ? message.content : JSON.stringify(message.content)}`).join("\n");
  if (typeof value.input === "string") return value.input;
  const content = value.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  return JSON.stringify(value, null, 2);
}

function renderKpis(kpis) {
  const cards = [
    ["Peticiones", kpis.requests, "Volumen de la jornada"],
    ["Tasa de éxito", kpis.success_rate === null ? "—" : `${kpis.success_rate}%`, `${kpis.errors} errores`],
    ["Latencia media", kpis.avg_duration_ms === null ? "—" : `${kpis.avg_duration_ms} ms`, `P95: ${kpis.p95_duration_ms ?? "—"} ms`],
    ["Inicio de respuesta", kpis.avg_ttft_ms === null ? "—" : `${kpis.avg_ttft_ms} ms`, "TTFT medio"],
    ["Alertas guardrail", kpis.guardrail_warnings, "Contenido marcado como riesgo"],
    ["Tokens", kpis.total_tokens || "—", `${kpis.prompt_tokens} entrada · ${kpis.completion_tokens} salida`],
  ];
  $("#log-kpis").innerHTML = cards.map(([label, value, note]) => `<article class="kpi-card"><span>${label}</span><strong>${value}</strong><small>${note}</small></article>`).join("");
  const peak = Math.max(...kpis.hourly, 0);
  $("#chart-summary").textContent = peak ? `Máximo ${peak} peticiones en una hora` : "Sin actividad";
  $("#hourly-chart").innerHTML = kpis.hourly.map((value, hour) => {
    const height = peak && value ? Math.max(3, value / peak * 100) : 3;
    const y = 100 - height;
    return `<div class="hour-column" title="${String(hour).padStart(2, "0")}:00 · ${value} peticiones"><span>${value || ""}</span><svg class="hour-bar" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="${y}" width="100" height="${height}" rx="3"></rect></svg><small>${[0, 6, 12, 18, 23].includes(hour) ? String(hour).padStart(2, "0") : ""}</small></div>`;
  }).join("");
}

function logTable(rows) {
  if (!rows.length) return '<p class="empty">No hay peticiones para este modelo y jornada.</p>';
  return `<div class="log-table-wrap"><table class="log-table"><thead><tr><th>Hora</th><th>Estado</th><th>Origen</th><th>Destino</th><th>Parámetros efectivos</th><th>Pregunta / entrada</th><th>Respuesta / salida</th><th>TTFT</th><th>Total</th><th>Guardrail</th></tr></thead><tbody>${rows.map((log) => {
    const time = madridTime(log.started_at || log.created_at);
    const request = displayPayload(log.request);
    const response = log.error || displayPayload(log.response);
    const guardrail = log.guardrail_status === "warning" ? "Riesgo · continuó" : log.guardrail_status === "safe" ? "Seguro" : log.guardrail_status === "unavailable" ? "No disponible" : "—";
    const parameters = JSON.stringify(log.parameters || {}, null, 2);
    return `<tr><td class="nowrap">${escapeHtml(time.split(", ").pop())}</td><td><span class="table-status ${log.status}">${log.status === "success" ? "Correcto" : "Error"}</span></td><td>${escapeHtml(log.origin_ip || "—")}</td><td>${escapeHtml(log.provider_ip || "—")}</td><td><details><summary>${escapeHtml(parameters.slice(0, 100))}</summary><pre>${escapeHtml(parameters)}</pre></details></td><td><details><summary>${escapeHtml(request.slice(0, 100))}</summary><pre>${escapeHtml(request)}</pre></details></td><td><details><summary>${escapeHtml(response.slice(0, 100))}</summary><pre>${escapeHtml(response)}</pre></details></td><td class="number">${log.ttft_ms ?? "—"}</td><td class="number">${log.duration_ms ?? "—"}</td><td><span class="guardrail-table ${escapeHtml(log.guardrail_status || "none")}">${guardrail}</span>${log.guardrail_reason ? `<details><summary>Detalle</summary><p>${escapeHtml(log.guardrail_reason)}</p></details>` : ""}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

async function loadLogDays() {
  const endpoint = selectedLogModel === "__litellm__" ? "/api/process-log-days" : `/api/models/${encodeURIComponent(selectedLogModel)}/log-days`;
  const data = await api(endpoint);
  const today = new Date().toLocaleDateString("en-CA", {timeZone: "Europe/Madrid"});
  const days = [...new Set([today, ...data.days])];
  if (!days.includes(selectedLogDay)) selectedLogDay = days[0];
  $("#log-day").innerHTML = days.map((day) => `<option value="${day}" ${day === selectedLogDay ? "selected" : ""}>${day === today ? `Hoy · ${day}` : day}</option>`).join("");
}

async function loadLogs() {
  /** Alterna entre stdout técnico y dashboard estructurado por modelo/día. */
  if (selectedLogModel === "__litellm__") {
    const raw = await api(`/api/process-log?day=${encodeURIComponent(selectedLogDay)}`);
    $("#log-dashboard").hidden = true;
    $("#download-logs").hidden = true;
    $("#clear-process-log").hidden = false;
    $("#logs").innerHTML = raw
      ? `<pre class="process-log">${escapeHtml(raw)}</pre>`
      : '<p class="empty">LiteLLM todavía no ha generado salida de proceso.</p>';
    const processLog = $(".process-log");
    if (processLog) processLog.scrollTop = processLog.scrollHeight;
    return;
  }
  if (!selectedLogModel) return;
  const data = await api(`/api/models/${encodeURIComponent(selectedLogModel)}/logs?day=${encodeURIComponent(selectedLogDay)}&limit=5000`);
  $("#log-dashboard").hidden = false;
  $("#download-logs").hidden = false;
  $("#clear-process-log").hidden = true;
  $("#download-logs").href = `/api/models/${encodeURIComponent(selectedLogModel)}/logs.xlsx?day=${encodeURIComponent(selectedLogDay)}`;
  renderKpis(data.kpis);
  $("#logs").innerHTML = logTable(data.rows);
  const tableLog = $(".log-table-wrap");
  if (tableLog) tableLog.scrollTop = tableLog.scrollHeight;
}

async function clearSelectedProcessLog() {
  const dialog = $("#clear-log-dialog");
  dialog.showModal();
  await new Promise((resolve) => dialog.addEventListener("close", resolve, {once: true}));
  if (dialog.returnValue !== "clear") return;
  await api(`/api/process-log?day=${encodeURIComponent(selectedLogDay)}`, {method: "DELETE"});
  await loadLogs();
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
  /** Recorre navegador→FastAPI→LiteLLM→proveedor y mide el tiempo total. */
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

$("#refresh").onclick = () => loadLogDays().then(loadLogs).catch(showError);
$("#clear-process-log").onclick = () => clearSelectedProcessLog().catch(showError);
$("#log-day").onchange = () => { selectedLogDay = $("#log-day").value; loadLogs().catch(showError); };
$("#test-button").onclick = runTest;
$("#model-form").onsubmit = addConfiguredModel;
$("#apply-model-preset").onclick = applyModelPreset;
$("#config-backend").onchange = updateModelParameterContext;
$("#config-compatibility").onchange = updateModelParameterContext;
$("#config-reasoning-mode").onchange = updateModelParameterContext;
$("#config-model").oninput = updateModelParameterContext;
$("#save-yaml").onclick = saveYaml;
$("#validate-yaml").onclick = () => validateYaml().catch(() => {});
$("#yaml-import-file").onchange = () => {
  const file = $("#yaml-import-file").files?.[0];
  $("#import-yaml-backup").disabled = !file;
  setInlineStatus("#yaml-import-status", file ? `Seleccionado: ${file.name}` : "");
};
$("#import-yaml-backup").onclick = importYamlBackup;
$("#guardrail-enabled").onchange = () => { renderGuardrailPolicy(); updateGuardrail(); };
$("#guardrail-model").onchange = () => { $("#guardrail-enabled").checked = Boolean($("#guardrail-model").value); renderGuardrailPolicy(); renderGuardrailExclusions(); updateGuardrail(); };
$("#guardrail-policy").onchange = () => { renderGuardrailPolicy(); if ($("#guardrail-enabled").checked) updateGuardrail(); };
$("#save-guardrail-exclusions").onclick = updateGuardrail;
$("#advisor-enabled").onchange = updateAdvisor;
$("#advisor-model").onchange = () => { $("#advisor-enabled").checked = Boolean($("#advisor-model").value); updateAdvisor(); };
$("#advisor-form").onsubmit = askAdvisor;
$("#advisor-cancel").onclick = closeAdvisor;
$("#advisor-apply").onclick = applyAdvisorRecommendation;
$("#advisor-dialog").addEventListener("close", () => advisorTrigger?.focus());
$("#cancel-model-edit").onclick = cancelModelEdit;
$("#cloudera-connection-form").onsubmit = saveClouderaConnection;
$("#cloudera-platform").onchange = updateClouderaFormContext;
$("#cloudera-kind").onchange = updateClouderaFormContext;
$("#cloudera-onpremise-version").onchange = updateClouderaOnpremCompatibility;
$("#cloudera-cai-version").onchange = updateClouderaOnpremCompatibility;
$("#cloudera-credential-type").onchange = updateClouderaOnpremCompatibility;
$("#cloudera-onprem-tls-verification").onchange = updateClouderaTlsFields;
document.querySelectorAll('input[name="cloudera-auth-mode"]').forEach((radio) => {
  radio.onchange = updateClouderaOnpremCompatibility;
});
$("#cancel-cloudera-edit").onclick = () => { cancelClouderaEdit(); setInlineStatus("#cloudera-status", "Edición cancelada."); };
$("#apply-config-button").onclick = async () => { try { await api("/api/config/apply", {method: "POST"}); await loadStatus(); } catch (error) { showError(error); } };

document.querySelectorAll(".primary-tab").forEach((button) => button.onclick = () => {
  document.querySelectorAll(".primary-tab").forEach((tab) => { tab.classList.toggle("active", tab === button); tab.setAttribute("aria-pressed", String(tab === button)); });
  document.querySelectorAll(".page-view").forEach((view) => { view.hidden = view.id !== `view-${button.dataset.view}`; });
  history.replaceState(null, "", `?tab=${button.dataset.view}`);
  if (button.dataset.view === "logs") loadLogDays().then(loadLogs).catch(showError);
  if (button.dataset.view === "config") refreshClouderaConnections().catch(() => {});
});

// La inicialización secuencial garantiza que la sonda sólo se lance si el gateway está listo.
async function initialize() {
  /** Secuencia de arranque: primero estado/modelos; después datos secundarios. */
  await loadStatus();
  await loadModels();
  await loadConfig();
  await loadClouderaConnections();
  renderConfiguredModels();
  updateModelParameterContext();
  const requestedTab = new URLSearchParams(location.search).get("tab");
  document.querySelector(`.primary-tab[data-view="${requestedTab}"]`)?.click();
  if (gatewayProcessAlive) await loadRemoteLatencies();
}

async function authenticationBootstrap() {
  const status = await api("/api/auth/status");
  if (!status.authenticated) {
    $("#login-screen").hidden = false;
    $("#dashboard").hidden = true;
    $("#login-password").focus();
    return;
  }
  csrfToken = status.csrf_token;
  dashboardAuthenticated = true;
  $("#login-screen").hidden = true;
  $("#dashboard").hidden = false;
  if (status.must_change_password) { openPasswordDialog(true); return; }
  await initialize();
}

function openPasswordDialog(required = false) {
  const dialog = $("#password-dialog");
  dialog.dataset.required = String(required);
  $("#cancel-password").hidden = required;
  $("#password-help").textContent = required
    ? "Por seguridad debes sustituir la contraseña inicial antes de usar el panel. Usa al menos 12 caracteres, mayúscula, minúscula, número y símbolo."
    : "Usa al menos 12 caracteres, mayúscula, minúscula, número y símbolo.";
  $("#password-form").reset();
  setInlineStatus("#password-status", "");
  dialog.showModal();
  $("#current-password").focus();
}

$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  setInlineStatus("#login-status", "Comprobando…");
  try {
    const status = await api("/api/auth/login", {method: "POST", body: JSON.stringify({username: $("#login-username").value, password: $("#login-password").value})});
    csrfToken = status.csrf_token;
    dashboardAuthenticated = true;
    $("#login-screen").hidden = true; $("#dashboard").hidden = false;
    if (status.must_change_password) { openPasswordDialog(true); return; }
    await initialize();
  } catch (error) { setInlineStatus("#login-status", error.message, "error"); }
  finally { button.disabled = false; }
};

$("#change-password-button").onclick = () => openPasswordDialog(false);
$("#cancel-password").onclick = () => $("#password-dialog").close();
$("#password-form").onsubmit = async (event) => {
  event.preventDefault();
  if ($("#new-password").value !== $("#confirm-password").value) {
    setInlineStatus("#password-status", "Las nuevas contraseñas no coinciden.", "error"); return;
  }
  try {
    const status = await api("/api/auth/change-password", {method: "POST", body: JSON.stringify({current_password: $("#current-password").value, new_password: $("#new-password").value})});
    csrfToken = status.csrf_token; $("#password-dialog").close(); await initialize();
  } catch (error) { setInlineStatus("#password-status", error.message, "error"); }
};
$("#logout-button").onclick = async () => {
  await api("/api/auth/logout", {method: "POST"});
  csrfToken = ""; dashboardAuthenticated = false; location.reload();
};
$("#password-dialog").addEventListener("cancel", (event) => {
  if ($("#password-dialog").dataset.required === "true") event.preventDefault();
});

updateClouderaFormContext();
authenticationBootstrap().catch(showError);
window.setInterval(() => {
  if (dashboardAuthenticated) Promise.all([loadStatus(), loadModelResources()]).catch(() => {});
}, 3000);
window.setInterval(() => {
  if (dashboardAuthenticated && !$("#view-logs").hidden) loadLogs().catch(() => {});
}, 10000);
window.setInterval(() => {
  if (dashboardAuthenticated && !$("#view-config").hidden) refreshClouderaConnections().catch(() => {});
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && dashboardAuthenticated && !$("#view-config").hidden) {
    refreshClouderaConnections().catch(() => {});
  }
});
