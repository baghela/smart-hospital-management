# Smart Hospital Management System

An end-to-end hospital operations platform with a **LangGraph AI copilot** and a
**RAG-based hospital FAQ assistant**, built on Streamlit + FastAPI + PostgreSQL.

---

## 1. Who this is for and why it exists


**Business outcome.** Front-desk and billing staff currently answer the same
operational questions by phone and lose time hunting through policy PDFs, while
high-impact actions (discharge, claim decisions, dispensing) are approved over
chat with no audit trail. This system:

1. Answers operational questions from approved documents only, with citations.
2. Refuses to answer anything it cannot verify, and routes the caller to the right desk.
3. Puts a recorded, role-checked human approval in front of the three riskiest actions.

**How success is judged.**

| Measure | Target | Current (measured) |
| --- | --- | --- |
| Retrieval document recall | >= 95% | **100%** |
| Keyword recall on answerable questions | >= 95% | **100%** |
| Evidence sufficiency on answerable questions | >= 95% | **100%** |
| Abstention accuracy on unanswerable questions | 100% | **100%** |
| Guardrail accuracy (injection / clinical / emergency) | 100% | **100%** |
| Answerable vs unanswerable score margin | >= 0.15 | **0.28** |
| Retrieval latency p95 | < 2000 ms | **~9 ms** |
| High-impact actions executed without sign-off | 0 | **0** (enforced in code + tests) |
| Automated tests passing | 100% | **184 / 184** |

Reproduce with `python -m evaluation.run_eval` and `python -m pytest tests -q`.

---

## 2. MVP scope (frozen)

**In scope**

1. Patient registration, unique patient IDs, search, profiles, consent.
2. Department and doctor directory.
3. Appointment booking, reschedule, cancel, check-in, queue management, plus a
   conversational **booking agent** (Appointments → "🤖 Book with agent"): name a
   doctor (and optionally a date), pick a free slot, confirm, and the slot is
   booked and blocked (`backend/copilot/booking_agent.py`).
4. OPD encounters with clinician sign-off.
5. Admission, ward/bed management, interactive bed board, transfers, gated discharge.
6. Laboratory orders, specimen tracking, results.
7. Pharmacy inventory, prescriptions, gated dispensing.
8. Itemised billing, invoices, payments, refunds, receipts.
9. Insurance policies, eligibility, claims, gated claim decisions.
10. Discharge summaries and follow-up booking.
11. Admin dashboards, live reports, audit views.
12. FAQ RAG assistant with hybrid retrieval, citations, abstention and escalation.

**Explicitly not implemented (and why)**

| Deferred | Reason |
| --- | --- |
| pgvector / Chroma index | Embeddings are stored as JSON in PostgreSQL to keep a single datastore; see section 11 |
| Cross-encoder reranker | A deterministic lexical reranker is used so tests stay offline and free |
| OCR for scanned PDFs | Code path exists (`rag/chunking.py::_ocr_page`) but needs a local tesseract install |
| Insurer / payer API integration | No external sandbox available; claim status is internal only |
| Multi-tenant hospital groups | Single-tenant by design for the MVP |

---

## 3. Architecture

```
streamlit_app/        Presentation only. Calls services; enforces nothing itself.
  app.py              Login + live overview
  pages/              13 role-aware pages (dashboard -> admin)
  common.py           Auth state, DB sessions, KPI/badge/pagination widgets

backend/
  core/               config.py (env-driven), errors.py, logging.py (JSON)
  db/                 base.py, session.py (engine + session scope)
  models/             entities.py (21 tables), enums.py
  auth/               security.py (PBKDF2 + HMAC tokens), rbac.py (permission matrix)
  guardrails/         policy.py - injection, clinical boundary, uploads, budgets, rate limits
  audit/              logger.py - PHI-scrubbed audit trail
  services/           patient, directory, appointment, admission, billing,
                      insurance, lab, pharmacy, approval, discharge, report, auth
  ai/                 providers.py - GroqProvider | GeminiProvider (Bearer-token REST)
  copilot/            graph.py (LangGraph), orchestrator.py, tools.py, prompts.py
  api/                schemas.py, deps.py, routes/ (9 routers)
  main.py             FastAPI app, error contract, tracing, health checks

rag/
  chunking.py         PDF/DOCX/CSV/TXT extraction, page + section metadata
  embeddings.py       sentence-transformers, with an offline hashing fallback
  ingest.py           Validate -> sanitize -> chunk -> embed -> index
  retriever.py        BM25 + dense vector, RRF fusion, rerank, dedupe, thresholding
  citations.py        Per-sentence grounding validation and conservative repair
  escalation_matrix.py / .yaml   Static role-mapped contact matrix

alembic/              Migration environment + baseline revision
data/seed/            Synthetic seed data and 5 sample FAQ documents
evaluation/           dataset.json (24 cases) + run_eval.py
tests/                unit/ (6 modules) + integration/ (5 modules) - 184 tests
```

