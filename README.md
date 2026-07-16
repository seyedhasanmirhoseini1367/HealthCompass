# HealthCompass

A Django web application that helps patients understand their own health records through an AI-powered chat interface. Upload lab results, prescriptions, wearable data, and clinical notes — then ask questions in plain language and get answers grounded in your actual records.

---

## Overview

HealthCompass is built around a single idea: your health data should be understandable to you, not just to clinicians. The platform lets patients upload medical documents in multiple formats, automatically indexes them into a searchable vector store, and exposes a streaming chat interface backed by a multi-LLM RAG pipeline.

**Key capabilities:**

- Multi-role user system (Patient, Doctor, Data Scientist, Hospital Admin)
- Medical record management with 8 record types and 3 import sources
- Wearable data ingestion (CSV from Fitbit, Garmin, Apple Watch, Oura)
- Lab value extraction with abnormal/critical flagging
- AI Health Assistant with streaming responses and cited sources
- Hybrid RAG pipeline — keyword + semantic routing, BM25 + dense retrieval, streaming multi-LLM generation with safety guardrails

---

## Application Modules

| Module | Purpose |
|---|---|
| `accounts` | Custom user model with role-based profiles (Patient, Doctor, Data Scientist, Hospital Admin) |
| `medical_records` | Upload and manage records; parse lab values; ingest wearable CSV |
| `rag_assistant` | Vector store, chat sessions, AI chat interface |
| `ai_insights` | AI-generated health summaries and trend analysis |
| `dashboard` | Patient dashboard with health overview |
| `integrations` | Third-party data source integrations |
| `notifications` | In-app notification system |

### Record types supported

`Lab Result` · `Prescription` · `Diagnosis` · `Vaccination` · `Imaging Report` · `Wearable Data` · `Discharge Summary` · `Other`

### Import sources

`Manual Upload` · `Kanta XML Import` · `Wearable CSV Import`

---

## RAG System

The AI Health Assistant is built on a Retrieval-Augmented Generation pipeline that retrieves relevant chunks from the patient's own records before generating a response. No records from other patients are ever included.

### Architecture — single LangGraph pipeline

Both `stream_ask()` (streaming SSE) and `ask()` (sync) delegate to the same LangGraph-orchestrated pipeline:

```
User question
      │
      ▼
 safety_gate_node    ← pre-query emergency / self-harm check (no LLM call)
      │ safe
      ▼
 understand_node     ← QueryUnderstanding: mode / route / rewritten query
      │
      ▼
 router_node         ← cold-start gate (no indexed records → cold_start route)
      │
      ├── cold_start_node    → population reference ranges (no records indexed)
      ├── trajectory_node    → Δ(D,t,s) chronological context (trend queries)
      ├── lab_results_node   → lab result chunks
      ├── medications_node   → medication chunks
      ├── wearable_node      → wearable data chunks
      ├── diagnosis_node     → clinical note chunks
      ├── records_node       → all record types
      └── general_node       → Finnish clinical knowledge base (Käypä hoito / THL)
                │
                ▼
      generate_streaming()   ← Groq → Gemini → Anthropic → OpenAI
        (guardrail buffer: first 500 chars softened before streaming)
                │
                ▼
        Guardrail layer      ← post-generation disclaimers appended
                │
                ▼
      SSE tokens + sources + chart data → browser / mobile
                         (or assembled synchronously for ask())
```

`stream_ask()` uses a routing-only subgraph (no `generate_node`) that resolves retrieval state, then calls `generate_streaming()` directly for token-by-token SSE output. `ask()` consumes the same SSE stream and assembles the full response tuple synchronously.

### Routing

The router classifies each question with **word-boundary keyword matching** — no LLM call, no latency cost. For temporal queries ("trend", "over time", "is my creatinine improving"), a semantic embedding fallback (`gemini-embedding-001` cosine similarity against prototype phrases) catches paraphrases the keyword list misses.

| Route | Triggered by keywords |
|---|---|
| `trajectory` | trend, over time, improving, getting worse, journey, how has my… |
| `lab_results` | lab, blood, cholesterol, glucose, HbA1c, TSH, vitamin, abnormal… |
| `medications` | medication, prescription, dose, side effect, interaction, insulin… |
| `wearable` | heart rate, steps, sleep, bpm, SpO2, Fitbit, Garmin, HRV… |
| `diagnosis` | diagnosis, MRI, CT scan, X-ray, ultrasound, discharge, surgery… |
| `records` | record, history, timeline, previous, last time… |
| `general` | (fallback — retrieves across all types) |

### Retrieval

Hybrid retrieval combining four signals:

```
Score(q, D) = α·BM25(q,D) + β·cos(q,D) + γ·Δ(D,t,s) + δ·Context(D,i)
```

