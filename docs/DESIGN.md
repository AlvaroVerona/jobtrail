# Job Search App — Documento de diseño

Estado: borrador v0.1 · 2026-10-16 · Autor: Álvaro Verona
Nombre: `jobtrail` (decidido 2026-10-16, ver §18).

## 1. Problema

Una búsqueda de empleo hecha por volumen (Easy Apply) no deja información útil. En el tracker real que motivó este proyecto, con más de 200 aplicaciones casi todas por Easy Apply, no había ni una entrevista y ningún dato para saber qué canal, qué tipo de rol o qué versión de CV funcionaba. Las herramientas existentes son hojas de cálculo (sin analítica) o servicios cerrados (sin control de tus datos).

## 2. Objetivo

Una herramienta local para llevar una búsqueda de empleo **dirigida**: pocas aplicaciones, bien elegidas, medidas. Tres capacidades:

1. **Registrar** aplicaciones, contactos e interacciones con un historial de estados fiable.
2. **Medir** qué funciona (conversión por canal, track y versión de CV) y qué está estancado.
3. **Asistir** con un LLM local: estructurar una oferta, puntuar su encaje con tu CV con evidencia, sugerir qué resaltar y redactar follow-ups.

Es también un proyecto de portfolio: debe poder clonarse, instalarse en un comando y probarse con datos de demostración sin tener cuenta de nada.

## 3. No-objetivos (v1)

- **No hace scraping de LinkedIn ni de ningún portal.** Incumple sus términos de uso y es frágil. Las ofertas entran pegando texto o una URL por el usuario.
- **No envía aplicaciones ni mensajes por ti.** Redacta borradores; tú envías.
- **No es multiusuario ni está alojada.** Una persona, una máquina, datos locales.
- **No inventa experiencia.** Las sugerencias de CV solo reordenan y resaltan lo que ya está en el CV (§8).
- Sin app móvil ni sincronización en la nube.

## 4. Requisitos de replicabilidad

| Requisito | Decisión |
|---|---|
| Multiplataforma | Solo Python 3.12+, sin shell nativo. |
| Instalación | `uv sync` y `uv run jobtrail serve`. |
| Sin cuentas ni claves | LLM local con Ollama por defecto. Funciona sin LLM (registro y analítica) si Ollama no está. |
| Probar sin datos propios | `uv run jobtrail demo` carga datos sintéticos. |
| Tus datos no acaban en git | `data/` y `.env` en `.gitignore`; la base real nunca está en el repo. |
| Reproducible | Tests herméticos, sin llamadas reales al LLM. CI en GitHub Actions. |

## 5. Arquitectura

Un solo proceso Python que sirve API y UI.

```
 Navegador  ──HTTP──▶  FastAPI (routes)
                         ├── UI: Jinja2 + HTMX (sin build de JS)
                         ├── services/   lógica de negocio
                         │     ├── tracker, analytics, followups
                         │     └── posting_parser, fit_scorer, cv_advisor, message_drafter
                         ├── llm/        LLMClient (Ollama por defecto, FakeLLM en tests)
                         ├── ingestion/  CV (pdf, docx, md), importadores CSV
                         └── database/   SQLAlchemy + SQLite (data/jobtrail.db)
```

**UI: FastAPI + Jinja2 + HTMX, no Streamlit.** Streamlit sería más rápido de montar, pero acopla la lógica a la UI, se testea peor y se ve como un prototipo. Con Jinja2 + HTMX la capa de servicios queda aislada y testeable, y se puede añadir una CLI sobre los mismos servicios.

## 6. Qué se reutiliza de BusinessConsultant

Se copia y se adapta (es código propio), sin dependencia entre repos:

| Origen (`business-consulting-app/sidecar/src`) | Uso aquí |
|---|---|
| `llm/base.py`, `ollama_client.py` | Interfaz `LLMClient` y cliente Ollama. |
| `llm/json_utils.py`, `llm/retry.py` | Parseo de JSON con sanear-y-reintentar para salidas estructuradas. |
| `ingestion/pdf_parser.py`, `docx_parser.py`, `registry.py` | Lectura del CV. |
| `services/evidence_validation.py` (patrón) | Comprobar que cada cita del LLM existe de verdad en el CV (§8). |
| Patrón `FakeLLM` en `tests/` | Tests herméticos. |
| `schemas/` con Pydantic por tarea | Una salida tipada por capacidad de LLM. |

**No se reutiliza:** `anthropic`/`claude_client.py` (esta app no necesita razonamiento profundo; ver §9), `reportlab`/`python-pptx`, el shell SwiftUI, el router Claude/Ollama.

## 7. Modelo de datos

SQLite vía SQLAlchemy. Fechas en UTC. El historial de estados es una tabla propia, no solo el estado actual, porque la analítica de embudo depende de ella.

