# Scribe: experimental AI scribe demo

> **Experimental demo. Not for clinical use.** This is a learning and testing module. Do not use it on real patient care, and only upload recordings you have permission to process (simulated or de-identified consultations are best).

You upload a recording of a consultation. The server transcribes it locally with faster-whisper, then the project's existing LLM chain drafts a SOAP note. Every note item cites the transcript segments it came from. Assessment and Plan items also show matching excerpts from the curated guideline knowledge base (Käypä hoito / Terveyskirjasto / THL).

## Pipeline

```
upload (mp3/wav/m4a, ≤25 MB)
  → status: uploaded      background thread starts after the DB commit
  → status: transcribing  faster-whisper "small", CPU, int8, VAD on → Transcript(text, segments)
  → status: generating    existing provider chain (Groq → Gemini → Anthropic → OpenAI), strict JSON,
                          validated, one retry on invalid output → Note(soap, raw_output, provider)
                          GeneralKnowledgeService top-3 per Assessment/Plan item → Note.evidence
  → status: done | failed
```

| File | Role |
|------|------|
| `services/transcription.py` | Lazy one-per-process Whisper model; a lock runs one transcription at a time |
| `services/note_generation.py` | Prompt, provider loop (honours `PHI_LLM_PROVIDERS`), JSON validation and retry |
| `services/evidence.py` | Best-effort guideline retrieval. A failure here never fails the encounter |
| `services/pipeline.py` | Status transitions, background thread, stale-job detection |

## Setup

1. Install: `pip install -r requirements.txt`. This adds `faster-whisper==1.2.1`.
2. Migrate: `python manage.py migrate scribe`.
3. LLM keys come from the existing env config (`GROQ_API_KEY`, `GEMINI_API_KEY`, …). No new keys are needed.
4. The first transcription downloads the Whisper weights (~480 MB for `small`) from Hugging Face into the HF cache. Pre-fetch them once with `python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"`.
5. The user needs the **"Sending my health data to external AI providers"** consent (`/accounts/consent/`). The transcript is sent to an external LLM, so upload is refused without it.
6. To get guideline evidence, the general knowledge base must be indexed, as it is for the assistant's general-question mode. Otherwise items show "No matching guideline excerpts".

Optional settings. They are read with `getattr`, so none are required:

| Setting | Default | |
|---|---|---|
| `SCRIBE_WHISPER_MODEL` | `small` | `tiny` / `base` are much faster for quick tests |
| `SCRIBE_WHISPER_THREADS` | `2` | CPU threads for CTranslate2 |
| `SCRIBE_WHISPER_BEAM_SIZE` | `1` | Greedy decoding. Use 5 for slightly better accuracy at ~2–3× the CPU |
| `SCRIBE_STALE_MINUTES` | `120` | In-progress jobs older than this are marked failed |

## Running it

Start the server (`python manage.py runserver`), log in, and open **`/scribe/`**. Upload a recording and the detail page polls every 4 s until the note is ready. Click a note item to highlight its source lines in the transcript.

Tests (Whisper, the LLM and retrieval are all mocked):

```
python manage.py test apps.scribe
```

## Evaluation

```
pip install -r apps/scribe/requirements-eval.txt      # rouge-score, bert-score (+ torch)
python manage.py scribe_eval --data /path/to/data --limit 5 [--out results.csv]
                             [--bertscore-model distilbert-base-uncased] [--no-bertscore]
```

Datasets are never downloaded automatically. Expected layout:

```
data/
  audio/
    <id>.wav | .mp3 | .m4a                # one mixed recording, or
    <id>_doctor.wav + <id>_patient.wav    # one file per speaker (PriMock57 style), mixed before transcription
  notes/
    <id>.txt                              # reference note as plain text, or
    <id>.json                             # {"note": "..."}  (also accepts "reference"/"text", a string, or a list of lines)
```

Samples without both audio and a reference note are skipped. For PriMock57, copy or convert its audio and reference notes into this layout. Check the note format of the release you have, since the loader only reads the shapes listed above.

The command writes one CSV row per sample: id, status, provider, segment count, transcription and generation seconds, ROUGE-L F, BERTScore F1, error, reference text and generated text. It also prints the means. The generated note is flattened to `Subjective:\n- …` text before scoring. It writes nothing to the database and skips evidence retrieval.

Absolute scores are not comparable to published PriMock57 numbers. The reference notes there use a different structure and style, and BERTScore defaults to `distilbert-base-uncased` (small) rather than the library's `roberta-large`. Use the numbers to compare prompt or model changes with each other.

## Known limitations

- **No diarization.** The LLM infers who is speaking from context. Short or ambiguous exchanges can be attributed to the wrong person.
- **Background threads, not a queue.** A server restart kills in-flight jobs (they show as failed after `SCRIBE_STALE_MINUTES`). Each web worker process loads its own Whisper model (~1 GB RAM for `small`), so this needs a host with headroom. A 512 MB instance will not run it.
- **Speed.** CPU transcription takes roughly 0.3–1× the recording length for `small` with int8, and jobs run one at a time per process.
- **Output length.** Generation uses the shared `RAG_CONFIG['MAX_TOKENS']` (1200). A very long consultation can produce a truncated reply, which fails validation twice and then fails the encounter.
- **Grounding is checked structurally, not semantically.** Segment indices are validated to exist, but nothing verifies that a segment really supports its item. Items with no valid citation are flagged in the UI.
- **Evidence retrieval** loads the whole knowledge-base index and makes one embedding call per Assessment/Plan item. That is fine for a demo but slow for long notes.
- **No editing, no export, no live recording, no audio playback.** The audio is not served back: `/media/` refuses files it cannot attribute to an owner.
- **The egress point is not registered** in `apps/accounts/egress.py` yet. The consent check and the `PHI_LLM_PROVIDERS` allowlist are enforced directly in this app.
