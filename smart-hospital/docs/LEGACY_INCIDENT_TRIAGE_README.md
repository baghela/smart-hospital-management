# AI-assisted Incident Triage (Local JSON + REST API)

This project provides a workflow-driven incident triage assistant that reads local data, exposes API/CLI entry points, routes LLM calls through a configurable gateway, and stores operator feedback.

## What this project does

- Reads historical incidents, KB articles, and recent logs from `data/`
- Supports incident triage through CLI (`src.app.Main`) and REST API (`POST /triage`)
- Uses explicit staged reasoning in the service layer: classify, retrieve, compare, draft, validate
- Returns a standard triage response contract including `probableCause`, `confidence`, `evidence`, and `nextSteps`
- Captures feedback through `POST /feedback` and updates acceptance-rate signal
- Routes AI traffic through Bodhi-compatible `base_url` + API key configuration

## Project structure

- `src/src/app/Main.java` - CLI triage runner
- `src/src/app/ApiServerMain.java` - REST API bootstrap
- `src/src/http/TriageHttpServer.java` - `/health`, `/triage`, `/feedback` handlers and request/response contracts
- `src/src/service/IncidentTriageService.java` - staged orchestration flow
- `src/src/service/FeedbackStore.java` - feedback persistence and acceptance-rate signal
- `src/src/repository/JsonFileIncidentRepository.java` - local JSON ingestion
- `src/src/ai/PlaceholderAiClient.java` - gateway-routed chat completions client with retries/fallbacks
- `src/src/ai/CloudAiPlatformClient.java` - Cloud AI Platform (Azure OpenAI-style) deployment routing client
- `src/src/observability/TriageMetrics.java` - runtime counters/timers surfaced at `GET /metrics`
- `src/src/model/Severity.java` - enum-like severity validation for REST and CLI
- `src/src/test/TriageServiceTest.java` - executable smoke test
- `src/src/test/ComprehensiveQualityTest.java` - API contract, validation, retry, and trace propagation checks
- `src/src/test/FrameworkCapabilitiesTest.java` - tool registry, session memory, and workflow branching checks
- `src/src/test/ProductionReadinessTest.java` - severity validation, cloud AI routing, and metrics checks
- `src/src/tool/` - explicit callable tools (`Tool`, `ToolRegistry`, retrieval tools)
- `src/src/memory/` - session state and in-memory session store
- `src/src/workflow/` - config-driven workflow engine (`WorkflowEngine`, `WorkflowContext`)
- `incident-triage-workflow.yaml` - parseable workflow artifact
- `workflow-builder-export.json` - workflow builder style export (nodes/edges graph)

## Compile and run

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
$javaFiles = Get-ChildItem -Path "src\src" -Filter "*.java" -Recurse | ForEach-Object { $_.FullName }
javac $javaFiles
```

Run CLI demo:

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.app.Main
```

Run API server:

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.app.ApiServerMain
```

Run smoke test:

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.test.TriageServiceTest
```

Run comprehensive quality checks:

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.test.ComprehensiveQualityTest
```

Run framework capability checks (tools, memory, workflow):

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.test.FrameworkCapabilitiesTest
```