| Tabla | Campos principales |
|---|---|
| `company` | id, name, website, notes |
| `application` | id, company_id, role, track, channel, status, posting_id, cv_version_id, applied_at, salary_expected, salary_asked, notes |
| `status_event` | id, application_id, from_status, to_status, at, note |
| `posting` | id, url, raw_text, parsed (JSON), seniority, location, fetched_at |
| `contact` | id, company_id, name, role, channel, notes |
| `interaction` | id, application_id, contact_id, kind (email, call, interview, message), at, summary |
| `followup` | id, application_id, due_at, kind, done_at, draft |
| `cv_version` | id, label, file_hash, text, created_at |
| `fit_analysis` | id, application_id/posting_id, cv_version_id, score, detail (JSON), model, created_at |

**Estados:** `wishlist → to_apply → applied → follow_up_sent → screening → interviewing → offer`, y terminales `rejected`, `no_response`, `withdrawn`. `no_response` se asigna a mano o por la regla de estancamiento (§10), nunca de forma silenciosa.

**Listas configurables, no fijas:** `channel` (Easy Apply, web de empresa, referido, recruiter, email) y `track` son tablas de opciones editables, para que otra persona use sus propios tracks.

## 8. Capacidades con LLM

Cada una tiene un esquema Pydantic de salida, un prompt versionado y un test con `FakeLLM`.

1. **`posting_parser`**: texto de oferta → `{title, company, seniority, must_have[], nice_to_have[], location, remote, salary_range?}`. Rechaza campos no presentes en el texto en lugar de rellenarlos.
2. **`fit_scorer`**: CV + oferta estructurada → puntuación 0–100 y lista de requisitos con `met | partial | missing`. **Cada `met`/`partial` debe llevar una cita literal del CV.** Una validación determinista comprueba que la cita aparece en el texto del CV; si no aparece, el punto se degrada a `missing`. La puntuación final sale de esa lista con una fórmula fija, no de un número que dé el modelo.
3. **`cv_advisor`**: sugiere qué experiencias y proyectos subir o resaltar para esa oferta, citando el CV. Prohibido proponer texto con habilidades o logros que no estén en el CV; la misma validación de citas lo hace cumplir.
4. **`message_drafter`**: borrador de follow-up o mensaje a recruiter, usando contacto, historial y estado. Sale como texto editable.

**Sin LLM disponible**, estas cuatro se desactivan con un mensaje claro y el resto de la app sigue funcionando.

**Estado (M6):** las cuatro capacidades están implementadas. `cv_advisor` solo propone líneas del CV para requisitos que el análisis de encaje verificó, y `message_drafter` no recibe los requisitos de la oferta y revisa cifras y nombres propios del borrador. Ver [`decisions/0002-outreach-guards.md`](decisions/0002-outreach-guards.md): la validación de citas del M5 no bastaba para texto libre.

**Límite que hay que conocer (M5):** la validación demuestra que la cita *existe* en el CV (palabras completas, sin distinguir mayúsculas ni saltos de línea), no que *respalde* el requisito. Por eso la interfaz muestra cada cita junto a su requisito. Tampoco puede citarse con un solo carácter (un lenguaje como "R" debe citarse con la línea de habilidades completa). El texto de la oferta es entrada no confiable que llega al prompt; como el modelo no tiene herramientas ni ejecuta acciones, lo peor que puede hacer una oferta maliciosa es sesgar su propio análisis, y las citas siguen verificadas contra el CV.

**Cambio respecto al borrador:** `pypdf`, `python-docx` y `ollama` son dependencias normales y no extras, para que `uv sync` lo deje todo listo.

## 9. Decisión de LLM

Ollama local por defecto (`llama3.1` o `qwen3:8b`), modelo configurable por `.env`. No hay backend de pago. Las tareas de arriba son extracción y comparación contra un texto dado, que es lo que un modelo local de 8B hace aceptablemente, y la validación determinista de citas cubre su punto débil (alucinar evidencia). Si en pruebas el modelo local falla de forma sistemática, se reevalúa con datos, no por defecto.

## 10. Analítica

Todo calculado en SQL/Python, sin LLM.

- **Embudo** por canal, track y versión de CV: aplicadas → respuesta → screening → entrevista → oferta.
- **Tasa de respuesta** y **tiempo hasta respuesta**.
- **Estancadas:** aplicaciones en `applied` o `follow_up_sent` más de N días (configurable, por defecto 21).
- **Aviso de muestra pequeña:** ningún porcentaje se muestra sin su `n`, y por debajo de un mínimo configurable se marca como no concluyente. (Con un punto de partida de más de 200 aplicaciones y ninguna entrevista no se pueden comparar tracks; la herramienta debe decirlo.)
- **Carga semanal:** aplicaciones y follow-ups de la semana frente al objetivo.

## 11. Importación y exportación

