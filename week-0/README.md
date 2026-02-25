# GitHub Repository Summarizer

API service that takes a GitHub repository URL and returns an LLM-generated summary of the project.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your LLM API key (at least one is required)
export NEBIUS_API_KEY=your-key-here
# or: export GOOGLE_API_KEY=your-key-here

# 3. (Optional) Set GitHub token for higher rate limits (5,000 vs 60 req/hr)
export GITHUB_TOKEN=your-github-token
# or place it in ~/.ssh/github_token

# 4. Start the server
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## Verification

### API endpoint

```bash
# Start the server (in background or separate terminal)
python -m uvicorn main:app --host 0.0.0.0 --port 8000 &

# Test: successful summary
curl -s -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/psf/requests"}' | python -m json.tool

# Test: invalid URL → 400
curl -s -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "not-a-url"}'

# Test: non-existent repo → 404
curl -s -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/nonexistent/repo999"}'
```

The response JSON contains `summary`, `technologies`, and `structure` fields.

Interactive API docs: http://localhost:8000/docs

### CLI tool

```bash
# Full analysis (auto-detects LLM provider from env vars)
python cli.py https://github.com/psf/requests

# Force a specific provider
python cli.py https://github.com/psf/requests --provider nebius
python cli.py https://github.com/psf/requests --provider google

# Just fetch and print the README (no LLM call)
python cli.py https://github.com/psf/requests --readme-only

# Verbose mode (shows per-file details and raw LLM responses)
python cli.py https://github.com/psf/requests -v
```

### Test with large repository

```bash
python cli.py https://github.com/torvalds/linux
```

Pre-computed results for 10 repositories (including torvalds/linux) are in `testdata/summaries.md`.

## Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `NEBIUS_API_KEY` | One of these | Nebius Token Factory API key |
| `GOOGLE_API_KEY` | is required | Google GenAI API key |
| `GITHUB_TOKEN` | No | GitHub token for higher API rate limits (5,000 vs 60 req/hr) |

The server auto-detects which LLM provider to use based on which API key is set (Nebius takes priority).

## Project Structure

```
week-0/
├── main.py                     # FastAPI application
├── cli.py                      # CLI tool for development/testing
├── requirements.txt            # Python dependencies
├── summary/
│   ├── agent.py                # RepoAnalyzer — orchestrates the L1+L2 pipeline
│   ├── github.py               # GitHubClient — API calls, tree fetching
│   ├── tree.py                 # Tree pruning, formatting, skip logic
│   ├── llm.py                  # LLM base class, factory, prompt loading
│   ├── nebius.py               # Nebius provider (Llama-3.3-70B-Instruct)
│   └── gemini.py               # Google GenAI provider (Gemini 2.5 Flash)
├── prompts/
│   ├── file_picker.txt         # Prompt for LLM file selection
│   └── summarizer.txt          # Prompt for final summarization
└── testdata/
    ├── repositories.txt        # 10 test repository URLs
    └── summaries.md            # Pre-computed results with token stats
```

## Model Choice

**Nebius:** Llama-3.3-70B-Instruct — chosen after benchmarking 5 models on `torvalds/linux`:

| Model | Time | Output tokens | Result |
|---|---|---|---|
| **Llama-3.3-70B-Instruct** | **29.7s** | 247 | ✅ Correct, concise |
| Gemini 2.5 Flash (Google) | 50.0s | 438 | ✅ Excellent |
| Qwen3-32B | 242.8s | 9,059 | ✅ Good (hidden `<think>` tokens) |
| Qwen3-30B-A3B-Instruct | 201.3s | 8,443 | ✅ Good (hidden `<think>` tokens) |
| DeepSeek-V3-0324 | — | 8,192 | ❌ All output tokens used on reasoning |

Qwen3 models burn ~8K tokens on internal `<think>` reasoning even in "Instruct" mode. DeepSeek-V3 used its entire output budget on hidden reasoning, producing no useful output. Llama is the clear winner: fast, no wasted tokens, correct results.

**Google:** Gemini 2.5 Flash — fast, large context window, excellent quality.

## Design Decisions

### Documentation-First Approach

The core insight: **well-documented repositories don't need source code analysis** for a good summary. Humans understand projects by reading documentation first — so does our system.

### Two-Layer Pipeline

**Layer 1 — Deterministic (no LLM call):**
- Fetch the file tree via GitHub Trees API (incremental, depth-limited)
- Prune the tree: remove binary files, lock files, vendored dirs; cap at 200 entries
- Always fetch: `README.md`, `AGENTS.md`, `llms.txt`, `GEMINI.md`, `CLAUDE.md`, etc.

**Layer 2 — LLM-Guided File Selection:**
- Send the directory tree to the LLM: "Given this structure and a token budget, which files should I read?"
- The LLM picks config files, documentation, and key source files
- Fetch each selected file (even if not in the truncated tree — we try anyway)

**Final:** Assemble all context (tree + L1 + L2 files) and send for structured summarization.

### Incremental Tree Fetching

Instead of downloading the entire recursive tree (72K+ entries for Linux kernel), we fetch level-by-level up to depth 3, with a cap of 50 API calls. Skip filtering is applied inline — directories like `node_modules/`, `vendor/`, `__pycache__/` are never expanded.

### Token Budget

- Context budget: 80,000 tokens (default) or provider-specific limit
- Per-file truncation: 10,000 tokens
- Token counting via `tiktoken` (cl100k_base encoding)
- Files are filled greedily by priority layer (L1 first, then L2)
