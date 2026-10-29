# API Documentation

Base URL: `http://localhost:8000`
All business endpoints are mounted under `/api`. Interactive docs: `http://localhost:8000/docs`.

## Conventions

| Topic | Behaviour |
| --- | --- |
| Auth | `Authorization: Bearer <token>` from `POST /api/auth/login` |
| Tracing | Send `X-Trace-Id`; it is echoed back and written to the audit trail |
| Errors | `{ "error": "...", "code": "...", "trace_id": "..." }` — never a stack trace |
| Status codes | `400` domain, `401` auth, `403` RBAC, `404` missing, `409` conflict, `422` validation, `503` provider |
| Approval gate | Gated actions return `202` with an `approval_id` instead of executing |

### Error codes

`unauthenticated`, `permission_denied`, `not_found`, `validation_error`, `conflict`,
`guardrail_blocked`, `token_budget_exceeded`, `rate_limited`, `approval_required`,
`provider_unavailable`, `internal_error`.

---

## Health

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get
Invoke-RestMethod -Uri "http://localhost:8000/health/db" -Method Get
```

---

## Authentication

### `POST /api/auth/login`

```powershell
$login = @{ username = "admin"; password = "Hospital@123" } | ConvertTo-Json
$auth = Invoke-RestMethod -Uri "http://localhost:8000/api/auth/login" -Method Post -ContentType "application/json" -Body $login
$headers = @{ Authorization = "Bearer $($auth.access_token)" }
```

Response:

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "username": "admin",
  "role": "admin",
  "permissions": ["appointment:read", "patient:write", "..."]
}
```

### `GET /api/auth/me`

Returns the caller's role and effective permission list.

---

## Patients

| Method | Path | Permission |
| --- | --- | --- |
| `POST` | `/api/patients` | `patient:write` |
| `GET` | `/api/patients?q=&page=&page_size=` | `patient:read` |
| `GET` | `/api/patients/{id}` | `patient:read` |
| `PATCH` | `/api/patients/{id}` | `patient:write` |

```powershell
$patient = @{
  full_name = "Aarav Sharma"
  date_of_birth = "1988-04-12"
  gender = "male"
  phone = "9800011001"
  email = "aarav@example.com"
  consent_treatment = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/patients" -Method Post -Headers $headers -ContentType "application/json" -Body $patient
```

Patients authenticated with the `patient` role can only read their own record (`403` otherwise).

---

## Departments & doctors

| Method | Path | Permission |
| --- | --- | --- |
| `POST` | `/api/departments` | `doctor:write` |
| `GET` | `/api/departments` | `doctor:read` |
| `POST` | `/api/doctors` | `doctor:write` |
| `GET` | `/api/doctors?department_id=&q=` | `doctor:read` |

---