Run the interactive CLI (prompts for incident fields):

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.app.Main --interactive
```

## Bodhi gateway configuration

Set environment variables before running CLI/API:

```powershell
$env:BODHI_BASE_URL = "https://your-bodhi-gateway/v1"
$env:BODHI_API_KEY = "<your_gateway_api_key>"
$env:BODHI_MODEL = "gpt-4o-mini"
```

Backward-compatible fallbacks are still supported:

- `AI_API_ENDPOINT` (used when `BODHI_BASE_URL` is missing)
- `AI_API_TOKEN` (used when `BODHI_API_KEY` is missing)
- `AI_MODEL` (used when `BODHI_MODEL` is missing)

If no API key is set, the assistant returns baseline rule-based triage with an AI-disabled evidence note.

Concrete gateway request shape used by `PlaceholderAiClient`:

```http
POST {BODHI_BASE_URL}/chat/completions
Authorization: Bearer $env:BODHI_API_KEY
Content-Type: application/json
```

```json
{
  "model": "gpt-4o-mini",
  "temperature": 0.2,
  "response_format": { "type": "json_object" },
  "messages": [
    { "role": "system", "content": "You are an SRE incident triage assistant..." },
    { "role": "user", "content": "Incident context: { ... }" }
  ]
}
```

Example successful response:

```json
{
  "choices": [
    {
      "message": {
        "content": "{\"rootCauses\":[\"checkout deployment regression\"],\"actions\":[\"rollback the release\"],\"evidence\":[\"matched historical incident INC-102\"]}"
      }
    }
  ]
}
```

## Runtime tracing

- Incoming API calls accept optional `X-Trace-Id`; if absent, one is generated.
- The trace id is echoed in responses and propagated through service stages and AI gateway calls.
- AI calls include both `X-Trace-Id` and `X-Bodhi-Trace-Id` headers.

## Framework capabilities

- Tool Integration: retrieval sources are exposed as explicit callable tools via
  `ToolRegistry` (`historical_incidents`, `knowledge_articles`, `recent_logs`)
  with structured input/output maps.
- Memory & State: `SessionStore` keeps per-session `SessionState` (stage audit,
  last recommendation, reusable context). Pass `sessionId` in the `/triage` body
  to reuse a session; the response echoes `sessionId` and `stageAudit`.
- Workflow Engine: `WorkflowEngine` parses `workflow-builder-export.json`
  (nodes/edges) and evaluates branch conditions (missing fields, low confidence,
  data unavailable), making orchestration config-driven rather than hardcoded.

## API examples

```powershell
Invoke-RestMethod -Uri "http://localhost:8080/health" -Method Get
```

```powershell
$triageBody = @'
{
  "id": "INC-2001",
  "service": "payments-api",
  "severity": "SEV1",
  "title": "Checkout failing with 503",
  "description": "Timeouts and SQL slowness after deployment",
  "errorCode": "HTTP_503"
}
'@
Invoke-RestMethod -Uri "http://localhost:8080/triage" -Method Post -ContentType "application/json" -Body $triageBody
```

```powershell
$feedbackBody = @'
{
  "recommendationId": "<paste_recommendation_id_here>",
  "accepted": true,
  "notes": "Rollback and index fix resolved issue"
}
'@
Invoke-RestMethod -Uri "http://localhost:8080/feedback" -Method Post -ContentType "application/json" -Body $feedbackBody
```

```powershell
Invoke-RestMethod -Uri "http://localhost:8080/metrics" -Method Get
```

## Runtime metrics

`GET /metrics` returns live counters from `TriageMetrics`:

- `triageCount`, `lowConfidenceCount`, `datasourceFallbackCount`
- `verificationRepairCount`
- `modelCallCount`, `modelRetryCount`, `modelFailureCount`, `averageModelLatencyMs`
- `feedbackAcceptanceRate`

## Output verification loop

`IncidentTriageService.verifyRecommendation(...)` runs after generation. It checks
`probableCause`, `confidence`, `evidence`, and `nextSteps` and verifies evidence
grounding. If a field is missing or unsupported by retrieved data, it applies a
conservative repair (safe next steps + reduced confidence) and increments
`verificationRepairCount`.

## Input validation (REST + CLI)

- `/triage` and `/feedback` validate required fields and reject malformed payloads with `400`.
- Severity is validated against `Severity` (SEV1..SEV5) in both `TriageHttpServer` and `Main`.

## Cloud AI Platform

`CloudAiPlatformClient` demonstrates Cloud AI Platform routing using an Azure
OpenAI-style deployment path and `api-key` auth. It is selected automatically
when the following environment variables are present:

```powershell
$env:CLOUD_AI_ENDPOINT   = "https://<your-resource>.openai.azure.com"
$env:CLOUD_AI_DEPLOYMENT = "triage-gpt"
$env:CLOUD_AI_API_VERSION= "2024-02-15-preview"
$env:CLOUD_AI_REGION     = "eastus"
$env:CLOUD_AI_API_KEY    = "<your_cloud_api_key>"
```

Resolved URL pattern:
`{CLOUD_AI_ENDPOINT}/openai/deployments/{CLOUD_AI_DEPLOYMENT}/chat/completions?api-version={CLOUD_AI_API_VERSION}`

Cloud calls record latency and retries into `/metrics` and are traced with
`X-Trace-Id` and `X-Cloud-Region` headers.

## Priority fixes mapping (feedback -> code evidence)

- Workflow builder flow -> `workflow-builder-export.json` (start `intake`, terminals
  `end` and `needs-input-end`, branches for missing fields / datasource fallback /
  low-confidence / verification) executed by `WorkflowEngine`.
- Gateway client wiring -> `PlaceholderAiClient` sends real chat completions with
  retries/fallbacks; call path `IncidentTriageService.draftRecommendation` -> `AiClient.suggest`.
- Structured request validation -> `TriageHttpServer.TriageRequest.validate()` +
  `Severity` enum checks; mirrored in `Main.validateIncident`.
- Output verification loop -> `IncidentTriageService.verifyRecommendation` with
  conservative repair.
- Runtime tracing and metrics -> trace ids across `TriageHttpServer`,
  `IncidentTriageService`, `PlaceholderAiClient`, `CloudAiPlatformClient` + `/metrics`.

## Run all tests

```powershell
Set-Location "C:\Users\abaghe12\Optum\GEN-AI-L2"
java -cp src src.test.TriageServiceTest
java -cp src src.test.ComprehensiveQualityTest
java -cp src src.test.FrameworkCapabilitiesTest
java -cp src src.test.ProductionReadinessTest
```

```powershell
java -cp src src.test.ReasoningAndStateTest
```

## Topic proficiency -> code evidence

| Topic | Evidence (file : symbol) | Test |
| --- | --- | --- |
| User Experience | `TriageHttpServer.errorPayload` (structured errors), response `planSteps`/`evidenceGrounding`; `Main` prints plan + grounding; `Main --interactive` | ComprehensiveQualityTest, ReasoningAndStateTest |
| Agent Orchestration | `WorkflowEngine.run`; `IncidentTriageService.triage` executable needs-input routing (`workflow:route=needs-input`) | FrameworkCapabilitiesTest, ReasoningAndStateTest |
| Tool Integration | `ToolRegistry` + `HistoricalIncidentsTool`/`KnowledgeArticlesTool`/`RecentLogsTool`; `IncidentTriageService.invokeRecentLogsTool` | FrameworkCapabilitiesTest |
| Memory & State | `SessionStore.persist` (JSONL snapshots at `data/sessions.jsonl`), `SessionState.stageAudit` | ReasoningAndStateTest |
| Reasoning & Planning | `IncidentTriageService.buildPlan` (explicit plan), `verifyRecommendation` (post-generation check) | ReasoningAndStateTest |
| Retrieval & Knowledge | `scoreHistoricalIncidents`, `matchKnowledge`, `scoringRationale`, `enrichWithPlanAndGrounding` (evidence grounding) | ReasoningAndStateTest |
| Framework Depth | `WorkflowEngine` graph execution + `ToolRegistry` orchestration + staged pipeline | FrameworkCapabilitiesTest |
| Bodhi Gateway Integration | `PlaceholderAiClient` live chat completions with retries; call path `draftRecommendation -> AiClient.suggest` | ComprehensiveQualityTest |
| Reliability & Safety | DTO + `Severity` validation, conservative repair, tracing, `TriageMetrics` + `/metrics` | ProductionReadinessTest |
| Workflow Builder Mastery | `workflow-builder-export.json` (start `intake`, terminals `end`/`needs-input-end`, verification node) executed by engine | FrameworkCapabilitiesTest |
| Cloud AI Platform | `CloudAiPlatformClient` (Azure-style deployment routing) | ProductionReadinessTest |

## Triage response fields

In addition to `probableCause`, `confidence`, `evidence`, `nextSteps`, the
`/triage` response now also returns:

- `planSteps` - the ordered plan the agent followed
- `evidenceGrounding` - concrete retrieval signals (incident scores + KB ids) supporting the recommendation
- `sessionId`, `stageAudit` - session memory continuity


