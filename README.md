# IA Gateway

> La guía completa de arquitectura, instalación y operación de la versión beta está en [BETA.md](BETA.md).

Panel web local para administrar un proxy LiteLLM configurado mediante `config.yaml`.

## Instalación

Requiere Python 3.9 o posterior.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Arranque

Primero crea el fichero local de secretos (sólo es necesario una vez):

```bash
cp .env.example .env
```

Edita `.env` y configura las dos credenciales. `OPENAI_API_KEY` permite que
LiteLLM llame a OpenAI; `LITELLM_MASTER_KEY` protege la entrada al gateway para
todos los modelos, incluidos los locales. Este fichero está excluido de Git.

Puedes generar una clave general robusta con:

```bash
openssl rand -hex 32
```

```bash
source .venv/bin/activate
python app.py
```

Abre <http://127.0.0.1:5100>. Desde el panel puedes:

- arrancar y detener LiteLLM, expuesto en `http://127.0.0.1:4000`;
- comprobar que el puerto está realmente disponible y ver CPU/RAM cada 3 segundos;
- activar o desactivar modelos del `config.yaml`;
- enviar una prueba rápida de chat o embeddings a cada modelo activo;
- consultar por modelo las entradas, salidas, duración y errores.

Para probar el gateway una vez arrancado:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model":"topito","messages":[{"role":"user","content":"Hola"}]}'
```

La prueba integrada en la web añade esa autorización desde el backend. La clave
general nunca se envía al JavaScript ni aparece en el navegador.

Los estados y logs se guardan bajo `runtime/`, que no se versiona. El fichero
`config.yaml` original nunca se modifica: se crea una copia filtrada en
`runtime/active_config.yaml`.

> El panel muestra el contenido completo de prompts y respuestas. No lo expongas
> a una red no confiable y revisa tus obligaciones de privacidad. Campos comunes
> de autenticación se ocultan antes de escribirlos en SQLite.

## Configuración opcional

Puedes conservar en `config.yaml` otras secciones de LiteLLM (`router_settings`,
`general_settings`, etc.); se copian a la configuración activa. El callback local
se añade junto a los callbacks que ya existan.

Documentación de API del propio panel: <http://127.0.0.1:5100/api/docs>.
