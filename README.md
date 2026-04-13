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
- LangGraph-based routing — questions are directed to the most relevant record type before retrieval

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

### Architecture

```
User question
      │
      ▼
 router_node          ← keyword-based routing, no LLM call
      │
      ├── lab_results_node   → retrieves lab result chunks
      ├── medications_node   → retrieves medication chunks
      ├── wearable_node      → retrieves wearable data chunks
      ├── diagnosis_node     → retrieves clinical note chunks
      ├── records_node       → retrieves all record types
      └── general_node       → retrieves all record types
                │
                ▼
         generate_node       ← Gemini → Anthropic → OpenAI fallback
                │
                ▼
          verify_node        ← checks answer quality
                │
        ┌───────┴───────┐
    needs_retry=True   needs_retry=False
        │                    │
   back to search           END
   (max 2 retries)
```

### Routing

The router classifies each question with keyword matching — free, no LLM call required:

| Route | Triggered by keywords |
|---|---|
| `lab_results` | lab, blood, cholesterol, glucose, HbA1c, TSH, vitamin, abnormal… |
| `medications` | medication, prescription, dose, side effect, interaction, insulin… |
| `wearable` | heart rate, steps, sleep, bpm, SpO2, Fitbit, Garmin, HRV… |
| `diagnosis` | diagnosis, MRI, CT scan, X-ray, ultrasound, discharge, surgery… |
| `records` | record, history, timeline, previous, last time… |
| `general` | (fallback — retrieves across all types) |

### Retrieval

Hybrid retrieval combining three signals:

- **BM25 (35%)** — keyword overlap (sparse retrieval)
- **Cosine similarity (65%)** — semantic embedding match (dense retrieval)
- **Time decay** — recent records ranked slightly higher
- **MMR re-ranking** — Maximal Marginal Relevance to reduce redundant chunks

Embeddings use `sentence-transformers` (`all-MiniLM-L6-v2`) stored as binary blobs in SQLite.

### Generation

Three LLMs tried in order; the first successful response is returned:

1. **Gemini** (`gemini-2.5-flash`) — primary, via `google-genai` v1.x
2. **Anthropic Claude** — first fallback
3. **OpenAI GPT** — second fallback

All three support both non-streaming (full response) and streaming (token-by-token SSE) modes.

### Self-correction

After generation, `verify_node` checks the answer for:

- Fewer than 15 words
- Uncertain phrases ("I don't know", "unable to find", "no information")
- Empty context (no chunks retrieved)

If any condition is true and fewer than 2 retries have been attempted, the graph loops back to retrieval with the same route.

### Streaming

The chat UI connects to `/assistant/stream/` which returns a `text/event-stream` response. Events:

```
data: {"type": "token",   "content": "..."}   ← one per token
data: {"type": "sources", "sources": [...]}   ← after stream ends
data: {"type": "done"}
data: {"type": "error",   "message": "..."}   ← on failure
```

Tokens are rendered incrementally with live Markdown parsing. The full response is saved to the database in a `finally` block so chat history is always consistent.

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
| LLM orchestration | LangGraph 0.2, google-genai, Anthropic SDK, OpenAI SDK |
| Embeddings | sentence-transformers (`all-MiniLM-L6-v2`) |
| Sparse retrieval | rank-bm25 |
| Data processing | pandas, numpy, scikit-learn, scipy, pdfplumber |
| Database | SQLite (development) |
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

GEMINI_API_KEY=your-gemini-api-key
ANTHROPIC_API_KEY=              # optional
OPENAI_API_KEY=                 # optional
```

At least one LLM API key is required. Gemini has a free tier.

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
- **ML** — sentence-transformers embeddings, cosine similarity, scikit-learn, scipy
