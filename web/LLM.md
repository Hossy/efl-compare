# LLM Setup

This tool no longer bundles a model. Instead it calls an **OpenAI-compatible
Chat Completions API** that *you* point it at. That means you can use anything —
your existing local GGUF, Ollama, LM Studio, vLLM, or the hosted OpenAI API —
by editing three lines in `.env`. Nothing else in the code changes.

> **Coming from the old local-GPU build?** You don't need a GPU rebuild or a
> different model. Keep the same `Qwen2.5-7B-Instruct-Q4_K_M.gguf` you already
> had — just serve it (recipe [A](#a-your-existing-local-gguf-llamacpp) below)
> and set `.env`.

---

## TL;DR

```bash
cp .env.example .env      # then edit .env:
```

```ini
EFL_LLM_BASE_URL=http://127.0.0.1:8080/v1   # your server, usually ends in /v1
EFL_LLM_MODEL=qwen3.6-35b                    # the model id your server reports
EFL_LLM_API_KEY=sk-no-key-required           # any placeholder for local servers
EFL_LLM_DISABLE_THINKING=true                # true for reasoning models (Qwen3, etc.)
```

Then run normally:

```bash
python3 efl_compare.py --zip YOUR_ZIP --json plans_latest.json
```

`.env` is git-ignored, so your endpoint/keys never get committed. Real
environment variables override `.env` if you'd rather set them per-shell.

---

## Pick a backend

Any server that speaks the OpenAI `/v1/chat/completions` API works. Common ones:

### A. Your existing local GGUF (llama.cpp)

The original tool used a local `llama-cpp-python` in-process. The modern
equivalent is llama.cpp's **`llama-server`**, which exposes the exact API this
tool wants. Point it at the same `.gguf` file you already downloaded:

```bash
# Build/download llama.cpp: https://github.com/ggml-org/llama.cpp
llama-server \
  --model ./models/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  --n-gpu-layers -1 \        # -1 = offload all layers to GPU; 0 = CPU only
  --ctx-size 16384 \
  --alias qwen2.5-7b          # this becomes EFL_LLM_MODEL
```

`.env`:

```ini
EFL_LLM_BASE_URL=http://127.0.0.1:8080/v1
EFL_LLM_MODEL=qwen2.5-7b
EFL_LLM_API_KEY=sk-no-key-required
EFL_LLM_DISABLE_THINKING=false   # Qwen2.5 is NOT a reasoning model; leave false
```

> Docker alternative (no local build): the official CUDA server image —
> ```bash
> docker run --gpus all -p 8080:8080 \
>   -v "$PWD/models:/models" ghcr.io/ggml-org/llama.cpp:server-cuda \
>   --model /models/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
>   --host 0.0.0.0 --port 8080 --n-gpu-layers -1 --alias qwen2.5-7b
> ```

### B. Ollama

```bash
ollama serve                 # runs on :11434
ollama pull qwen2.5:7b-instruct
```

```ini
EFL_LLM_BASE_URL=http://127.0.0.1:11434/v1
EFL_LLM_MODEL=qwen2.5:7b-instruct
EFL_LLM_API_KEY=ollama
EFL_LLM_DISABLE_THINKING=false
```

### C. LM Studio

Start its local server (Developer tab → Start Server, default port `1234`), load
a model, then:

```ini
EFL_LLM_BASE_URL=http://127.0.0.1:1234/v1
EFL_LLM_MODEL=your-loaded-model-id
EFL_LLM_API_KEY=lm-studio
```

### D. vLLM

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8000
```

```ini
EFL_LLM_BASE_URL=http://127.0.0.1:8000/v1
EFL_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
EFL_LLM_API_KEY=sk-no-key-required
```

### E. Hosted OpenAI API

```ini
EFL_LLM_BASE_URL=https://api.openai.com/v1
EFL_LLM_MODEL=gpt-4o-mini
EFL_LLM_API_KEY=sk-...your-real-key...
```

(Keep `.env` out of git — it already is.)

---

## Settings reference

| Variable | Default | Notes |
|---|---|---|
| `EFL_LLM_BASE_URL` | — (required for LLM) | Base URL, usually ending in `/v1`. Omit and the tool tells you to configure it (or use `--no-llm`). |
| `EFL_LLM_MODEL` | `default` | Model id your server reports at `GET /v1/models`. |
| `EFL_LLM_API_KEY` | `sk-no-key-required` | Placeholder for local servers; real key for hosted OpenAI. |
| `EFL_LLM_TIMEOUT` | `120` | Per-request timeout (seconds). |
| `EFL_LLM_DISABLE_THINKING` | `false` | `true` suppresses hidden reasoning tokens on reasoning models (Qwen3, DeepSeek-R1, etc.). Sent via the OpenAI `extra_body` escape hatch; ignored by servers that don't support it. |

The tool constrains every response to a JSON Schema using the OpenAI
`response_format: {type: "json_schema", ...}` standard — so any compliant server
returns strictly-shaped JSON with no prompt-engineering on your part.

---

## Verify your connection

**1. Is the server up and what model id does it report?**

```bash
curl -s http://127.0.0.1:8080/v1/models | python3 -m json.tool
```

**2. Does the tool's own LLM path work end-to-end?**

```bash
python3 - <<'PY'
import llm_backend, credit_parser as cp
llm_backend.load_dotenv()
print("backend:", llm_backend.label())
print(cp.parse_credits("A $50 bill credit applies when usage is 1000 kWh or more."))
PY
```

Expected: `backend: <model> @ <host>` followed by
`[{'amount': 50.0, 'threshold_kwh': 1000, 'cumulative': False, 'requires_enrollment': False}]`.

**3. Full run** (once 1–2 pass):

```bash
python3 efl_compare.py --zip YOUR_ZIP --json plans_latest.json
```

The run's stats line ends with `LLM backend: <model> @ <host>` and an LLM
call/token count.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LLMNotConfigured: No LLM endpoint configured` | `EFL_LLM_BASE_URL` isn't set. Copy `.env.example` → `.env` and fill it in (or pass `--no-llm`). |
| `Connection refused` / timeouts | Server isn't running, wrong host/port, or a firewall/container-network boundary. Confirm step 1's `curl` works from the same machine the tool runs on. |
| Empty output / `finish_reason: length` / garbled JSON | A **reasoning model** spent the token budget "thinking." Set `EFL_LLM_DISABLE_THINKING=true`. If your server ignores that flag, raise the server's default max tokens or use a non-reasoning model. |
| `404` / "model not found" | `EFL_LLM_MODEL` doesn't match what the server reports. Check `GET /v1/models` (step 1) and copy the exact id. |
| `401` / auth errors | Real key required (hosted OpenAI), or the server expects a specific key. Set `EFL_LLM_API_KEY`. |
| Works in `curl` but not the tool | Make sure `EFL_LLM_BASE_URL` includes the `/v1` path segment most servers require. |
| Slow first call, fast after | Normal — the server caches the long few-shot prompt prefix. Subsequent calls reuse it. |

---

## How it's wired (for maintainers)

- `llm_backend.py` — the only place that talks to the LLM. Loads `.env`, builds
  an `openai.OpenAI` client from the env vars, and exposes `ChatBackend`.
- `credit_parser.py` — calls `llm_backend.ChatBackend().create_chat_completion(...)`
  with a per-call JSON schema. Two call sites: bill-credit extraction and
  EFL rate/structure extraction.
- `efl_compare.py` — loads `.env` at startup and orchestrates everything.

To add a new provider, you almost certainly don't need code changes — just point
`.env` at it. If a provider needs a non-standard parameter, add it in
`ChatBackend.create_chat_completion` via `extra_body` (that's how
`enable_thinking` is passed today).