- **BM25 (α)** — keyword overlap (sparse). Weight adapts by query intent: 0.20 for temporal, 0.50 for medication, 0.35 default.
- **Cosine similarity (β)** — semantic match via `Gemini gemini-embedding-001` (3072-dim, stored as raw `float32` bytes in Postgres). Weight is `1 − α`.
- **Time decay (γ·Δ)** — records older than 1 year are down-weighted by up to 15%.
- **Context-type boost (δ)** — chunks whose document type aligns with the query intent get a small relevance bonus.
- **MMR re-ranking** — Maximal Marginal Relevance (λ=0.6) diversifies the final top-6 set.

### Generation

Four LLMs tried in order; first successful response is returned:

1. **Groq** (`llama-3.1-8b-instant`) — primary; generous free tier, fast
2. **Gemini** (`gemini-2.5-flash`) — first fallback, via `google-genai` v1.x
3. **Anthropic Claude** (`claude-haiku-4-5`) — second fallback
4. **OpenAI GPT** (`gpt-4o-mini`) — third fallback

Both streaming (SSE, token-by-token) and non-streaming modes are supported across all four providers.

### Safety layer

**Pre-query gate** — checks for emergency symptoms and self-harm language before any retrieval or LLM call fires. Returns a hard-coded crisis response immediately.

**Post-generation guardrails** — three rules applied to every LLM response:
- Specific dosage recommendation → medication disclaimer appended
- Definitive diagnosis statement (`you have diabetes`) → language softened + diagnostic disclaimer
- Emergency / alarming language → urgent care reminder appended

### Self-correction (LangGraph path only)

After generation, `verify_node` checks for empty context (no chunks retrieved). If true and fewer than 2 retries have been attempted, the graph loops back through `records_node` for a broader retrieval pass.

### Streaming

The chat UI connects to `/assistant/stream/` which returns a `text/event-stream` response. Events:

```
data: {"type": "token",   "content": "..."}        ← one per token
data: {"type": "sources", "sources": [...]}        ← after all tokens
data: {"type": "meta",    "provider": "groq", "chunks": 6, "mode": "personal"}
data: {"type": "chart",   "chart": {...}}           ← trajectory queries only
data: {"type": "done"}
data: {"type": "error",   "message": "..."}        ← on failure
```

Tokens are rendered incrementally with live Markdown parsing. The full response is saved to `QueryLog` in a `finally` block so chat history is always consistent.

### API endpoints

| Method | URL | Description |
|---|---|---|
| `GET` | `/assistant/` | Chat interface |
| `POST` | `/assistant/send/` | Non-streaming response (JSON) |
| `POST` | `/assistant/stream/` | Streaming SSE response |
| `GET` | `/assistant/session/<id>/history/` | Session message history |
| `POST` | `/assistant/session/<id>/rename/` | Rename a session |
| `POST` | `/assistant/session/<id>/delete/` | Delete a session |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5.2, Python 3.11+ |
| LLM orchestration | LangGraph 0.2, Groq SDK, google-genai, Anthropic SDK, OpenAI SDK |
| Embeddings | Gemini `gemini-embedding-001` (3072-dim, stored as raw float32 bytes in Postgres) |
| Sparse retrieval | rank-bm25 |
| Data processing | pandas, numpy, scikit-learn, scipy, pdfplumber |
| Database | Postgres (Railway production) · SQLite (local dev fallback) |
| Config | python-decouple |
| Frontend | Django templates, Marked.js (Markdown), SSE via Fetch API |

---

## Setup

**1. Clone and create a virtual environment**

```bash
git clone https://github.com/seyedhasanmirhoseini1367/HealthCompass.git
cd HealthCompass
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Create a `.env` file**

```env
SECRET_KEY=your-django-secret-key
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost

# At least one generation key is required. Groq has a generous free tier.
GROQ_API_KEY=your-groq-api-key
GEMINI_API_KEY=your-gemini-api-key   # also used for gemini-embedding-001
ANTHROPIC_API_KEY=                   # optional fallback
OPENAI_API_KEY=                      # optional fallback
```

At minimum you need `GEMINI_API_KEY` (for embeddings) plus one generation key (`GROQ_API_KEY` recommended — free tier). Keys with no value are skipped; the provider chain advances to the next available key.

**3. Run migrations and start**

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

---

## Skills Demonstrated

- **Django** — custom user model with role-based profiles, signals, class-based admin with inlines, UUID primary keys, JSONField
- **LangGraph** — StateGraph with conditional edges, self-correction retry loops, typed state
- **RAG pipeline** — hybrid BM25 + dense retrieval, MMR re-ranking, time decay, multi-LLM fallback
- **Streaming** — Server-Sent Events with Django `StreamingHttpResponse`, incremental Markdown rendering in the browser
- **LLM integration** — google-genai v1.x, Anthropic, OpenAI SDKs; system prompts; chat history management
- **Data engineering** — PDF text extraction, CSV wearable ingestion, lab value parsing, abnormal flagging
- **ML** — Gemini gemini-embedding-001 dense vectors, BM25 sparse retrieval, cosine similarity, MMR re-ranking, scikit-learn, scipy