### Layer rules

- Streamlit never enforces permissions; services do. Hiding a button is cosmetic.
- Business rules (money, scheduling, eligibility, stock) are deterministic Python.
- The LLM only rephrases retrieved evidence, and only when it can cite it.
- Retrieved documents are data, never instructions.

---

## 4. Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ (or Docker)
- At least one AI provider key (optional - the system runs and safely abstains without one)

### Install

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Optional extras:

```powershell
pip install "sentence-transformers>=3.0.0"
pip install pytesseract Pillow
```

### Configure

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the generated value into `SECRET_KEY`.

### API keys

Add whichever you have to `.env`. Groq and Gemini are called directly over
their OpenAI-compatible REST endpoints with `Authorization: Bearer <key>` (only
`httpx` is required). The chain in `AI_PROVIDER_ORDER` is used for fallback.

```bash
AI_PROVIDER_ORDER=groq,gemini

GROQ_API_KEY=...
GROQ_MODEL=llama-3.1-8b-instant

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
```

Keys are read from the environment, never logged, and never returned by the API.

Outbound TLS uses the OS certificate store (`truststore`), so it works behind
corporate TLS inspection. Set `AI_CA_BUNDLE=<pem>` to use a specific CA bundle.
If a provider fails, the reason (missing key, HTTP status, proxy block) is
logged and shown in the FAQ assistant's execution trace.

---

## 5. Database initialization and migrations

### PostgreSQL (primary path)

```powershell
docker run --name hospital-db -e POSTGRES_USER=hospital -e POSTGRES_PASSWORD=hospital -e POSTGRES_DB=hospital -p 5432:5432 -d postgres:16-alpine
alembic upgrade head
```

Rollback and new revisions:

```powershell
alembic downgrade base
alembic revision --autogenerate -m "describe the change"
```

### SQLite (offline check only)

```powershell
$env:DATABASE_URL = "sqlite:///./dev_hospital.db"
```

Schema is created automatically on first use. Used by the test suite.

### Concurrency guarantees

| Constraint | Prevents |
| --- | --- |
| `uq_appointment_doctor_active_slot` | Two active appointments in the same doctor slot |
| `uq_admission_active_bed` | Two active admissions on the same bed |
| `uq_bed_ward_label`, `uq_faq_title_version` | Duplicate beds / document versions |

`active_slot` and `active_bed_id` are set to `NULL` on cancellation or discharge,
so freed resources become bookable again while history is preserved.

---

## 6. Seed data and document ingestion

```powershell
python -m data.seed.seed_data
```

Creates 8 staff accounts, 2 patient-portal logins, 4 departments, 4 doctors,
3 wards (18 beds), 6 synthetic patients, appointments, an admission, pharmacy
stock, an invoice with a payment, an insurance policy with a claim, a lab order,
and indexes the 5 sample FAQ documents.

| Username | Role | Password |
| --- | --- | --- |
| `admin` | administrator | `Hospital@123` |
| `drmehta`, `drnair` | doctor | `Hospital@123` |
| `nurse1` | nurse | `Hospital@123` |
| `reception1` | receptionist | `Hospital@123` |
| `lab1` | laboratory | `Hospital@123` |
| `pharma1` | pharmacist | `Hospital@123` |
| `billing1` | billing | `Hospital@123` |
| `pat-000001` | patient | `Hospital@123` |

All data is synthetic. Change these before any shared deployment.

Ingest more documents from the **Documents** page, or via the API:

```powershell
curl.exe -X POST "http://localhost:8000/api/documents" -H "Authorization: Bearer <token>" -F "file=@data/seed/faqs/visiting_hours.txt" -F "title=Hospital Visiting Hours" -F "corpus=operational_faq"
```

---

## 7. Startup