## Appointments

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/appointments/availability?doctor_id=&day=` | Free slots only |
| `POST` | `/api/appointments` | `409` on double booking |
| `GET` | `/api/appointments?patient_id=&doctor_id=&day=&status=` | Paginated |
| `POST` | `/api/appointments/{id}/reschedule` | `409` if the new slot is taken |
| `POST` | `/api/appointments/{id}/cancel` | Frees the slot for rebooking |
| `POST` | `/api/appointments/{id}/check-in` | Assigns a queue number |
| `POST` | `/api/appointments/{id}/complete` | Clinician-signed encounter |
| `GET` | `/api/appointments/queue/{doctor_id}` | Today's checked-in queue |

```powershell
$booking = @{
  patient_id = 1
  doctor_id = 1
  slot_start = "2026-10-21T10:00:00"
  reason = "Routine consultation"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/appointments" -Method Post -Headers $headers -ContentType "application/json" -Body $booking
```

Conflict response:

```json
{ "error": "That slot has just been booked by someone else.", "code": "conflict", "trace_id": "a1b2c3" }
```

---

## Beds & admissions

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/wards` | Creates a ward and its beds |
| `GET` | `/api/beds/board` | Interactive bed board payload |
| `GET` | `/api/beds/availability` | Per-ward availability summary |
| `POST` | `/api/admissions` | `409` if the bed is taken |
| `GET` | `/api/admissions?status=` | Active / discharged |
| `POST` | `/api/admissions/{id}/transfer` | Releases the old bed atomically |
| `POST` | `/api/admissions/{id}/discharge-request` | **Step 1** → `202` + `approval_id` |
| `POST` | `/api/admissions/{id}/discharge` | **Step 2** — needs an approved ticket |
| `GET` | `/api/admissions/{id}/summary` | Data-derived discharge summary |
| `POST` | `/api/admissions/{id}/follow-up?doctor_id=&days_ahead=` | Books the first free slot |

### Gated discharge (two-step)

```powershell
# Step 1 - raise the approval ticket
$discharge = @{ summary = "Patient stable, vitals normal, discharged with oral medication." } | ConvertTo-Json
$ticket = Invoke-RestMethod -Uri "http://localhost:8000/api/admissions/1/discharge-request" -Method Post -Headers $headers -ContentType "application/json" -Body $discharge

# Step 2 - a doctor approves, then the discharge executes
$decision = @{ approved = $true; notes = "Reviewed and agreed" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/api/approvals/$($ticket.approval_id)/decision" -Method Post -Headers $doctorHeaders -ContentType "application/json" -Body $decision
Invoke-RestMethod -Uri "http://localhost:8000/api/admissions/1/discharge" -Method Post -Headers $headers
```

Calling step 2 without approval:

```json
{ "error": "Discharge requires an approved clinician sign-off before it can run.", "code": "approval_required" }
```

---

## Billing

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/invoices` | Creates a draft |
| `GET` | `/api/invoices?patient_id=&status=` | |
| `GET` | `/api/invoices/{id}` | Itemised summary |
| `POST` | `/api/invoices/{id}/items` | Recalculates subtotal/tax/total |
| `POST` | `/api/invoices/{id}/issue` | Draft → issued |
| `POST` | `/api/invoices/{id}/payments` | Returns a receipt |
| `POST` | `/api/invoices/{id}/refunds` | Bounded by the amount paid |

```powershell
$item = @{ description = "Consultation - General Medicine"; unit_price = 600; quantity = 1; category = "consultation" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/api/invoices/1/items" -Method Post -Headers $headers -ContentType "application/json" -Body $item
```

All totals are computed in Python (`backend/services/billing_service.py`), never by an LLM.

---

## Insurance

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/policies` | |
| `GET` | `/api/policies?patient_id=` | |
| `POST` | `/api/claims` | Deterministic eligibility check |
| `GET` | `/api/claims?status=` | |
| `POST` | `/api/claims/{id}/decision-request` | **Step 1** → `202` + `approval_id` |
| `POST` | `/api/claims/{id}/apply-decision` | **Step 2** — needs billing sign-off |

---

## Laboratory & pharmacy

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/lab/catalog` | Test codes and prices |
| `POST` | `/api/lab/orders` | |
| `GET` | `/api/lab/orders?patient_id=&status=` | |
| `POST` | `/api/lab/orders/{id}/collect` | Issues a specimen id |
| `POST` | `/api/lab/orders/{id}/start` | |
| `POST` | `/api/lab/orders/{id}/result` | Lab staff only |
| `POST` | `/api/pharmacy/items` | Upsert inventory |
| `GET` | `/api/pharmacy/items?low_stock_only=` | |
| `POST` | `/api/pharmacy/prescriptions` | Doctors only |
| `POST` | `/api/pharmacy/prescriptions/{id}/dispense-request` | **Step 1** → `202` |
| `POST` | `/api/pharmacy/prescriptions/{id}/dispense` | **Step 2** — decrements stock |

Lab state transitions are validated: `ordered → collected → in_progress → resulted`.

---

## Approvals (human-in-the-loop)

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/approvals/policy` | The three gated actions and their approver roles |
| `GET` | `/api/approvals?status=pending` | |
| `GET` | `/api/approvals/{id}` | |
| `POST` | `/api/approvals/{id}/decision` | Four-eyes rule enforced |

```json
{
  "gated_actions": {
    "discharge": "Patient discharge requires clinician sign-off.",
    "claim_decision_support": "Insurance claim decisions require billing sign-off.",
    "pharmacy_dispense": "Medication dispensing requires pharmacist sign-off."
  },
  "note": "Low-risk mutations (booking, reschedule, cancel, transfer) execute immediately and are audited."
}
```

---

## Copilot & RAG

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/copilot/ask` | Full guarded LangGraph run |
| `POST` | `/api/copilot/ask/stream` | Streams the **validated** answer |
| `GET` | `/api/copilot/tools` | The seven read-only tools |
| `POST` | `/api/copilot/feedback` | 👍 / 👎 with optional notes |
| `GET` | `/api/copilot/escalation-matrix` | Role/topic contact matrix |

```powershell
$ask = @{ question = "What are the ICU visiting hours?"; session_id = "demo" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/api/copilot/ask" -Method Post -Headers $headers -ContentType "application/json" -Body $ask
```

Grounded response:

```json
{
  "answer": "ICU visiting is limited to 17:00 to 18:00 daily and to one attendant per patient [S1].",
  "status": "answered",
  "sources": [
    {
      "marker": "S1",
      "title": "Hospital Visiting Hours",
      "section": "Intensive Care Visiting",
      "page": 1,
      "version": "1.0",
      "score": 0.83,
      "preview": "ICU visiting is limited to 17:00 to 18:00 daily..."
    }
  ],
  "validation": { "valid": true, "cited_sources": [1], "coverage": 1.0 },
  "provider": "groq",
  "model": "gpt-4o-mini",
  "corpus": "operational_faq",
  "tokens_used": 412,
  "elapsed_ms": 884,
  "repair_attempts": 0,
  "node_trace": [{ "node": "workflow_start" }, { "node": "auth_rbac_gate" }],
  "trace_id": "a1b2c3d4e5f6",
  "engine": "langgraph"
}
```

Abstention response (insufficient evidence):

```json
{
  "answer": "I could not verify this from the approved hospital documents, so I will not guess.\n\nPlease contact the **Insurance & TPA Desk** - phone +91-80-4000-1030...\nReference: `ESC-INS-48213`",
  "status": "escalated",
  "sources": [],
  "escalation": {
    "topic": "insurance",
    "desk": "Insurance & TPA Desk",
    "contact": "+91-80-4000-1030",
    "email": "insurance@smarthospital.example",
    "hours": "09:00-17:00 IST",
    "reference": "ESC-INS-48213"
  }
}
```

Blocked response (prompt injection):

```json
{
  "answer": "That request looks like an attempt to override system rules, so it was blocked.",
  "status": "blocked",
  "sources": []
}
```

### Answer statuses

| Status | Meaning |
| --- | --- |
| `answered` | Validated and fully cited |
| `answered_repaired` | Unsupported sentences stripped, remainder validated |
| `escalated` | Insufficient evidence → role-mapped contact returned |
| `abstained` | Model returned `INSUFFICIENT_EVIDENCE` |
| `unverified` | Repairs exhausted without valid grounding |
| `blocked` | Prompt injection / unsafe request |
| `clinical` | Diagnosis, medication or claim-approval request refused |
| `emergency` | Emergency phrase detected |
| `budget_exceeded` | Session token budget reached |
| `no_provider` | No AI provider configured |
| `provider_error` | All providers failed |

---

## Documents

| Method | Path | Permission |
| --- | --- | --- |
| `POST` | `/api/documents` (multipart) | `faq:ingest` |
| `GET` | `/api/documents?corpus=` | authenticated |
| `POST` | `/api/documents/{id}/reindex` | `faq:ingest` |
| `POST` | `/api/documents/{id}/status?status=` | `faq:ingest` |
| `DELETE` | `/api/documents/{id}` | `faq:delete` |

```powershell
curl.exe -X POST "http://localhost:8000/api/documents" `
  -H "Authorization: Bearer $($auth.access_token)" `
  -F "file=@data/seed/faqs/visiting_hours.txt" `
  -F "title=Hospital Visiting Hours" `
  -F "category=visiting" `
  -F "corpus=operational_faq" `
  -F "version=1.0"
```

Corpora are isolated: `operational_faq`, `insurance_policy`, `clinical_information`.

---

## Admin

| Method | Path | Permission |
| --- | --- | --- |
| `GET` | `/api/admin/kpis` | authenticated |
| `GET` | `/api/admin/reports/appointments-trend?days=` | `report:read` |
| `GET` | `/api/admin/reports/revenue` | `report:read` |
| `GET` | `/api/admin/reports/department-load` | `report:read` |
| `GET` | `/api/admin/reports/faq-feedback` | `report:read` |
| `GET` | `/api/admin/providers` | `admin:config` |
| `GET` | `/api/admin/audit?limit=` | `audit:read` |

`GET /api/admin/providers` returns configuration state and capability flags but
never returns API keys.

