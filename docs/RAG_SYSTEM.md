# RAG System Architecture — HealthCompass

## Overview

HealthCompass uses a **Hybrid Retrieval-Augmented Generation (RAG)** pipeline to answer patient health questions using their own medical records. It combines semantic (embedding) search with keyword (BM25) search, then feeds the best-matching context to an LLM.

---

## System Components

```mermaid
graph TD
    subgraph INGESTION["📥 Ingestion Pipeline"]
        MR[MedicalRecord\nuploaded by doctor] --> DP[DocumentProcessor]
        DP --> MD[MedicalDocument\nformatted text + metadata]
        MD --> CH[MedicalChunk\n200-word chunks\n40-word overlap]
        CH --> ES[EmbeddingService\nsentence-transformers\nall-MiniLM-L6-v2]
        ES --> DB[(Database\nchunk + binary\nembedding stored)]
    end

    subgraph QUERY["💬 Query Pipeline"]
        U[Patient asks question] --> RS[RetrievalService]
        RS --> BM25[BM25 keyword score\n35% weight]
        RS --> COS[Cosine similarity\n65% weight]
        BM25 --> BL[Blend scores]
        COS --> BL
        BL --> TD[Time decay\nolder records\ndown-weighted ≤15%]
        TD --> MMR[MMR re-ranking\nrelevance + diversity\nλ=0.6]
        MMR --> TOP[Top 6 chunks\nthreshold ≥ 0.15]
        TOP --> GS[GenerationService]
    end

    subgraph GENERATION["🤖 Generation"]
        GS --> G[Gemini 1.5 Flash]
        G -->|fails| A[Claude Haiku]
        A -->|fails| O[GPT-4o Mini]
        O -->|fails| FB[Fallback message]
        G --> RESP[Response + sources]
        A --> RESP
        O --> RESP
    end

    DB --> RS
    RESP --> QL[(QueryLog saved)]
    RESP --> UI[Patient sees answer]
```

---

## Ingestion: How Records Become Searchable

```mermaid
flowchart LR
    R[MedicalRecord] --> DP

    subgraph DP["DocumentProcessor — chunking strategy"]
        direction TB
        LAB["lab_result\n→ one chunk per\nbatch of values"]
        MED["medication\n→ one chunk\nper prescription"]
        WEA["wearable\n→ statistical summary\nmean / min / max"]
        NOT["note / imaging /\ndischarge\n→ 200-word windows\n40-word overlap"]
        RAW["anything else\n→ raw text fallback"]
    end

    DP --> DOC[MedicalDocument]
    DOC --> CHK[MedicalChunk list]
    CHK --> EMB[EmbeddingService\nbatch encode → pickle bytes]
    EMB --> DB[(DB: MedicalChunk\n.embedding = binary)]
```

---

## Retrieval: 6-Stage Scoring Pipeline

```mermaid
flowchart TD
    Q[User query] --> LOAD[Load all patient\nchunk embeddings from DB]
    LOAD --> B[BM25 score\nsimple tokenization\nkeyword overlap]
    LOAD --> C[Cosine similarity\nquery embedding vs chunk]
    B --> BLEND["Hybrid blend\n0.35 × BM25 + 0.65 × cosine"]
    C --> BLEND
    BLEND --> DEC["Time decay\nscore × 1 − 0.15 × min(age_days/365, 1)"]
    DEC --> MMR["MMR re-ranking\npenalise chunks similar\nto already-selected ones"]
    MMR --> FILTER["Filter: score ≥ 0.15\nKeep top 6"]
    FILTER --> CTX[Context chunks\nsent to LLM]
```

---

## Generation: Provider Chain

```mermaid
sequenceDiagram
    participant R as RetrievalService
    participant G as GenerationService
    participant Gem as Gemini 1.5 Flash
    participant Ant as Claude Haiku
    participant OAI as GPT-4o Mini

    R->>G: top 6 chunks + query + history
    G->>Gem: prompt with context
    alt Gemini succeeds
        Gem-->>G: response
    else Gemini fails
        G->>Ant: same prompt
        alt Anthropic succeeds
            Ant-->>G: response
        else Anthropic fails
            G->>OAI: same prompt
            alt OpenAI succeeds
                OAI-->>G: response
            else all fail
                G-->>G: static fallback message
            end
        end
    end
    G-->>R: response text + source metadata
```

---

## Database Models

```mermaid
erDiagram
    MedicalRecord ||--o{ MedicalDocument : "indexed into"
    MedicalDocument ||--|{ MedicalChunk : "split into"
    Patient ||--o{ MedicalChunk : "owns"
    Patient ||--o{ ChatSession : "has"
    ChatSession ||--o{ QueryLog : "contains"
    MedicalChunk }o--o{ QueryLog : "cited in"

    MedicalDocument {
        string document_type
        text content
        json metadata
    }
    MedicalChunk {
        text content
        int chunk_index
        binary embedding
        json metadata
    }
    QueryLog {
        text query
        text response
        json sources
    }
```

---

## Key Configuration

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Embedding model | `all-MiniLM-L6-v2` | 384-dim sentence vectors |
| Chunk size | 200 words | Balance between context and precision |
| Chunk overlap | 40 words | Avoid cutting mid-sentence |
| BM25 weight | 35% | Keyword relevance |
| Semantic weight | 65% | Meaning-level relevance |
| Time decay | 15% max over 1 year | Prefer recent records |
| MMR lambda | 0.6 | 60% relevance, 40% diversity |
| Similarity threshold | 0.15 | Filter irrelevant chunks |
| Top-K | 6 chunks | Sent to LLM as context |
| Max tokens | 800 | LLM response length cap |

---

## File Map

```
rag_assistant/
├── models.py                  — MedicalDocument, MedicalChunk, ChatSession, QueryLog
├── rag_engine.py              — backward-compatible shim → delegates to services/
├── views.py                   — chat UI, API endpoints
└── services/
    ├── embedding_service.py   — encode text → binary vectors, bulk DB update
    ├── document_processor.py  — MedicalRecord → MedicalDocument → MedicalChunk
    ├── retrieval_service.py   — BM25 + cosine + decay + MMR → top-6 chunks
    ├── generation_service.py  — Gemini → Anthropic → OpenAI provider chain
    └── rag_service.py         — orchestrator: ask() and index_record() public API
```
