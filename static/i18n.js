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
    ["Modelos", "Models", "Modelli"], ["Configuración", "Configuration", "Configurazione"], ["Batería de pruebas", "Load testing", "Test di carico"], ["Logs", "Logs", "Log"],
    ["CAPACIDAD · EXTREMO A EXTREMO", "CAPACITY · END TO END", "CAPACITÀ · END TO END"],
    ["Encuentra el punto de saturación del gateway y de cada modelo con carga escalonada y respuestas autocorregibles.", "Find the saturation point of the gateway and each model with stepped load and automatically checked answers.", "Trova il punto di saturazione del gateway e di ogni modello con carico progressivo e risposte verificate automaticamente."],
    ["Sin ejecutar", "Not run", "Non eseguito"], ["En ejecución", "Running", "In esecuzione"], ["Deteniendo", "Stopping", "Arresto in corso"], ["Completada", "Completed", "Completato"], ["Cancelada", "Cancelled", "Annullato"],
    ["Diseño de la prueba", "Test design", "Configurazione del test"], ["Control de carga", "Load control", "Controllo del carico"],
    ["La concurrencia sube nivel a nivel, nunca empieza directamente en el máximo.", "Concurrency increases one level at a time; it never starts directly at the maximum.", "La concorrenza aumenta un livello alla volta, senza iniziare direttamente dal massimo."],
    ["1 · Modelos y techo de concurrencia", "1 · Models and concurrency ceiling", "1 · Modelli e limite di concorrenza"],
    ["2 · Cuándo termina", "2 · Stopping rule", "2 · Criterio di arresto"], ["3 · Qué quieres medir", "3 · What to measure", "3 · Cosa misurare"],
    ["Número de peticiones", "Number of requests", "Numero di richieste"], ["Tiempo total", "Total time", "Tempo totale"],
    ["Misma cantidad por modelo", "Same amount per model", "Stessa quantità per modello"], ["Misma duración por modelo", "Same duration per model", "Stessa durata per modello"],
    ["Peticiones por modelo", "Requests per model", "Richieste per modello"], ["Timeout por petición", "Per-request timeout", "Timeout per richiesta"],
    ["Se reparte entre todos los niveles.", "Split across all levels.", "Suddiviso tra tutti i livelli."],
    ["Estrategia", "Strategy", "Strategia"], ["Gateway completo · modelos en paralelo", "Full gateway · models in parallel", "Gateway completo · modelli in parallelo"],
    ["Capacidad por modelo · uno tras otro", "Per-model capacity · one at a time", "Capacità per modello · uno alla volta"],
    ["Mide la presión agregada sobre IA Gateway.", "Measures aggregate pressure on IA Gateway.", "Misura la pressione aggregata su IA Gateway."],
    ["Aísla cada backend para comparar su capacidad sin interferencias.", "Isolates each backend for an interference-free capacity comparison.", "Isola ogni backend per confrontarne la capacità senza interferenze."],
    ["Calentar cada modelo con una petición no contabilizada", "Warm up each model with one uncounted request", "Riscalda ogni modello con una richiesta non conteggiata"],
    ["Carga prevista", "Planned load", "Carico previsto"], ["Iniciar batería", "Start test", "Avvia test"], ["Detener con seguridad", "Stop safely", "Arresta in sicurezza"],
    ["Preparado para medir", "Ready to measure", "Pronto per la misurazione"],
    ["Se usarán sumas, ordenaciones, copia exacta y transformaciones simples para separar disponibilidad de corrección.", "Arithmetic, sorting, exact copy, and simple transformations separate availability from correctness.", "Somme, ordinamenti, copia esatta e semplici trasformazioni separano la disponibilità dalla correttezza."],
    ["TTFT y latencia p50 / p95 / p99", "TTFT and p50 / p95 / p99 latency", "TTFT e latenza p50 / p95 / p99"],
    ["Throughput, errores y respuestas correctas", "Throughput, errors, and correct answers", "Throughput, errori e risposte corrette"],
    ["Concurrencia sostenible antes de degradarse", "Sustainable concurrency before degradation", "Concorrenza sostenibile prima del degrado"],
    ["Descargar JSON", "Download JSON", "Scarica JSON"], ["Prueba en curso", "Test in progress", "Test in corso"], ["Midiendo bajo carga", "Measuring under load", "Misurazione sotto carico"],
    ["Carga escalonada en curso", "Stepped load in progress", "Carico progressivo in corso"], ["Medición completada", "Measurement completed", "Misurazione completata"], ["Medición interrumpida", "Measurement interrupted", "Misurazione interrotta"],
    ["Peticiones", "Requests", "Richieste"], ["Throughput conjunto", "Combined throughput", "Throughput complessivo"], ["Error global", "Overall error rate", "Errore globale"], ["Corrección global", "Overall correctness", "Correttezza globale"],
    ["Completadas", "Completed", "Completate"], ["Rendimiento", "Throughput", "Rendimento"], ["Latencia p95", "p95 latency", "Latenza p95"], ["Errores", "Errors", "Errori"], ["Correctas", "Correct", "Corrette"], ["Nivel sostenible", "Sustainable level", "Livello sostenibile"],
    ["CONCURRENCIA", "CONCURRENCY", "CONCORRENZA"], ["TTFT P95 EN VIVO", "LIVE TTFT P95", "TTFT P95 IN TEMPO REALE"], ["Milisegundos", "Milliseconds", "Millisecondi"],
    ["Cian nivel · violeta en vuelo", "Cyan level · violet in flight", "Ciano livello · viola in esecuzione"], ["Escalón", "Stage", "Livello"], ["Total p95", "Total p95", "Totale p95"], ["Error", "Error", "Errore"],
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
    ["Configura capacidad, generación y operación sin enviar opciones incompatibles al servidor.", "Configure capacity, generation, and operation without sending incompatible options to the server.", "Configura capacità, generazione e funzionamento senza inviare opzioni incompatibili al server."],
    ["Alias", "Alias", "Alias"], ["Modelo LiteLLM", "LiteLLM model", "Modello LiteLLM"],
    ["API base", "API base", "API base"], ["(opcional)", "(optional)", "(opzionale)"],
    ["Variable API key", "API key variable", "Variabile API key"], ["(nunca el secreto)", "(never the secret)", "(mai il segreto)"],
    ["Backend", "Backend", "Backend"], ["Detectar automáticamente", "Detect automatically", "Rileva automaticamente"],
    ["OpenAI estándar", "Standard OpenAI", "OpenAI standard"], ["Compatibilidad", "Compatibility", "Compatibilità"],
    ["Automática", "Automatic", "Automatica"], ["Versiones actuales", "Current versions", "Versioni attuali"],
    ["Perfil de uso", "Use-case profile", "Profilo caso d'uso"], ["Equilibrado", "Balanced", "Bilanciato"],
    ["Determinista / extracción", "Deterministic / extraction", "Deterministico / estrazione"], ["Creativo", "Creative", "Creativo"],
    ["Razonamiento complejo", "Complex reasoning", "Ragionamento complesso"], ["RAG / respuestas fundamentadas", "RAG / grounded answers", "RAG / risposte fondate"],
    ["Alto rendimiento", "High throughput", "Alte prestazioni"], ["Aplicar perfil", "Apply profile", "Applica profilo"],
    ["Capacidad y operación", "Capacity and operation", "Capacità e funzionamento"],
    ["contexto, salida, timeout y concurrencia", "context, output, timeout, and concurrency", "contesto, output, timeout e concorrenza"],
    ["Ventana total de contexto", "Total context window", "Finestra di contesto totale"],
    ["(tokens)", "(tokens)", "(token)"],
    ["No aumenta el límite con el que se desplegó vLLM/NIM.", "Does not increase the limit used to deploy vLLM/NIM.", "Non aumenta il limite con cui è stato distribuito vLLM/NIM."],
    ["Capacidad máxima de salida", "Maximum output capacity", "Capacità massima di output"],
    ["Se resta de la ventana para declarar el máximo de entrada.", "Subtracted from the window to declare the maximum input.", "Viene sottratta dalla finestra per dichiarare l'input massimo."],
    ["Salida predeterminada", "Default output", "Output predefinito"], ["(max_tokens)", "(max_tokens)", "(max_tokens)"],
    ["Timeout", "Timeout", "Timeout"], ["(segundos)", "(seconds)", "(secondi)"],
    ["Reintentos", "Retries", "Tentativi"], ["En Workbench se recomienda 0 para evitar réplicas ocupadas.", "For Workbench, 0 is recommended to avoid busy replicas.", "Per Workbench si consiglia 0 per evitare repliche occupate."],
    ["Máximo en paralelo", "Maximum parallel requests", "Massimo in parallelo"],
    ["Límite local del router LiteLLM para este deployment.", "Local LiteLLM router limit for this deployment.", "Limite locale del router LiteLLM per questo deployment."],
    ["Útil en Ollama; confirma que otros backends lo admitan.", "Useful for Ollama; confirm support on other backends.", "Utile per Ollama; verifica il supporto sugli altri backend."],
    ["Generación y muestreo", "Generation and sampling", "Generazione e campionamento"],
    ["creatividad, diversidad y repetición", "creativity, diversity, and repetition", "creatività, diversità e ripetizione"],
    ["Penalización de repetición", "Repetition penalty", "Penalità di ripetizione"],
    ["Penalización de frecuencia", "Frequency penalty", "Penalità di frequenza"],
    ["Penalización de presencia", "Presence penalty", "Penalità di presenza"],
    ["Secuencias de parada", "Stop sequences", "Sequenze di arresto"], ["(una por línea)", "(one per line)", "(una per riga)"],
    ["thinking y esfuerzo", "thinking and effort", "thinking e sforzo"], ["Modo", "Mode", "Modalità"],
    ["Según modelo/servidor", "According to model/server", "In base al modello/server"],
    ["Activar razonamiento", "Enable reasoning", "Attiva ragionamento"], ["Desactivar razonamiento", "Disable reasoning", "Disattiva ragionamento"],
    ["Nivel de esfuerzo", "Effort level", "Livello di sforzo"],
    ["Conservar el razonamiento en la respuesta", "Preserve reasoning in the response", "Conserva il ragionamento nella risposta"],
    ["Selecciona el backend para ver cómo se aplican estos parámetros.", "Select the backend to see how these parameters are applied.", "Seleziona il backend per vedere come vengono applicati questi parametri."],
    ["Triton OIP recibe tensores. El contexto, batching y decoding se cambian en config.pbtxt o al desplegar el modelo, no como parámetros OpenAI.", "Triton OIP receives tensors. Context, batching, and decoding are changed in config.pbtxt or at deployment time, not as OpenAI parameters.", "Triton OIP riceve tensori. Contesto, batching e decoding si modificano in config.pbtxt o durante il deployment, non come parametri OpenAI."],
    ["Workbench 1.5.5 SP3 envía enable_thinking al wrapper y limita la salida predeterminada a 512 tokens para evitar dejar la réplica ocupada.", "Workbench 1.5.5 SP3 sends enable_thinking to the wrapper and limits default output to 512 tokens to avoid leaving the replica busy.", "Workbench 1.5.5 SP3 invia enable_thinking al wrapper e limita l'output predefinito a 512 token per evitare di lasciare occupata la replica."],
    ["Workbench envuelve los parámetros dentro de request; confirma en el predictor qué opciones admite el modelo.", "Workbench wraps parameters inside request; confirm which options the predictor supports.", "Workbench incapsula i parametri in request; verifica nel predictor quali opzioni supporta il modello."],
    ["NIM usa la API OpenAI compatible. Top K, Min P y repetición viajan en extra_body; el thinking se aplica mediante chat_template_kwargs cuando el modelo lo soporta.", "NIM uses the OpenAI-compatible API. Top K, Min P, and repetition travel in extra_body; thinking is applied through chat_template_kwargs when supported by the model.", "NIM usa l'API compatibile OpenAI. Top K, Min P e ripetizione viaggiano in extra_body; il thinking viene applicato tramite chat_template_kwargs quando supportato dal modello."],
    ["vLLM recibe Top K, Min P y repetición en extra_body. La ventana real sigue limitada por --max-model-len del deployment.", "vLLM receives Top K, Min P, and repetition in extra_body. The real window remains limited by the deployment's --max-model-len.", "vLLM riceve Top K, Min P e ripetizione in extra_body. La finestra reale resta limitata da --max-model-len del deployment."],
    ["LiteLLM traduce las opciones compatibles hacia Ollama. Keep alive controla cuánto permanece cargado el modelo.", "LiteLLM translates compatible options for Ollama. Keep alive controls how long the model remains loaded.", "LiteLLM traduce le opzioni compatibili per Ollama. Keep alive controlla per quanto tempo il modello resta caricato."],
    ["Sólo se envían parámetros OpenAI estándar; las opciones específicas de vLLM/NIM se omiten.", "Only standard OpenAI parameters are sent; vLLM/NIM-specific options are omitted.", "Vengono inviati solo parametri OpenAI standard; le opzioni specifiche di vLLM/NIM vengono omesse."],
    ["Razonamiento", "Reasoning", "Ragionamento"], ["Por defecto", "Default", "Predefinito"],
    ["Timeout (segundos)", "Timeout (seconds)", "Timeout (secondi)"], ["Si falla, usar", "On failure, use", "In caso di errore, usa"],
    ["Sin fallback", "No fallback", "Nessun fallback"],
    ["Descartar parámetros incompatibles", "Drop incompatible parameters", "Scarta parametri incompatibili"],
    ["Prioridad de parámetros", "Parameter precedence", "Priorità dei parametri"],
    ["El cliente puede sustituir los valores predeterminados", "The client may override default values", "Il client può sostituire i valori predefiniti"],
    ["El modelo impone sus valores en todas las llamadas", "The model enforces its values on every request", "Il modello impone i propri valori in ogni richiesta"],
    ["Extras y prueba de contrato", "Extras and contract test", "Extra e test del contratto"],
    ["cualquier opción específica del proveedor", "any provider-specific option", "qualsiasi opzione specifica del provider"],
    ["Parámetros extra", "Extra parameters", "Parametri extra"],
    ["JSON de prueba Triton", "Triton test JSON", "JSON di test Triton"],
    ["(obligatorio para Triton)", "(required for Triton)", "(obbligatorio per Triton)"],
    ["Ruta de inferencia Triton", "Triton inference path", "Percorso di inferenza Triton"],
    ["Cancelar edición", "Cancel editing", "Annulla modifica"],
    ["SEGURIDAD CONFIGURABLE", "CONFIGURABLE SECURITY", "SICUREZZA CONFIGURABILE"],
    ["Guardrail previo opcional", "Optional pre-request guardrail", "Guardrail preventivo opzionale"],
    ["Sin modelo seleccionado, las peticiones pasan directamente al modelo principal. Los embeddings se excluyen automáticamente; también puedes excluir alias concretos.", "Without a selected model, requests go directly to the main model. Embeddings are excluded automatically; you can also exclude specific aliases.", "Senza un modello selezionato, le richieste vanno direttamente al modello principale. Gli embedding vengono esclusi automaticamente; puoi anche escludere alias specifici."],
    ["Sin modelo seleccionado, las peticiones pasan directamente al modelo principal. Si eliges uno, clasificará primero cada conversación.", "Without a selected model, requests go directly to the main model. If you choose one, it will classify each conversation first.", "Senza un modello selezionato, le richieste vanno direttamente al modello principale. Se ne scegli uno, classificherà prima ogni conversazione."],
    ["Aplicar antes de los modelos de chat", "Apply before chat models", "Applica prima dei modelli di chat"],
    ["Modelo guardrail", "Guardrail model", "Modello guardrail"], ["Política", "Policy", "Criterio"],
    ["Permisiva · avisar y continuar", "Permissive · warn and continue", "Permissiva · avvisa e continua"],
    ["Restringida · bloquear", "Restricted · block", "Restrittiva · blocca"],
    ["Guardrail desactivado", "Guardrail disabled", "Guardrail disattivato"],
    ["No aplicar guardrail a", "Do not apply guardrail to", "Non applicare il guardrail a"],
    ["Los modelos de embeddings aparecen siempre excluidos.", "Embedding models are always excluded.", "I modelli di embedding sono sempre esclusi."],
    ["Guardar exclusiones", "Save exclusions", "Salva esclusioni"],
    ["Política restringida · bloquea", "Restricted policy · blocks", "Criterio restrittivo · blocca"],
    ["Política permisiva · avisa y continúa", "Permissive policy · warns and continues", "Criterio permissivo · avvisa e continua"],
    ["ASESOR DE CONFIGURACIÓN", "CONFIGURATION ADVISOR", "CONSULENTE DI CONFIGURAZIONE"],
    ["Recomendaciones por caso de uso", "Use-case recommendations", "Raccomandazioni per caso d'uso"],
    ["El asesor conversa sobre el objetivo de cada modelo, propone parámetros compatibles y sólo los aplica tras confirmación.", "The advisor discusses each model's purpose, suggests compatible parameters, and applies them only after confirmation.", "Il consulente discute l'obiettivo di ogni modello, propone parametri compatibili e li applica solo dopo conferma."],
    ["Habilitar asesor", "Enable advisor", "Abilita consulente"], ["Modelo asesor", "Advisor model", "Modello consulente"],
    ["Sin asesor", "No advisor", "Nessun consulente"], ["Hablar con el asesor", "Talk to the advisor", "Parla con il consulente"],
    ["¿Para qué quieres usar este modelo?", "What do you want to use this model for?", "Per cosa vuoi usare questo modello?"],
    ["Pedir recomendación", "Request recommendation", "Richiedi raccomandazione"], ["Aplicar al formulario", "Apply to form", "Applica al modulo"],
    ["INTEGRACIÓN OPCIONAL", "OPTIONAL INTEGRATION", "INTEGRAZIONE OPZIONALE"],
    ["Descubre Model Endpoints de AI Inference y modelos desplegados en uno o varios Workbenches.", "Discover AI Inference Model Endpoints and models deployed in one or more Workbenches.", "Scopri i Model Endpoint di AI Inference e i modelli distribuiti in uno o più Workbench."],
    ["Credenciales locales · nunca se muestran", "Local credentials · never displayed", "Credenziali locali · mai visualizzate"],
    ["Nombre", "Name", "Nome"], ["Servicio", "Service", "Servizio"], ["Instalación", "Deployment", "Installazione"],
    ["Runtime de CDP Base", "CDP Base Runtime", "Runtime CDP Base"],
    ["7.3.2 o posterior", "7.3.2 or later", "7.3.2 o successivo"], ["Anterior a 7.3.2", "Earlier than 7.3.2", "Precedente alla 7.3.2"],
    ["Comprobar cada", "Check every", "Controlla ogni"], ["Minutos · mientras el panel esté abierto", "Minutes · while the dashboard is open", "Minuti · mentre il pannello è aperto"],
    ["URL de endpoints", "Endpoint URL", "URL degli endpoint"],
    ["Origen que publica el catálogo y los modelos; no es la consola central de CDP.", "Origin that publishes the catalogue and models; it is not the central CDP console.", "Origine che pubblica catalogo e modelli; non è la console CDP centrale."],
    ["Cloud · token CDP renovable automáticamente", "Cloud · automatically renewable CDP token", "Cloud · token CDP rinnovabile automaticamente"],
    ["El JWT es temporal. IA Gateway puede regenerarlo antes de caducar utilizando un Access Key ID y Private Key.", "The JWT is temporary. IA Gateway can regenerate it before expiry using an Access Key ID and Private Key.", "Il JWT è temporaneo. IA Gateway può rigenerarlo prima della scadenza usando Access Key ID e Private Key."],
    ["CDP token o API v2 key actual", "Current CDP token or API v2 key", "Token CDP o API v2 key attuale"],
    ["URL de tokens IAM", "IAM token URL", "URL token IAM"],
    ["On-premise 7.3.2+ · credencial de servicio", "On-premises 7.3.2+ · service credential", "On-premise 7.3.2+ · credenziale di servizio"],
    ["La opción recomendada es generar continuamente un CDP_TOKEN de UMS con una access key de machine user. También puedes pegar una Knox API key larga y rotarla según la política del Data Lake.", "The recommended option is to continuously generate a UMS CDP_TOKEN with a machine-user access key. You can also paste a long-lived Knox API key and rotate it according to the Data Lake policy.", "L'opzione consigliata è generare continuamente un CDP_TOKEN UMS con una access key di un machine user. Puoi anche incollare una Knox API key a lunga durata e ruotarla secondo la policy del Data Lake."],
    ["Renovación automática de AI Inference:", "Automatic AI Inference renewal:", "Rinnovo automatico di AI Inference:"],
    ["deja la credencial anterior vacía y usa una access key/private key creada en User Management del Control Plane. IA Gateway generará el primer UMS JWT y lo sustituirá anticipadamente sin reiniciar LiteLLM.", "leave the credential above blank and use an access key/private key created in Control Plane User Management. IA Gateway will generate the first UMS JWT and replace it early without restarting LiteLLM.", "lascia vuota la credenziale precedente e usa una access key/private key creata in User Management del Control Plane. IA Gateway genererà il primo JWT UMS e lo sostituirà in anticipo senza riavviare LiteLLM."],
    ["URL del Control Plane CDP", "CDP Control Plane URL", "URL del Control Plane CDP"],
    ["Caducidad administrativa", "Administrative expiry", "Scadenza amministrativa"],
    ["Para API keys opacas, permite avisar y preparar la rotación antes de la fecha configurada en Cloudera.", "For opaque API keys, this enables advance warning and rotation before the date configured in Cloudera.", "Per le API key opache, consente di avvisare e preparare la rotazione prima della data configurata in Cloudera."],
    ["Knox API key o JWT", "Knox API key or JWT", "Knox API key o JWT"],
    ["Knox API key de AI Inference o CDP JWT (UMS)", "AI Inference Knox API key or CDP JWT (UMS)", "Knox API key di AI Inference o CDP JWT (UMS)"],
    ["API key de Workbench", "Workbench API key", "API key di Workbench"],
    ["URL de Knox Token API v2", "Knox Token API v2 URL", "URL Knox Token API v2"],
    ["opcional · referencia", "optional · reference", "opzionale · riferimento"],
    ["On-premise anterior a 7.3.2 · JWT Knox renovable", "On-premises before 7.3.2 · renewable Knox JWT", "On-premise precedente alla 7.3.2 · JWT Knox rinnovabile"],
    ["IA Gateway solicita un JWT nuevo con Basic Authentication cuando falta o está próximo a caducar.", "IA Gateway requests a new JWT with Basic Authentication when it is missing or close to expiry.", "IA Gateway richiede un nuovo JWT con Basic Authentication quando manca o sta per scadere."],
    ["CDP/Knox JWT actual", "Current CDP/Knox JWT", "JWT CDP/Knox attuale"],
    ["URL de tokens Knox", "Knox token URL", "URL token Knox"],
    ["Ciclo de vida:", "Lifecycle:", "Ciclo di vita:"],
    ["IA Gateway detecta la caducidad de los JWT y programa la renovación antes de que entren en riesgo. Sólo muestra renovación automática cuando dispone de un mecanismo no interactivo compatible; las API keys opacas de Knox o Workbench deben rotarse antes de su fecha administrativa.", "IA Gateway detects JWT expiry and schedules renewal before they are at risk. It only shows automatic renewal when a compatible non-interactive mechanism is available; opaque Knox or Workbench API keys must be rotated before their administrative expiry date.", "IA Gateway rileva la scadenza dei JWT e pianifica il rinnovo prima che siano a rischio. Mostra il rinnovo automatico solo quando è disponibile un meccanismo non interattivo compatibile; le API key opache Knox o Workbench devono essere ruotate prima della data di scadenza amministrativa."],
    ["Renovación prevista:", "Scheduled renewal:", "Rinnovo previsto:"],
    ["Rotar antes de:", "Rotate before:", "Ruota prima di:"],
    ["Workbench usa una API key con audiencia API/Application y fecha de expiración. Créala en User Settings y rótala antes de esa fecha.", "Workbench uses an API key with API/Application audience and an expiry date. Create it in User Settings and rotate it before that date.", "Workbench usa una API key con audience API/Application e una data di scadenza. Creala in User Settings e ruotala prima di tale data."],
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
      , [/^Nivel (\d+) · (\d+) en vuelo$/, "Level $1 · $2 in flight"], [/^Nivel (\d+)$/, "Level $1"],
      [/^Mínimo (\d+) para alcanzar realmente el nivel (\d+)\.$/, "Minimum $1 to actually reach level $2."],
      [/^(\d+) peticiones$/, "$1 requests"], [/^(\d+) s previstos$/, "$1 s expected"]
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
      , [/^Nivel (\d+) · (\d+) en vuelo$/, "Livello $1 · $2 in esecuzione"], [/^Nivel (\d+)$/, "Livello $1"],
      [/^Mínimo (\d+) para alcanzar realmente el nivel (\d+)\.$/, "Minimo $1 per raggiungere effettivamente il livello $2."],
      [/^(\d+) peticiones$/, "$1 richieste"], [/^(\d+) s previstos$/, "$1 s previsti"]
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
