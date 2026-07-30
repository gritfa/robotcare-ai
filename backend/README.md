# RobotCare AI Backend

FastAPI backend for a deterministic, safety-bounded robot vacuum diagnostic flow.

## Run locally

```powershell
python -m pip install -e ".[test]"
uvicorn app.main:app --reload
```

OpenAPI documentation: `http://127.0.0.1:8000/docs`.

PostgreSQL (with pgvector) is the only supported database. Set
`ROBOTCARE_DATABASE_URL` to a SQLAlchemy PostgreSQL URL
(`postgresql+psycopg://...`); run `alembic upgrade head` before starting the
API. Startup seeds two Haier models (`JH69U1`, `VC35U1`) with deterministic
flows for failure to dock and Wi-Fi setup failure.

## Test

Tests run against a dedicated PostgreSQL test container
(`pgvector/pgvector:pg16`, default URL
`postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_test`, override
with `ROBOTCARE_TEST_DATABASE_URL`):

```powershell
python -m pytest
```

PDF export and binary attachment upload are intentionally deferred. The current
milestone produces a persisted plain-text service report.

## API contract (MVP)

All application endpoints use the `/api/v1` prefix. Except for registration,
login, and model listing, send the JWT as `Authorization: Bearer <token>`.
Registration and login accept JSON, not form data.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/register` | Create an account and return a token |
| POST | `/auth/login` | Authenticate and return a token |
| GET | `/auth/me` | Return the authenticated user |
| GET | `/models` | List active seeded robot models |
| POST/GET | `/devices` | Create or list the current user's devices |
| GET/PATCH/DELETE | `/devices/{id}` | Read, update, or delete an owned device |
| POST/GET | `/diagnostics` | Start or list deterministic diagnostics |
| GET | `/diagnostics/{id}` | Read an owned diagnostic |
| GET | `/diagnostics/{id}/steps/current` | Return exactly one current safe step |
| POST | `/diagnostics/{id}/feedback` | Submit `resolved` or `not_resolved` |
| POST/GET | `/diagnostics/{id}/report` | Create or read an unresolved service report |
| POST | `/knowledge/search` | Search model-specific PDF chunks with a JWT |
| GET | `/knowledge/status` | Return document, chunk, and vector counts per model |

Knowledge search accepts:

```json
{"robot_model_id": 1, "query": "无法回充怎么办", "top_k": 5, "min_score": 0.25}
```

It returns only persisted source chunks and their cosine scores, document title,
source URL, and one-based PDF page number. It does not generate an answer or
invent a citation. Results below `min_score` are omitted.

Ingest an official PDF with its real source URL:

```powershell
python -m app.knowledge_cli ingest --model-code JH69U1 --pdf .\manual.pdf --source-url https://example.com/manual.pdf
```

The ingester uses SHA-256 to skip unchanged files. When the same model/source
URL changes, its old chunks are replaced in one transaction. Set
Set `ROBOTCARE_DASHSCOPE_API_KEY` before using the real embedding provider. For
an Alibaba Cloud Model Studio workspace endpoint, also set
`ROBOTCARE_DASHSCOPE_BASE_URL` to the region-specific DashScope SDK URL ending
in `/api/v1` (not the OpenAI-compatible URL ending in `/compatible-mode/v1`).

Core request shapes are deliberately stable for the Vue client:

```json
{"robot_model_id": 1, "nickname": "客厅机器人", "serial_number": null}
```

```json
{
  "device_id": 1,
  "issue_category_code": "return_to_dock_failure",
  "issue_description": "机器人无法自动回到充电座",
  "error_code": null
}
```

```json
{"outcome": "not_resolved"}
```

The state machine is deterministic: `in_progress` advances one configured step
at a time, `resolved` ends immediately, and failure of the final step produces
`unresolved`. A service report can be generated only for `unresolved` sessions.
Cross-user resource access returns `403`; missing resources return `404`; invalid
state transitions return `409`; request validation returns `422`.

## Proven in the current MVP

- Database tables come from Alembic migrations; the two initial flows are seeded at startup.
- JWT authentication uses Argon2 password hashes.
- Device, diagnostic, feedback, and report routes enforce resource ownership.
- Automated tests cover registration/login, isolation, both terminal states,
  deterministic progression, flow validation, and idempotent report creation.
- PDF knowledge ingestion persists page-scoped chunks and JSON embeddings.
- Search is strictly filtered by robot model and rejects low-score results.
- Offline fake-provider tests prove pagination, ten-item embedding batches,
  idempotent replacement, model isolation, ranking, sources, status, and JWT enforcement.

Not yet implemented: generated RAG answers, PDF report rendering, admin APIs,
PostgreSQL migrations, and a production deployment configuration.