- **CSV** de entrada y salida para todas las tablas principales.
- **Importador del tracker de Notion** vía CSV exportado, con el mapeo de columnas conocido (Company, Role, Status, Applied date, Plataforma, Canal, Track, etc.). Sin API de Notion en v1: así el repo no depende de ninguna cuenta.
- **Notion API opcional:** idea para más adelante, no implementada.

## 12. Interfaz (v1)

Páginas: **Tablero** (kanban por estado), **Aplicaciones** (tabla filtrable), **Detalle de aplicación** (historial, interacciones, fit, borradores), **Seguimiento** (follow-ups vencidos y estancadas), **Analítica**, **Ajustes** (canales, tracks, umbrales, modelo). HTMX para cambios de estado y formularios sin recargar la página.

## 13. Estructura del repo

```
jobtrail/
├── README.md              # qué es, captura, instalación en 3 comandos, demo
├── LICENSE                # MIT
├── pyproject.toml
├── .env.example
├── .github/workflows/ci.yml
├── docs/
│   ├── DESIGN.md
│   ├── decisions/         # ADRs cortos
│   └── ENGINEERING_LOG.md
├── src/jobtrail/
│   ├── main.py            # app FastAPI
│   ├── cli.py             # serve, demo, import-csv
│   ├── config.py
│   ├── database/          # models, repository, init
│   ├── schemas/           # Pydantic
│   ├── services/          # tracker, analytics, followups, fit, cv_advisor, drafter
│   ├── llm/               # base, ollama_client, json_utils, retry, fake
│   ├── ingestion/         # cv parsers, csv import
│   └── web/               # routes, templates/, static/
(datos de demo en src/jobtrail/demo_data/, CSV sintético sin nada personal)
└── tests/
```

## 14. Pruebas y CI

- Tests herméticos: el LLM siempre es `FakeLLM`; la base es SQLite en memoria.
- Cobertura obligatoria en lo determinista: máquina de estados, embudo, regla de estancamiento, validación de citas, importador CSV.
- CI: `ruff`, `pytest`, y un job que arranca el servidor con datos de demo y comprueba `/health` y una página.
- Un test explícito de que **un fit con citas inventadas se degrada**, porque es la garantía central de calidad.

## 15. Privacidad

El CV y el tracker son datos personales. Todo queda en local; la única salida de red es hacia Ollama en `localhost`. `data/`, `.env` y cualquier `*.db` van en `.gitignore`. El README lo dice explícitamente y la demo usa datos sintéticos.

## 16. Hitos

| # | Entregable | Criterio de hecho |
|---|---|---|
| M0 | Repo, CI, esqueleto, `/health` | CI en verde en GitHub |
| M1 | Modelo de datos, tracker y máquina de estados | CRUD + historial de estados con tests |
| M2 | Importador CSV y `demo` | Un export real de más de 200 filas importa y la demo corre en limpio |
| M3 | Analítica y vista de seguimiento | Embudo y estancadas correctos sobre datos de demo |
| M4 | UI web (tablero, tabla, detalle) | Flujo completo navegable |
| M5 | LLM: `posting_parser` y `fit_scorer` con validación de citas | Test de citas inventadas pasa |
| M6 | `cv_advisor`, `message_drafter` | Salidas validadas y editables |
| M7 | README, capturas, pulido para portfolio | Alguien ajeno lo instala y lo prueba en <5 min |

M0–M4 ya dan una herramienta útil sin LLM; M5–M7 son la parte diferenciadora.

## 17. Riesgos

| Riesgo | Mitigación |
|---|---|
| El modelo local puntúa mal el encaje | Puntuación por fórmula sobre requisitos con cita verificada; el modelo solo extrae y relaciona. |
| Alucinación de experiencia en sugerencias de CV | Validación determinista de citas; test dedicado. |
| Muestras pequeñas llevan a conclusiones falsas | `n` visible y marca de no concluyente. |
| Alcance que crece (scraping, auto-aplicar) | Está en no-objetivos; se revisa solo con un ADR. |
| Datos personales filtrados al repo | `.gitignore`, demo sintética, revisión antes de hacerlo público. |

## 18. Decisiones

Resueltas el 2026-10-16 (se aceptaron las recomendaciones del borrador):

1. **Nombre:** `jobtrail` (paquete `jobtrail`, comando `jobtrail`).
2. **CLI:** solo `serve`, `demo` e `import-csv` en v1.
3. **Idioma:** README y UI en inglés (alcance de portfolio). Los textos de UI se centralizan para poder traducirlos; este documento de diseño queda en español.
4. **Licencia:** MIT.

Resuelta con datos:

5. **Modelo local por defecto:** `qwen3:8b`, decidido el 2026-10-16 con una prueba comparativa (ver [`decisions/0001-default-model.md`](decisions/0001-default-model.md)).
