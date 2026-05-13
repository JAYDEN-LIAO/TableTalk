# CLAUDE.md

This file provides guidance to Claude Code when working in `apps/api/`.

## Commands

```bash
# From repo root
pnpm dev:api
pnpm --filter @selgetabel/api install

# From apps/api
uv sync
uv run uvicorn app.main:app --reload --host localhost --port 8000
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"
uv run pytest tests
```

## Directory Structure

```
app/
├── main.py                 # FastAPI app entry, middleware, exception handling
├── api/
│   ├── main.py             # Router aggregation
│   └── routes/             # auth, btrack, chat, file, llm, role, thread, user, fixture
├── core/                   # config, database, jwt, crypto, permissions, branding, SSE
├── engine/                 # parser, executor, formula generation, intent, context, LLM providers, token_counter
├── processor/              # staged processing pipeline
│   └── stages/             # base, generate_validate, execute, errors
├── services/               # chat, processing, context, intent, auth, file, thread, storage
├── models/                 # SQLAlchemy ORM models
├── schemas/                # Pydantic request/response models
├── persistence/            # data access helpers
├── events/                 # event bus and types
└── scripts/                # initialization helpers
```

## Key Concepts

### Request Paths

- `chat` routes handle the main conversational and processing entry points.
- Auth, RBAC, user, file, thread, btrack, and LLM configuration are split into separate route modules.
- Fixture routes are only included in development mode.

### Agent

- `excel_agent.py` implements a **tool-calling agent loop** with 4 tools (conversation/clarification/processing/analysis).
- **v2 Guardrails**: max_iterations hard limit (5), token budget tracking, stagnation self-correction (Jaccard similarity ≥70% → auto-clarify).
- **v2 Structured Observation**: tool results formatted as JSON with status/summary/file_changes/errors, token-budget-aware trimming.
- Guardrail events persisted to `context_snapshot.guardrails` for post-hoc analysis.

### Processing Pipeline

The current backend is split between conversational handling and operation execution:

1. Accept request and resolve user/file/thread context.
2. Agent selects tool via tool calling (with guardrails).
3. Build prompt/context payload (v2: tiktoken precise counting, 3-level compression, schema-on-demand).
4. Generate model output through provider adapters.
5. Validate structured operations in `engine/parser.py` (v2: error-classified targeted retry hints).
6. Execute operations and generate Excel output through `engine/executor.py` and `engine/excel_generator.py`.
7. Stream progress and results back to the client.

### LLM Integration

- Provider abstractions live in `app/engine/llm_providers/`.
- `llm_client.py`, `intent_classifier.py`, and `context_builder.py` are central to model-facing behavior.
- Database-backed provider configuration is handled through the LLM APIs and related service code.

## Data And Infra

- ORM models live in `app/models/`; schema contracts live in `app/schemas/`.
- Database migrations are managed with Alembic in `alembic/`.
- Object storage integration is implemented in `app/services/oss.py`.
- Shared response envelopes use `app/schemas/response.py`.

## Tests

- Automated tests currently live in `tests/`.
- Existing coverage focuses on processor stages and stage export behavior.
- When touching parser, executor, pipeline, or intent logic, add or extend targeted pytest coverage instead of relying only on manual testing.

## Change Guidance

- Prefer keeping route handlers thin and move business logic into `services/`, `engine/`, or `processor/`.
- Avoid mixing intent/chat concerns with Excel execution concerns unless the flow truly spans both.
- If you add a new operation type, update parser validation, executor behavior, Excel generation, prompts, and the corresponding spec in `docs/specs/OPERATION_SPEC.md`.
