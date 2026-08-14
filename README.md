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
  `400`/`401`/`403`) is stricter than the SDK's blanket "retry twice" default. Backoff is
  `2^attempt + jitter`, up to 3 total attempts.
- **Timeout**: client timeout is set to 30s, well under the SDK's 10-minute default. A timeout
  surfaces to the caller as `504`.
- **Kill switch**: `LLM_ENABLED=false` short-circuits before the model client is even imported, and
  returns a deterministic all-null, `needs_review: true` fallback.
- **Stub mode**: `LLM_STUB=1` returns a fixed schema-valid object with zero model calls — used for all
  local development so `uvicorn --reload` restarts don't burn OpenRouter's 50/day free quota.
- **Repair loop**: exactly one repair retry on a parse or schema failure, sending the model its own
  broken output plus the exact validation error. A second failure quarantines to
  `logs/quarantine.jsonl` and returns `422` — the raw model text is never handed to the caller, on
  either the success or the failure path.

## Eval

`evals/cases.json` has 8 hand-labelled cases, including one non-receipt input and two cases that
should hit the "when unsure → needs_review" rule. Run:

```bash
uvicorn app.main:app &
python -m evals.run_eval
```

**Score: not yet run against the real model** — this development sandbox has no network route to
`openrouter.ai`, so the eval above was only exercised in `LLM_STUB=1` mode to prove the harness itself
works (it correctly reports 0/8, since stub mode always returns the same fixed object regardless of
input — that's stub mode doing its job, not a real score). Run `python -m evals.run_eval` yourself
with a real `LLM_API_KEY` and record the date + score here, e.g.:

> `7/8 — 2026-08-14 — prompt extract-v1`

## Cost

Each call logs one structured JSON line (prompt version, model, input/output tokens, duration,
whether it needed a repair) via Python's `logging` module to stdout — see `_log_cost` in
`app/llm/client.py`. `openrouter/free` costs nothing per call, so token counts matter for the free
daily cap (50 requests/day) rather than for a dollar figure. For a paid model, this same log line is
what a cost-per-1,000-requests estimate would be built from — see the [LLM price
calculator](https://llmpricecheck.com) for the arithmetic.

## What I'd fix with another day

Give the prompt a real few-shot example for multi-currency ambiguity (a receipt with both a `$` symbol
and a country context that contradicts it) — the current three examples don't cover that case, and it's
the one most likely to produce a confident-but-wrong currency guess.

## Repo layout

```
app/
  main.py            FastAPI app, 400-on-bad-input handler
  routes/extract.py  the endpoint: kill switch, stub mode, real path
  llm/
    client.py         OpenAI-compatible client: timeout, retries, cost log
    parse.py          parse -> validate -> repair once -> quarantine
    schema.py          request/response Pydantic models, enums
prompts/
  extract-v1.md        the prompt, versioned
evals/
  cases.json            8 labelled cases
  run_eval.py           scores a running instance against cases.json
JOB-CARD.md
.env.example
```
