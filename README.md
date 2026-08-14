# Receipt Extractor API — `POST /extract`

Pastes of receipts and invoices are messy text. This endpoint takes that text and gives back six clean,
validated fields — vendor, date, total amount, currency, a confidence score, and a `needs_review` flag —
so the rest of an app can trust the shape of the answer without a human reading every receipt by hand.
It's one request in, one structured answer out: no conversation, no memory between calls.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in LLM_API_KEY with your OpenRouter key
uvicorn app.main:app --reload
```

**Valid request:**

```bash
curl -s -X POST http://127.0.0.1:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"text": "Cafe Aroma\n12 Jun 2025\nTotal: PKR 1082"}'
```

```json
{"vendor":"Cafe Aroma","date":"2025-06-12","total_amount":1082.0,"currency":"PKR","confidence":0.95,"needs_review":false}
```

**Broken request** (missing field, no model call spent on it):

```bash
curl -s -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" -d '{}'
```

```json
{"error": "text: Field required"}
```

## Job card

See [`JOB-CARD.md`](JOB-CARD.md). Summary: input is `{"text": "..."}` (1–4000 chars); output is
`vendor`, `date` (`YYYY-MM-DD`), `total_amount`, `currency` (`PKR|USD|EUR|GBP|other`), `confidence`,
`needs_review`. It must never invent an amount, date, or currency that isn't in the text. When unsure,
it returns `null`/`needs_review: true` rather than guessing.

## Provider

Built against **OpenRouter**, model `openrouter/free`. Three env vars swap the provider entirely —
no code changes:

| Var | This project | Ollama equivalent |
|---|---|---|
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | `http://localhost:11434/v1/` |
| `LLM_API_KEY` | your OpenRouter key | the literal string `ollama` |
| `LLM_MODEL` | `openrouter/free` | `gemma3:1b` |

That's the whole difference between a model on someone else's server and one running on your laptop —
which is exactly why the provider should never be hard-coded.

## Design decisions

- **Retries**: the SDK's own retries are disabled (`max_retries=0`) and driven manually in
  `app/llm/client.py`, because the assignment's retry rule (retry `429`/`5xx`/timeouts, never
  `400`/`401`/`403`) is stricter than the SDK's
