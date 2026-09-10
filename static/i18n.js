/* Lightweight, dependency-free UI localization for static and dynamic DOM content. */
(() => {
  "use strict";

  const supported = new Set(["es", "en", "it"]);
  const localeTags = {es: "es-ES", en: "en-GB", it: "it-IT"};
  const saved = localStorage.getItem("ia-gateway-language");
  let language = supported.has(saved)
    ? saved
    : supported.has(navigator.language?.slice(0, 2)) ? navigator.language.slice(0, 2) : "es";

  const rows = [
    ["Idioma", "Language", "Lingua"],
    ["Idioma de la interfaz", "Interface language", "Lingua dell'interfaccia"],
    ["ACCESO SEGURO · HTTPS", "SECURE ACCESS · HTTPS", "ACCESSO SICURO · HTTPS"],
    ["Identifícate para administrar modelos, credenciales y logs.", "Sign in to manage models, credentials and logs.", "Accedi per gestire modelli, credenziali e log."],
    ["Usuario", "Username", "Utente"], ["Contraseña", "Password", "Password"],
    ["Entrar", "Sign in", "Accedi"],
    ["En el primer acceso usa admin y cambia la contraseña inmediatamente.", "On first access, use admin and change the password immediately.", "Al primo accesso usa admin e cambia immediatamente la password."],
    ["En el primer acceso usa", "On first access, use", "Al primo accesso usa"],
    ["y cambia la contraseña inmediatamente.", "and change the password immediately.", "e cambia immediatamente la password."],
    ["CONTROL LOCAL", "LOCAL CONTROL", "CONTROLLO LOCALE"],
    ["Panel de LiteLLM ·", "LiteLLM dashboard ·", "Pannello LiteLLM ·"],
    ["Comprobando…", "Checking…", "Verifica…"],
    ["SQLite · tokens sin reinicio · modelos con reinicio", "SQLite · tokens without restart · models require restart", "SQLite · token senza riavvio · i modelli richiedono riavvio"],
    ["Cambiar password", "Change password", "Cambia password"], ["Salir", "Sign out", "Esci"],
    ["Aplicar cambios pendientes", "Apply pending changes", "Applica modifiche in sospeso"],
    ["Arrancar", "Start", "Avvia"], ["Detener", "Stop", "Arresta"],
    ["Secciones principales", "Main sections", "Sezioni principali"],
    ["Modelos", "Models", "Modelli"], ["Configuración", "Configuration", "Configurazione"], ["Logs", "Logs", "Log"],
    ["Inventario y estado operativo", "Inventory and operational status", "Inventario e stato operativo"],
    ["Cargando modelos…", "Loading models…", "Caricamento modelli…"],
    ["Prueba rápida", "Quick test", "Test rapido"],
    ["Envía una llamada sencilla a cualquiera de los modelos activos", "Send a simple request to any active model", "Invia una richiesta semplice a qualsiasi modello attivo"],
    ["Escribe aquí el mensaje de prueba…", "Type the test message here…", "Scrivi qui il messaggio di prova…"],
    ["Enviar prueba", "Send test", "Invia test"], ["Resultado", "Result", "Risultato"],
    ["Configuración YAML", "YAML configuration", "Configurazione YAML"],
    ["Gestiona modelos, resiliencia y seguridad sin editar archivos a mano", "Manage models, resilience and security without editing files by hand", "Gestisci modelli, resilienza e sicurezza senza modificare manualmente i file"],
    ["Modelos existentes", "Existing models", "Modelli esistenti"],
    ["Los secretos sólo muestran su referencia", "Secrets only show their reference", "I segreti mostrano solo il loro riferimento"],
    ["Añadir modelo", "Add model", "Aggiungi modello"], ["Modo guiado", "Guided mode", "Modalità guidata"],
    ["Alias", "Alias", "Alias"], ["Modelo LiteLLM", "LiteLLM model", "Modello LiteLLM"],
    ["API base", "API base", "API base"], ["(opcional)", "(optional)", "(opzionale)"],
    ["Variable API key", "API key variable", "Variabile API key"], ["(nunca el secreto)", "(never the secret)", "(mai il segreto)"],
    ["Razonamiento", "Reasoning", "Ragionamento"], ["Por defecto", "Default", "Predefinito"],
    ["Timeout (segundos)", "Timeout (seconds)", "Timeout (secondi)"], ["Si falla, usar", "On failure, use", "In caso di errore, usa"],
    ["Sin fallback", "No fallback", "Nessun fallback"],
    ["Descartar parámetros incompatibles", "Drop incompatible parameters", "Scarta parametri incompatibili"],
    ["Cancelar edición", "Cancel editing", "Annulla modifica"],
    ["SEGURIDAD CONFIGURABLE", "CONFIGURABLE SECURITY", "SICUREZZA CONFIGURABILE"],
    ["Guardrail previo opcional", "Optional pre-request guardrail", "Guardrail preventivo opzionale"],
    ["Sin modelo seleccionado, las peticiones pasan directamente al modelo principal. Si eliges uno, clasificará primero cada conversación.", "Without a selected model, requests go directly to the main model. If you choose one, it will classify each conversation first.", "Senza un modello selezionato, le richieste vanno direttamente al modello principale. Se ne scegli uno, classificherà prima ogni conversazione."],
    ["Aplicar antes de los modelos de chat", "Apply before chat models", "Applica prima dei modelli di chat"],
    ["Modelo guardrail", "Guardrail model", "Modello guardrail"], ["Política", "Policy", "Criterio"],
    ["Permisiva · avisar y continuar", "Permissive · warn and continue", "Permissiva · avvisa e continua"],
    ["Restringida · bloquear", "Restricted · block", "Restrittiva · blocca"],
    ["Guardrail desactivado", "Guardrail disabled", "Guardrail disattivato"],
    ["Política restringida · bloquea", "Restricted policy · blocks", "Criterio restrittivo · blocca"],
    ["Política permisiva · avisa y continúa", "Permissive policy · warns and continues", "Criterio permissivo · avvisa e continua"],
    ["INTEGRACIÓN OPCIONAL", "OPTIONAL INTEGRATION", "INTEGRAZIONE OPZIONALE"],
    ["Descubre Model Endpoints de AI Inference y modelos desplegados en uno o varios Workbenches.", "Discover AI Inference Model Endpoints and models deployed in one or more Workbenches.", "Scopri i Model Endpoint di AI Inference e i modelli distribuiti in uno o più Workbench."],
    ["Credenciales locales · nunca se muestran", "Local credentials · never displayed", "Credenziali locali · mai visualizzate"],
    ["Nombre", "Name", "Nome"], ["Servicio", "Service", "Servizio"], ["Instalación", "Deployment", "Installazione"],
    ["Comprobar cada", "Check every", "Controlla ogni"], ["Minutos · mientras el panel esté abierto", "Minutes · while the dashboard is open", "Minuti · mentre il pannello è aperto"],
    ["URL base", "Base URL", "URL base"], ["Ayuda para encontrar la URL base", "Help finding the base URL", "Aiuto per trovare l'URL base"],
    ["Busca la URL de un Model Endpoint y pega sólo el dominio, sin la ruta que empieza por", "Find a Model Endpoint URL and paste only the domain, without the path that starts with", "Trova l'URL di un Model Endpoint e incolla solo il dominio, senza il percorso che inizia con"],
    [". Por ejemplo, de", ". For example, from", ". Ad esempio, da"],
    ["usa", "use", "usa"],
    [". Si pegas la URL completa, la aplicación extraerá el dominio.", ". If you paste the full URL, the application will extract the domain.", ". Se incolli l'URL completo, l'applicazione estrarrà il dominio."],
    ["Sólo protocolo y dominio. No uses la URL de console.cdp.cloudera.com.", "Protocol and domain only. Do not use the console.cdp.cloudera.com URL.", "Solo protocollo e dominio. Non usare l'URL di console.cdp.cloudera.com."],
    ["CDP token o API v2 key", "CDP token or API v2 key", "Token CDP o API v2 key"],
    ["Déjalo vacío al editar para conservar el actual", "Leave blank while editing to keep the current value", "Lascia vuoto durante la modifica per mantenere il valore attuale"],
    ["Renovación automática del CDP token", "Automatic CDP token renewal", "Rinnovo automatico del token CDP"],
    ["URL de renovación", "Renewal URL", "URL di rinnovo"],
    ["On-premise: URL completa de Knox terminada en /token.", "On-premise: full Knox URL ending in /token.", "On-premise: URL Knox completo che termina in /token."],
    ["Vacío conserva el actual", "Blank keeps the current value", "Vuoto mantiene il valore attuale"],
    ["Vacío conserva la actual", "Blank keeps the current value", "Vuoto mantiene il valore attuale"],
    ["Cancelar", "Cancel", "Annulla"], ["Guardar conexión", "Save connection", "Salva connessione"],
    ["No hay conexiones Cloudera configuradas.", "No Cloudera connections configured.", "Nessuna connessione Cloudera configurata."],
    ["Credenciales:", "Credentials:", "Credenziali:"],
    ["cada modelo puede usar su propio JWT/API key o heredar el CDP token de la conexión. Los JWT UMS son temporales; Cloudera no publica una API de renovación por modelo. Para uso estable, genera una Knox API key desde", "each model can use its own JWT/API key or inherit the connection CDP token. UMS JWTs are temporary; Cloudera does not publish a per-model renewal API. For stable use, generate a Knox API key from", "ogni modello può usare il proprio JWT/API key o ereditare il token CDP della connessione. I JWT UMS sono temporanei; Cloudera non pubblica un'API di rinnovo per modello. Per un uso stabile, genera una Knox API key da"],
    ["en Model Endpoint Details.", "in Model Endpoint Details.", "in Model Endpoint Details."],
    ["cada modelo puede usar su propio JWT/API key o heredar el CDP token de la conexión. Los JWT UMS son temporales; Cloudera no publica una API de renovación por modelo. Para uso estable, genera una Knox API key desde Generate Key / Token en Model Endpoint Details.", "each model can use its own JWT/API key or inherit the connection CDP token. UMS JWTs are temporary; Cloudera does not publish a per-model renewal API. For stable use, generate a Knox API key from Generate Key / Token in Model Endpoint Details.", "ogni modello può usare il proprio JWT/API key o ereditare il token CDP della connessione. I JWT UMS sono temporanei; Cloudera non pubblica un'API di rinnovo per modello. Per un uso stabile, genera una Knox API key da Generate Key / Token in Model Endpoint Details."],
    ["Editor avanzado de config.yaml", "Advanced config.yaml editor", "Editor avanzato di config.yaml"],
    ["Fuente de verdad · ancho completo", "Source of truth · full width", "Fonte di verità · larghezza completa"],
    ["Contenido de config.yaml", "config.yaml content", "Contenuto di config.yaml"], ["Validar", "Validate", "Convalida"],
    ["Validar y guardar", "Validate and save", "Convalida e salva"],
    ["Los cambios requieren reiniciar LiteLLM. Al guardar podrás aplicarlos ahora o dejarlos pendientes.", "Model changes require a LiteLLM restart. When saving, you can apply them now or leave them pending.", "Le modifiche ai modelli richiedono il riavvio di LiteLLM. Al salvataggio puoi applicarle subito o lasciarle in sospeso."],
    ["COPIA DE SEGURIDAD", "BACKUP", "BACKUP"], ["Exportar o restaurar config.yaml", "Export or restore config.yaml", "Esporta o ripristina config.yaml"],
    ["La descarga conserva las referencias a credenciales, pero nunca expande sus valores. Trata el fichero como configuración sensible.", "The download keeps credential references but never expands their values. Treat the file as sensitive configuration.", "Il download mantiene i riferimenti alle credenziali ma non ne espande mai i valori. Tratta il file come configurazione sensibile."],
    ["Formato YAML · máximo 1 MB", "YAML format · 1 MB maximum", "Formato YAML · massimo 1 MB"],
    ["Descargar backup YAML", "Download YAML backup", "Scarica backup YAML"], ["Seleccionar backup", "Select backup", "Seleziona backup"],
    ["Validar e importar", "Validate and import", "Convalida e importa"],
    ["Importar sustituye config.yaml después de validarlo. Si LiteLLM está activo, podrás aplicarlo ahora o dejarlo pendiente.", "Import replaces config.yaml after validation. If LiteLLM is running, you can apply it now or leave it pending.", "L'importazione sostituisce config.yaml dopo la convalida. Se LiteLLM è attivo, puoi applicarlo subito o lasciarlo in sospeso."],
    ["Actividad, rendimiento y trazabilidad por modelo", "Activity, performance and traceability by model", "Attività, prestazioni e tracciabilità per modello"],
    ["Jornada", "Day", "Giorno"], ["Jornada del log", "Log day", "Giorno del log"], ["Descargar Excel", "Download Excel", "Scarica Excel"],
    ["Limpiar", "Clear", "Pulisci"], ["Actualizar", "Refresh", "Aggiorna"],
    ["Peticiones por hora", "Requests by hour", "Richieste per ora"],
    ["Peticiones por hora durante la jornada", "Requests by hour during the day", "Richieste per ora durante il giorno"],
    ["Selecciona un modelo.", "Select a model.", "Seleziona un modello."],
    ["¿Aplicar los cambios de modelos ahora?", "Apply model changes now?", "Applicare ora le modifiche ai modelli?"],
    ["Añadir, editar o eliminar modelos requiere reiniciar LiteLLM y puede interrumpir llamadas durante unos segundos. Los cambios de credenciales Cloudera se aplican sin reinicio.", "Adding, editing or removing models requires restarting LiteLLM and may interrupt calls for a few seconds. Cloudera credential changes apply without a restart.", "Aggiungere, modificare o eliminare modelli richiede il riavvio di LiteLLM e può interrompere le chiamate per alcuni secondi. Le modifiche alle credenziali Cloudera si applicano senza riavvio."],
    ["Guardar como pendiente", "Save as pending", "Salva come in sospeso"], ["Guardar, reiniciar y aplicar", "Save, restart and apply", "Salva, riavvia e applica"],
    ["¿Limpiar este log de LiteLLM?", "Clear this LiteLLM log?", "Pulire questo log di LiteLLM?"],
    ["Se eliminarán las líneas de la jornada seleccionada. Los logs de peticiones de los modelos no se verán afectados.", "Entries for the selected day will be removed. Model request logs will not be affected.", "Le righe del giorno selezionato verranno eliminate. I log delle richieste dei modelli non saranno interessati."],
    ["Sí, limpiar", "Yes, clear", "Sì, pulisci"], ["¿Eliminar este modelo?", "Delete this model?", "Eliminare questo modello?"],
    ["También se limpiarán sus fallbacks, estado y uso como guardrail. Los logs históricos se conservarán.", "Its fallbacks, state and guardrail use will also be cleared. Historical logs will be preserved.", "Verranno rimossi anche fallback, stato e uso come guardrail. I log storici saranno conservati."],
    ["Eliminar modelo", "Delete model", "Elimina modello"], ["¿Eliminar esta conexión?", "Delete this connection?", "Eliminare questa connessione?"],
    ["Se eliminarán también las credenciales de modelos asociadas. Los modelos ya incorporados a config.yaml no se borrarán.", "Associated model credentials will also be removed. Models already added to config.yaml will not be deleted.", "Verranno eliminate anche le credenziali dei modelli associati. I modelli già aggiunti a config.yaml non verranno eliminati."],
    ["Eliminar conexión", "Delete connection", "Elimina connessione"], ["CUENTA ADMIN", "ADMIN ACCOUNT", "ACCOUNT ADMIN"],
    ["Cambiar contraseña", "Change password", "Cambia password"],
    ["Usa al menos 12 caracteres, mayúscula, minúscula, número y símbolo.", "Use at least 12 characters, with uppercase, lowercase, a number and a symbol.", "Usa almeno 12 caratteri, con maiuscola, minuscola, numero e simbolo."],
    ["Contraseña actual", "Current password", "Password attuale"], ["Nueva contraseña", "New password", "Nuova password"],
    ["Repite la nueva contraseña", "Repeat new password", "Ripeti la nuova password"], ["Guardar contraseña", "Save password", "Salva password"],
    ["Activo", "Running", "Attivo"], ["Detenido", "Stopped", "Arrestato"], ["Sin servicio", "Unavailable", "Non disponibile"],
    ["Conflicto", "Conflict", "Conflitto"], ["Modelo activo", "Model active", "Modello attivo"],
    ["Modelo iniciándose", "Model starting", "Modello in avvio"], ["Endpoint preparado", "Endpoint ready", "Endpoint pronto"],
    ["Token caducado", "Token expired", "Token scaduto"], ["Token propio disponible", "Dedicated token available", "Token dedicato disponibile"],
    ["Usará el CDP token general", "Will use the shared CDP token", "Userà il token CDP condiviso"], ["Sin credencial", "No credential", "Nessuna credenziale"],
    ["Sin modelos detectados", "No models detected", "Nessun modello rilevato"], ["Respuesta sin probar", "Response not tested", "Risposta non testata"],
    ["Responde correctamente", "Responding correctly", "Risponde correttamente"], ["Prueba fallida", "Test failed", "Test fallito"],
    ["Modelo", "Model", "Modello"], ["Estado", "Status", "Stato"], ["Proveedor", "Provider", "Provider"],
    ["Editar", "Edit", "Modifica"], ["Eliminar", "Delete", "Elimina"], ["Guardar cambios", "Save changes", "Salva modifiche"],
    ["API por defecto", "Default API", "API predefinita"], ["No requerida", "Not required", "Non richiesta"],
    ["Sin guardrail · llamada directa", "No guardrail · direct request", "Nessun guardrail · richiesta diretta"],
    ["No hay modelos configurados.", "No models configured.", "Nessun modello configurato."],
    ["Latencia (sonda)", "Latency (probe)", "Latenza (sonda)"], ["Pendiente", "Pending", "In sospeso"],
    ["Gateway detenido", "Gateway stopped", "Gateway arrestato"], ["Saldo / tokens", "Balance / tokens", "Saldo / token"],
    ["No disponible vía API", "Not available via API", "Non disponibile tramite API"], ["Ollama no disponible", "Ollama unavailable", "Ollama non disponibile"],
    ["Memoria modelo", "Model memory", "Memoria modello"], ["VRAM modelo", "Model VRAM", "VRAM modello"],
    ["Estado modelo", "Model status", "Stato modello"], ["No cargado", "Not loaded", "Non caricato"],
    ["Memoria libre", "Free memory", "Memoria libera"], ["CPU compartida", "Shared CPU", "CPU condivisa"],
    ["Informativo", "Informational", "Informativo"], ["Respuesta rápida", "Fast response", "Risposta rapida"],
    ["Respuesta moderada", "Moderate response", "Risposta moderata"], ["Respuesta lenta", "Slow response", "Risposta lenta"],
    ["CHAT", "CHAT", "CHAT"], ["El texto se enviará al endpoint de embeddings.", "Text will be sent to the embeddings endpoint.", "Il testo verrà inviato all'endpoint embeddings."],
    ["Se enviará como un mensaje de usuario.", "It will be sent as a user message.", "Verrà inviato come messaggio utente."],
    ["Texto que quieres convertir en un vector…", "Text to convert into a vector…", "Testo da convertire in un vettore…"],
    ["CORRECTO", "SUCCESS", "CORRETTO"], ["Fecha:", "Date:", "Data:"], ["Origen:", "Source:", "Origine:"],
    ["Respuesta:", "Response:", "Risposta:"], ["Inicio respuesta:", "First response:", "Inizio risposta:"], ["Fin respuesta:", "Response end:", "Fine risposta:"],
    ["Pregunta / entrada", "Prompt / input", "Domanda / input"], ["Respuesta / salida", "Response / output", "Risposta / output"],
    ["Peticiones", "Requests", "Richieste"], ["Volumen de la jornada", "Daily volume", "Volume giornaliero"],
    ["Tasa de éxito", "Success rate", "Tasso di successo"], ["Latencia media", "Average latency", "Latenza media"],
    ["Inicio de respuesta", "Time to first response", "Tempo alla prima risposta"], ["TTFT medio", "Average TTFT", "TTFT medio"],
    ["Alertas guardrail", "Guardrail alerts", "Avvisi guardrail"], ["Contenido marcado como riesgo", "Content flagged as risky", "Contenuto segnalato come rischioso"],
    ["Tokens", "Tokens", "Token"], ["Sin actividad", "No activity", "Nessuna attività"], ["Hora", "Time", "Ora"],
    ["Destino", "Destination", "Destinazione"], ["Total", "Total", "Totale"], ["Detalle", "Details", "Dettagli"],
    ["No disponible", "Unavailable", "Non disponibile"], ["Enviando…", "Sending…", "Invio…"],
    ["Comprobando respuesta", "Checking response", "Verifica della risposta"], ["Validando endpoint y credencial…", "Validating endpoint and credential…", "Convalida di endpoint e credenziale…"],
    ["Probar acceso", "Test access", "Prova accesso"], ["Preparar borrador", "Prepare draft", "Prepara bozza"],
    ["Buscar modelos", "Discover models", "Cerca modelli"], ["Probar todos", "Test all", "Prova tutti"],
    ["Renovar token", "Renew token", "Rinnova token"], ["Generar token", "Generate token", "Genera token"],
    ["Guardar token propio", "Save dedicated token", "Salva token dedicato"], ["Sustituir token", "Replace token", "Sostituisci token"],
    ["Réplicas:", "Replicas:", "Repliche:"], ["Caduca:", "Expires:", "Scade:"],
    ["Cloudera informa:", "Cloudera reports:", "Cloudera segnala:"],
    ["Endpoint no incluido por la API", "Endpoint not returned by the API", "Endpoint non restituito dall'API"],
    ["JWT / API key del modelo", "Model JWT / API key", "JWT / API key del modello"],
    ["Open Inference predictivo", "Predictive Open Inference", "Open Inference predittivo"],
    ["Necesita conocer el esquema de tensores del modelo para probarlo.", "The model tensor schema is required to test it.", "Per provarlo è necessario conoscere lo schema dei tensori del modello."],
    ["La API no devolvió modelos visibles para esta conexión.", "The API returned no visible models for this connection.", "L'API non ha restituito modelli visibili per questa connessione."],
    ["Validando…", "Validating…", "Convalida…"], ["Validando y guardando…", "Validating and saving…", "Convalida e salvataggio…"],
    ["Las nuevas contraseñas no coinciden.", "The new passwords do not match.", "Le nuove password non coincidono."],
    ["Edición cancelada.", "Editing cancelled.", "Modifica annullata."],
    ["Por seguridad debes sustituir la contraseña inicial antes de usar el panel. Usa al menos 12 caracteres, mayúscula, minúscula, número y símbolo.", "For security, replace the initial password before using the dashboard. Use at least 12 characters, with uppercase, lowercase, a number and a symbol.", "Per sicurezza, sostituisci la password iniziale prima di usare il pannello. Usa almeno 12 caratteri, con maiuscola, minuscola, numero e simbolo."],
    ["Arquitectura de IA Gateway: entrada única, panel web, LiteLLM, proveedores y SQLite", "IA Gateway architecture: single entry point, web dashboard, LiteLLM, providers and SQLite", "Architettura IA Gateway: ingresso unico, pannello web, LiteLLM, provider e SQLite"],
    ["Renovando token…", "Renewing token…", "Rinnovo del token…"], ["Generando token inicial…", "Generating initial token…", "Generazione del token iniziale…"],
    ["JWT caducado", "JWT expired", "JWT scaduto"], ["Credencial guardada", "Credential saved", "Credenziale salvata"],
    ["Token pendiente de generar", "Token waiting to be generated", "Token in attesa di generazione"], ["Falta credencial", "Credential missing", "Credenziale mancante"],
    ["Generación automática configurada", "Automatic generation configured", "Generazione automatica configurata"], ["Generación automática sin completar", "Automatic generation incomplete", "Generazione automatica incompleta"],
    ["Borrar", "Delete", "Elimina"], ["Sin comprobaciones de modelos", "No model checks", "Nessun controllo dei modelli"],
    ["Token caducado, generando de nuevo…", "Token expired; generating it again…", "Token scaduto; nuova generazione…"], ["Lista para consultar", "Ready for queries", "Pronta per le richieste"],
    ["Completa los datos de generación o introduce un token", "Complete the generation details or enter a token", "Completa i dati di generazione o inserisci un token"],
    ["Pulsa «Buscar modelos» para ver los modelos de esta conexión.", "Select “Discover models” to view the models for this connection.", "Seleziona “Cerca modelli” per vedere i modelli di questa connessione."],
    ["No hay conexiones Cloudera configuradas. Añade una arriba para comenzar.", "No Cloudera connections are configured. Add one above to get started.", "Non sono configurate connessioni Cloudera. Aggiungine una sopra per iniziare."],
    ["Conexión y credenciales asociadas eliminadas.", "Connection and associated credentials deleted.", "Connessione e credenziali associate eliminate."],
    ["Guardando conexión y obteniendo credencial si es necesaria…", "Saving connection and obtaining a credential if needed…", "Salvataggio della connessione e acquisizione della credenziale, se necessaria…"],
    ["Buscando…", "Discovering…", "Ricerca…"], ["Conectando con Cloudera y buscando modelos…", "Connecting to Cloudera and discovering models…", "Connessione a Cloudera e ricerca dei modelli…"],
    ["Consultando el catálogo. En un Workbench puede tardar mientras se recorren proyectos y deployments…", "Querying the catalogue. In a Workbench this can take a while as projects and deployments are inspected…", "Consultazione del catalogo. In un Workbench può richiedere tempo durante l'analisi di progetti e deployment…"],
    ["Cloudera no publicó una URL para este modelo", "Cloudera did not publish a URL for this model", "Cloudera non ha pubblicato un URL per questo modello"],
    ["Prueba manual", "Manual test", "Test manuale"], ["Prueba automática", "Automatic test", "Test automatico"],
    ["Solicitando credencial a Cloudera…", "Requesting a credential from Cloudera…", "Richiesta di una credenziale a Cloudera…"],
    ["Token inicial generado · LiteLLM continúa activo", "Initial token generated · LiteLLM remains running", "Token iniziale generato · LiteLLM resta attivo"],
    ["Token renovado · LiteLLM continúa activo", "Token renewed · LiteLLM remains running", "Token rinnovato · LiteLLM resta attivo"],
    ["la credencial disponible", "the available credential", "la credenziale disponibile"],
    ["URL proporcionada por", "URL provided by", "URL fornito da"], ["la API de Cloudera", "the Cloudera API", "l'API Cloudera"],
    ["Escribe un token nuevo para sustituir", "Enter a new replacement token", "Inserisci un nuovo token sostitutivo"], ["Opcional: usa el CDP token si queda vacío", "Optional: use the CDP token when left blank", "Opzionale: usa il token CDP se lasciato vuoto"],
    ["Probando…", "Testing…", "Test…"], ["Borrador Cloudera preparado. Revisa los campos antes de añadirlo.", "Cloudera draft prepared. Review the fields before adding it.", "Bozza Cloudera pronta. Controlla i campi prima di aggiungerla."],
    ["Borrador preparado: este protocolo necesita un adaptador LiteLLM personalizado.", "Draft prepared: this protocol requires a custom LiteLLM adapter.", "Bozza pronta: questo protocollo richiede un adattatore LiteLLM personalizzato."],
    ["Uso crítico", "Critical usage", "Utilizzo critico"], ["Uso elevado", "High usage", "Utilizzo elevato"], ["Uso bajo", "Low usage", "Utilizzo basso"],
    ["Memoria crítica", "Critical memory", "Memoria critica"], ["Memoria limitada", "Limited memory", "Memoria limitata"], ["Memoria suficiente", "Sufficient memory", "Memoria sufficiente"],
    ["Configuración guardada y LiteLLM reiniciado.", "Configuration saved and LiteLLM restarted.", "Configurazione salvata e LiteLLM riavviato."],
    ["Configuración guardada. Se aplicará al arrancar LiteLLM.", "Configuration saved. It will apply when LiteLLM starts.", "Configurazione salvata. Verrà applicata all'avvio di LiteLLM."],
    ["Modelo modificado.", "Model updated.", "Modello aggiornato."], ["Modelo añadido.", "Model added.", "Modello aggiunto."],
    ["Selecciona primero un fichero .yaml o .yml.", "Select a .yaml or .yml file first.", "Seleziona prima un file .yaml o .yml."],
    ["El backup debe tener extensión .yaml o .yml.", "The backup must have a .yaml or .yml extension.", "Il backup deve avere estensione .yaml o .yml."],
    ["El fichero seleccionado está vacío.", "The selected file is empty.", "Il file selezionato è vuoto."], ["El backup supera el máximo permitido de 1 MB.", "The backup exceeds the 1 MB limit.", "Il backup supera il limite di 1 MB."],
    ["El fichero no es un YAML de texto válido.", "The file is not valid text YAML.", "Il file non è un YAML testuale valido."],
    ["Importación cancelada; config.yaml no ha cambiado.", "Import cancelled; config.yaml was not changed.", "Importazione annullata; config.yaml non è stato modificato."],
    ["Selecciona primero el modelo que actuará como guardrail.", "Select the model that will act as the guardrail first.", "Seleziona prima il modello che fungerà da guardrail."],
    ["Guardrail desactivado: las llamadas irán directamente al modelo elegido.", "Guardrail disabled: requests will go directly to the selected model.", "Guardrail disattivato: le richieste andranno direttamente al modello scelto."],
    ["Guardrail actualizado en política restringida.", "Guardrail updated with restricted policy.", "Guardrail aggiornato con criterio restrittivo."], ["Guardrail actualizado en política permisiva.", "Guardrail updated with permissive policy.", "Guardrail aggiornato con criterio permissivo."],
    ["⚠ Riesgo detectado; la petición continuó", "⚠ Risk detected; request continued", "⚠ Rischio rilevato; la richiesta è proseguita"],
    ["✓ Contenido clasificado como seguro", "✓ Content classified as safe", "✓ Contenuto classificato come sicuro"], ["⚠ No se pudo evaluar; la petición continuó", "⚠ Evaluation unavailable; request continued", "⚠ Valutazione non disponibile; la richiesta è proseguita"],
    ["Riesgo · continuó", "Risk · continued", "Rischio · continuata"], ["Seguro", "Safe", "Sicuro"],
    ["Embedding generado correctamente", "Embedding generated successfully", "Embedding generato correttamente"], ["Dimensiones:", "Dimensions:", "Dimensioni:"], ["Primeros valores:", "First values:", "Primi valori:"]
  ];

  const dictionaries = {es: new Map(), en: new Map(), it: new Map()};
  rows.forEach(([es, en, it]) => { dictionaries.es.set(es, es); dictionaries.en.set(es, en); dictionaries.it.set(es, it); });

  const patterns = {
    en: [
      [/^(\d+) modelos?$/, "$1 models"], [/^(\d+) errores$/, "$1 errors"], [/^(\d+) entrada · (\d+) salida$/, "$1 input · $2 output"],
      [/^Máximo (\d+) peticiones en una hora$/, "Maximum $1 requests in one hour"], [/^Editando (.+)$/, "Editing $1"],
      [/^Prueba automática cada (\d+) min$/, "Automatic test every $1 min"], [/^CDP token actualizado automáticamente\.(.*)$/, "CDP token updated automatically.$1"],
      [/^Editando conexión «(.+)»\. La credencial actual se conservará si dejas el campo vacío\.$/, "Editing connection “$1”. The current credential will be kept if you leave the field blank."],
      [/^Conexión (creada|actualizada), pero no se pudo generar el token: (.+)$/, "Connection $1, but the token could not be generated: $2"],
      [/^Conexión (creada|actualizada) correctamente\.(.*) URL efectiva: (.+)\. Pulsa «Buscar modelos»\.$/, "Connection $1 successfully.$2 Effective URL: $3. Select “Discover models”."],
      [/^No se pudo guardar la conexión: (.+)$/, "Could not save the connection: $1"], [/^Búsqueda completada: (.+)\.$/, "Discovery complete: $1."],
      [/^No se pudieron cargar modelos: (.+)$/, "Could not load models: $1"], [/^No se pudo completar la comprobación: (.+)$/, "Could not complete the check: $1"],
      [/^Prueba manual completada · (.+)$/, "Manual test complete · $1"], [/^Prueba automática completada · (.+)$/, "Automatic test complete · $1"], [/^No se pudo obtener el token: (.+)$/, "Could not obtain the token: $1"],
      [/^Se usará (.+)$/, "$1 will be used"], [/^Credencial de (.+) guardada\. Pulsa «Probar acceso» para validarla\.$/, "$1 credential saved. Select “Test access” to validate it."],
      [/^YAML válido · (\d+) modelos(.*)$/, "Valid YAML · $1 models$2"], [/^Validando (.+)…$/, "Validating $1…"],
      [/^Backup importado: (\d+) modelos\. (.+)$/, "Backup imported: $1 models. $2"], [/^No se pudo importar: (.+)$/, "Could not import: $1"],
      [/^Embedding generado correctamente\n\nDimensiones: (\d+)\nPrimeros valores: (.+)$/, "Embedding generated successfully\n\nDimensions: $1\nFirst values: $2"],
      [/^Vas a eliminar el alias «(.+)» de config\.yaml\.$/, "You are about to remove alias “$1” from config.yaml."],
      [/^Seleccionado: (.+)$/, "Selected: $1"], [/^Estado (.+)$/, "Status: $1"], [/^Modelo (.+)$/, "Model $1"],
      [/^puerto (\d+)$/, "port $1"], [/^(\d+) encontrados · (\d+) activos · (\d+) OpenAI compatibles$/, "$1 found · $2 active · $3 OpenAI compatible"],
      [/^Comprobando (\d+)\/(\d+) modelos…$/, "Checking $1/$2 models…"], [/^(\d+) con error$/, "$1 failed"], [/^(\d+) sin probar$/, "$1 untested"],
      [/^Caduca: (.+)$/, "Expires: $1"], [/^Réplicas: (.+)$/, "Replicas: $1"], [/^(\d+) modelos configurados$/, "$1 models configured"]
    ],
    it: [
      [/^(\d+) modelos?$/, "$1 modelli"], [/^(\d+) errores$/, "$1 errori"], [/^(\d+) entrada · (\d+) salida$/, "$1 input · $2 output"],
      [/^Máximo (\d+) peticiones en una hora$/, "Massimo $1 richieste in un'ora"], [/^Editando (.+)$/, "Modifica di $1"],
      [/^Prueba automática cada (\d+) min$/, "Test automatico ogni $1 min"], [/^CDP token actualizado automáticamente\.(.*)$/, "Token CDP aggiornato automaticamente.$1"],
      [/^Editando conexión «(.+)»\. La credencial actual se conservará si dejas el campo vacío\.$/, "Modifica della connessione “$1”. La credenziale attuale verrà mantenuta se lasci il campo vuoto."],
      [/^Conexión (creada|actualizada), pero no se pudo generar el token: (.+)$/, "Connessione salvata, ma non è stato possibile generare il token: $2"],
      [/^Conexión (creada|actualizada) correctamente\.(.*) URL efectiva: (.+)\. Pulsa «Buscar modelos»\.$/, "Connessione salvata correttamente.$2 URL effettivo: $3. Seleziona “Cerca modelli”."],
      [/^No se pudo guardar la conexión: (.+)$/, "Impossibile salvare la connessione: $1"], [/^Búsqueda completada: (.+)\.$/, "Ricerca completata: $1."],
      [/^No se pudieron cargar modelos: (.+)$/, "Impossibile caricare i modelli: $1"], [/^No se pudo completar la comprobación: (.+)$/, "Impossibile completare il controllo: $1"],
      [/^Prueba manual completada · (.+)$/, "Test manuale completato · $1"], [/^Prueba automática completada · (.+)$/, "Test automatico completato · $1"], [/^No se pudo obtener el token: (.+)$/, "Impossibile ottenere il token: $1"],
      [/^Se usará (.+)$/, "Verrà usata $1"], [/^Credencial de (.+) guardada\. Pulsa «Probar acceso» para validarla\.$/, "Credenziale di $1 salvata. Seleziona “Prova accesso” per convalidarla."],
      [/^YAML válido · (\d+) modelos(.*)$/, "YAML valido · $1 modelli$2"], [/^Validando (.+)…$/, "Convalida di $1…"],
      [/^Backup importado: (\d+) modelos\. (.+)$/, "Backup importato: $1 modelli. $2"], [/^No se pudo importar: (.+)$/, "Impossibile importare: $1"],
      [/^Embedding generado correctamente\n\nDimensiones: (\d+)\nPrimeros valores: (.+)$/, "Embedding generato correttamente\n\nDimensioni: $1\nPrimi valori: $2"],
      [/^Vas a eliminar el alias «(.+)» de config\.yaml\.$/, "Stai per eliminare l'alias “$1” da config.yaml."],
      [/^Seleccionado: (.+)$/, "Selezionato: $1"], [/^Estado (.+)$/, "Stato: $1"], [/^Modelo (.+)$/, "Modello $1"],
      [/^puerto (\d+)$/, "porta $1"], [/^(\d+) encontrados · (\d+) activos · (\d+) OpenAI compatibles$/, "$1 trovati · $2 attivi · $3 compatibili OpenAI"],
      [/^Comprobando (\d+)\/(\d+) modelos…$/, "Verifica di $1/$2 modelli…"], [/^(\d+) con error$/, "$1 con errore"], [/^(\d+) sin probar$/, "$1 non testati"],
      [/^Caduca: (.+)$/, "Scade: $1"], [/^Réplicas: (.+)$/, "Repliche: $1"], [/^(\d+) modelos configurados$/, "$1 modelli configurati"]
    ]
  };

  function translated(value) {
    if (language === "es" || !value) return value;
    const exact = dictionaries[language].get(value);
    if (exact !== undefined) return exact;
    for (const [pattern, replacement] of patterns[language]) {
      if (pattern.test(value)) return value.replace(pattern, replacement);
    }
    return value;
  }

  function preserveWhitespace(value) {
    const leading = value.match(/^\s*/)?.[0] || "";
    const trailing = value.match(/\s*$/)?.[0] || "";
    const core = value.slice(leading.length, value.length - trailing.length || undefined);
    return `${leading}${translated(core)}${trailing}`;
  }

  const textState = new WeakMap();
  const attributeState = new WeakMap();
  const attributes = ["placeholder", "aria-label", "title", "alt"];

  function translateTextNode(node) {
    let state = textState.get(node);
    if (!state || node.data !== state.rendered) state = {source: node.data, rendered: node.data};
    const rendered = preserveWhitespace(state.source);
    state.rendered = rendered;
    textState.set(node, state);
    if (node.data !== rendered) node.data = rendered;
  }

  function translateElement(element) {
    let states = attributeState.get(element) || {};
    attributes.forEach((name) => {
      if (!element.hasAttribute(name)) return;
      const current = element.getAttribute(name);
      const previous = states[name];
      const source = !previous || current !== previous.rendered ? current : previous.source;
      const rendered = translated(source);
      states[name] = {source, rendered};
      if (current !== rendered) element.setAttribute(name, rendered);
    });
    attributeState.set(element, states);
  }

  function translateTree(root = document.documentElement) {
    if (root.nodeType === Node.TEXT_NODE) translateTextNode(root);
    if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeType === Node.TEXT_NODE) translateTextNode(node);
      else translateElement(node);
    }
    document.documentElement.lang = language;
    document.querySelectorAll(".language-select").forEach((select) => { select.value = language; });
  }

  function setLanguage(next) {
    if (!supported.has(next)) return;
    language = next;
    localStorage.setItem("ia-gateway-language", language);
    translateTree();
    document.dispatchEvent(new CustomEvent("ia-gateway:language", {detail: {language}}));
  }

  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.type === "characterData") translateTextNode(mutation.target);
      mutation.addedNodes.forEach((node) => translateTree(node));
      if (mutation.type === "attributes") translateElement(mutation.target);
    });
  });

  window.IAGatewayI18n = {
    get language() { return language; },
    localeTag: () => localeTags[language],
    setLanguage,
    translate: translated,
    refresh: translateTree,
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".language-select").forEach((select) => {
      select.value = language;
      select.addEventListener("change", () => setLanguage(select.value));
    });
    translateTree();
    observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: attributes});
  });
})();