```powershell
uvicorn backend.main:app --reload --port 8000
```

```powershell
streamlit run streamlit_app/app.py
```

- API: `http://localhost:8000` (interactive docs at `/docs`)
- UI: `http://localhost:8501`

Docker Compose runs PostgreSQL, migrations + seed, the API and the UI together:

```powershell
docker compose up --build
```

### One-command PowerShell tasks

A built-in task runner is available at `scripts/tasks.ps1`.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task help
powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task bootstrap -UseSqlite
powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task test
powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task run -UseSqlite
powershell -ExecutionPolicy Bypass -File .\scripts\tasks.ps1 -Task stop
```

Useful tasks:
- `doctor`: prints Python/dependency checks and key env-variable presence
- `setup`: creates `.venv`, installs dependencies, ensures `.env`
- `bootstrap`: setup + seed data
- `test-core` / `test-ui` / `test`: split or full test runs
- `eval`: runs `evaluation.run_eval` (`-WithModelEval` to call configured providers)
- `docker-up` / `docker-down`: compose wrappers

---

## 8. Testing and evaluation

```powershell
python -m pytest tests -q
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m pytest tests --cov=backend --cov=rag
```

```powershell
python -m evaluation.run_eval
python -m evaluation.run_eval --with-model
```

### Test coverage map

| Requirement | Test module |
| --- | --- |
| CRUD and relational integrity | `tests/unit/test_crud.py` |
| Concurrent appointments and bed allocation | `tests/unit/test_concurrency.py` |
| Authentication and role permissions | `tests/unit/test_auth_rbac.py` |
| Prompt injection, uploads, budgets, rate limits | `tests/unit/test_guardrails.py` |
| Retrieval relevance and citation accuracy | `tests/unit/test_rag.py` |
| Gemini / Groq adapter compatibility | `tests/unit/test_providers.py` |
| Tool authorization, confirmation, audit logging | `tests/integration/test_copilot.py` |
| Human approval workflow | `tests/integration/test_approvals.py` |
| API contract, RBAC, error shapes | `tests/integration/test_api.py` |
| Unsupported questions and abstention | `tests/integration/test_evaluation.py` |
| Every UI page renders without exception | `tests/integration/test_streamlit_ui.py` |

The UI suite drives all 13 Streamlit pages through `streamlit.testing.AppTest`,
both signed out (guard shows a message and stops) and signed in as an
administrator, so no page can silently break.

### Test isolation

`tests/conftest.py` **forces** an isolated SQLite database and ignores any
`DATABASE_URL` in your shell, because the suite drops and recreates every table.
Override deliberately with `HOSPITAL_TEST_DATABASE_URL` if you need to test
against PostgreSQL.

---

## 9. AI copilot and the workflow

### Workflow artifacts

- `hospital-copilot-workflow-export.json` - node/edge graph, id `smart-hospital-mvp-v1`
- `hospital-copilot-workflow.yaml` - parsable YAML twin
- `backend/copilot/graph.py` - the **executable** LangGraph `StateGraph`

Node names are identical across all three. `tests/integration/test_copilot.py::test_graph_node_names_match_workflow_export`
fails the build if the executable graph and the exported workflow drift apart.

### Execution path

```
workflow_start -> auth_rbac_gate -> input_guardrail_scan -> intent_router
  -> policy_resource_loader -> hospital_tool_registry -> hybrid_retrieval_loop
  -> evidence_sufficiency_branch -> token_budget_check -> provider_selector
  -> grounded_answer_generation -> citation_validator
      |- validated     -> answer_preview -> feedback_capture -> audit_persist -> complete_end
      |- needs_repair  -> conservative_rewrite (max 2) -> citation_validator
      \- unverified    -> escalation_router -> audit_persist -> escalation_end
```

The **Execution trace** expander on every chat answer shows the real nodes
visited, provider used, repair attempts, token spend and validation report.

### Component renaming vs the original triage workflow

| Original node | Hospital node |
| --- | --- |
| `start` / `operator-input` | `workflow-start` / `staff-intake-form` |
| `set-runtime-context` / `intake` | `set-session-context` / `request-validation` |
| `severity-switch` | `intent-router` |
| `mcp-resources` / `mcp-tools` | `policy-resource-loader` / `hospital-tool-registry` |
| `http-tool` / `http-request` | `hms-api-tool` / `hms-service-call` |
| `retrieval-repeat` | `hybrid-retrieval-loop` |
| `model-select` / `triage-generation` | `provider-selector` / `grounded-answer-generation` |
| `self-check` / `validation-branch` | `citation-validator` / `evidence-sufficiency-branch` |
| `repair-task` / `repair-counter` / `repair-switch` / `repair-llm` | `human-approval-task` / `retry-counter` / `retry-router` / `conservative-rewrite` |
| `workflow-preview` / `workflow-image` | `answer-preview` / `bed-occupancy-visual` |
| `feedback` / `persist-feedback` | `feedback-capture` / `audit-persist` |

**Nodes added** (absent from the original): `auth-rbac-gate`, `input-guardrail-scan`,
`clinical-boundary-block`, `token-budget-check`, `escalation-router`,
`emergency-escalation`, `escalation-end`, `guardrail-block-end`, `budget-block-end`.

### Copilot tools (all read-only)

`search_hospital_faq`, `get_appointment_availability`, `get_patient_appointments`,
`get_bed_availability`, `get_invoice_summary`, `get_claim_status`,
`validate_retrieved_evidence`.

Every invocation checks the caller's permission and writes an audit event;
unauthorized calls record `tool_call_denied` and raise `403`.

### Token optimisation

| Technique | Where |
| --- | --- |
| Deterministic keyword routing instead of LLM classification | `intent_router`, `escalation_matrix.classify_topic` |
| Business rules in Python, never the LLM | all services |
| Context capped at `MAX_CONTEXT_CHARS` | `token_budget_check` |
| Chat history bounded to `MAX_HISTORY_TURNS` | `guardrails.bound_history` |
| Near-duplicate passages dropped before prompting | `retriever._deduplicate` |
| Content-hash deduplication at ingestion | `rag/ingest.py` |
| Per-session budget enforced before any call | `guardrails.TokenBudget` |
| Abstention short-circuit - weak evidence never reaches a model | `evidence_sufficiency_branch` |

---

## 10. Guardrails and human interaction

### Narrow human-in-the-loop scope

| Gated action | Approver roles | Flow |
| --- | --- | --- |
| `discharge` | doctor, admin | request -> approve -> execute |
| `claim_decision_support` | billing, admin | request -> approve -> apply |
| `pharmacy_dispense` | pharmacist, admin | request -> approve -> dispense |

Rules enforced in `backend/services/approval_service.py`:

- Step 2 refuses to run without an approved ticket (`ApprovalRequiredError`).
- The requester cannot approve their own request (four-eyes).
- Rejection requires a written reason.
- A decision cannot be reversed or repeated.
- Wrong-role approval attempts are rejected and audited.

Low-risk mutations (booking, reschedule, cancel, check-in, transfer, invoicing,
payments) execute immediately and are audited - approval fatigue is a real risk.

Use the **Approvals** page to review, approve or reject pending requests.

### Layered guardrails

| Layer | Implementation |
| --- | --- |
| Authentication | PBKDF2-SHA256 passwords, HMAC-signed expiring tokens |
| Authorization | 24-permission role matrix, enforced in services |
| Patient scoping | `enforce_patient_scope` - patients see only their own record |
| Input screening | Injection, PII, emergency phrases, length caps |
| Clinical boundary | Diagnosis / medication / claim-approval requests refused outright |
| Upload validation | Extension, size, MIME-vs-extension, path traversal |
| Document sanitization | Embedded instructions neutralised at ingestion time |
| Evidence requirement | No citation -> no answer |
| Citation validation | Per-sentence marker + term-overlap grounding |
| Output validation | Pydantic schemas on every request and response |
| Token budget | Per-session, checked before any provider call |
| Rate limiting | Per user and endpoint |
| Retries / fallback | Bounded retries, then the next provider in the chain |
| Error safety | No stack traces, secrets or internal prompts reach users |
| Audit trail | Actor, role, action, outcome, trace id - PHI scrubbed |

### Corpus separation

Operational FAQ, insurance policy and clinical information are stored and
retrieved as **separate corpora**, so an operational question can never be
answered from clinical text.

### What the system will never do

Diagnose a patient, prescribe or change medication, approve an insurance claim,
discharge a patient, dispense medication, or state a hospital policy it cannot cite.

### Emergency handling

Emergency phrases short-circuit the entire pipeline and return the 24x7
emergency contact before any retrieval or model call.

### Privacy, consent and retention

- Treatment consent is mandatory at registration; data-sharing consent is separate and timestamped.
- `patient_service.deactivate_patient` is the erasure/retention hook.
- Audit details are scrubbed of numbers, emails and dates before storage.
- All development and test data is synthetic.

---

## 11. Limitations and production hardening

### Known limitations

1. **Embeddings** default to a deterministic hashing embedder so the project runs
   offline. Install the `embeddings` extra for real semantic similarity.
2. **Vector search is in-process.** Embeddings are JSON columns scanned in Python -
   fine for thousands of chunks, not for millions.
3. **The abstention threshold is corpus-calibrated.** `RELEVANCE_THRESHOLD=0.25`
   sits inside a 0.28-wide margin measured on `evaluation/dataset.json`. A much
   larger or very different corpus should be re-calibrated by re-running the
   evaluation; `test_configured_threshold_sits_inside_the_margin` guards this.
4. **Stemming is suffix-stripping, not linguistic.** It handles the inflections
   that appear in hospital policy text, not irregular forms.
5. **Streaming is simulated.** The validated answer is chunked to the UI, because
   streaming unvalidated text would show uncited claims. Provider-level streaming
   exists in the adapters and is used for the raw call.
6. **OCR is a stub** unless tesseract is installed locally.
7. **Sessions are in-process.** Token budgets and rate limits live in memory and
   reset on restart; they are per-process, not per-cluster.
8. **The baseline migration uses `metadata.create_all`** to guarantee ORM/migration
   parity. Later revisions should be autogenerated with explicit `op` calls.
9. **Approval tickets have no expiry.** An approved discharge stays usable until consumed.
10. **No insurer integration.** Claim status is internal bookkeeping only.
11. **Audit scrubbing is pattern-based**, not a certified de-identification pipeline.

### Production hardening checklist

- [ ] Move vectors to **pgvector** with an HNSW index; keep the same retriever interface
- [ ] Add a cross-encoder reranker behind a feature flag
- [ ] Replace in-memory budgets/limits with **Redis**
- [ ] Rotate `SECRET_KEY`, enforce password policy, add MFA for admin/doctor roles
- [ ] Terminate TLS everywhere; enable PostgreSQL encryption at rest and in transit
- [ ] Move secrets to a managed vault (no `.env` in production)
- [ ] Ship structured logs to a SIEM with alerting on `permission_denied` and `tool_call_denied`
- [ ] Add approval ticket expiry and mandatory re-request after N hours
- [ ] Implement retention jobs for the consent/erasure workflow
- [ ] Add backup, PITR and restore drills
- [ ] Load test concurrent booking and bed allocation under real contention
- [ ] Add a canary evaluation run in CI that fails the build on metric regression
- [ ] Formal clinical safety review and sign-off before any patient-facing deployment
- [ ] Penetration test focused on prompt injection, RBAC bypass and cross-patient access

---

## 12. Repository map

| Path | Purpose |
| --- | --- |
| `streamlit_app/` | Multipage UI (13 pages) |
| `backend/` | FastAPI app, services, guardrails, AI adapters, copilot |
| `rag/` | Ingestion, hybrid retrieval, citations, escalation matrix |
| `alembic/` | Migration environment and baseline revision |
| `data/seed/` | Synthetic seed data and sample FAQ documents |
| `evaluation/` | Evaluation dataset and harness |
| `tests/` | 184 unit and integration tests |
| `docs/API.md` | Full API reference with examples |
| `hospital-copilot-workflow-export.json` | Workflow export (`smart-hospital-mvp-v1`) |
| `hospital-copilot-workflow.yaml` | Parsable YAML twin |
| `.env.example` | Placeholder configuration |
| `Dockerfile`, `docker-compose.yml` | Container deployment |
| `.streamlit/config.toml` | UI theme and server settings |

---

## 13. Quick start

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
$env:DATABASE_URL = "sqlite:///./dev_hospital.db"
python -m data.seed.seed_data
python -m pytest tests -q
python -m evaluation.run_eval
streamlit run streamlit_app/app.py
```

Sign in as `admin` / `Hospital@123`, open **FAQ Assistant** and ask
*"What are the ICU visiting hours?"*. Without a provider key the assistant
retrieves the evidence and escalates to the right desk instead of guessing -
which is the intended behaviour.
