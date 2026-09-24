# Ollama Local Models

SkopaqTrader supports Ollama as a local LLM fallback. When cloud API keys are unavailable or you want to run analysis offline, Ollama provides a self-hosted alternative.

## How It Works

Ollama is the last option in each agent's fallback chain. If all cloud providers fail (missing keys, rate limits, errors), the system automatically falls back to a local Ollama model.

```python
# skopaq/llm/model_tier.py — fallback chain per role
"market_analyst":  [("google", "gemini-3.8-flash"), ("ollama", "auto")]
"social_analyst":  [("openrouter", "x-ai/grok-4.6"), ..., ("ollama", "auto")]
"trader":          [("google", "gemini-3.8-flash"), ("ollama", "auto")]
```

!!! note "Judge roles excluded"
    The `research_manager` and `risk_manager` roles do NOT have Ollama fallback. These judge roles require the strongest reasoning quality, so they only fall back from Claude Opus to Gemini -- never to a local model.

## Enabling Ollama

Ollama is opt-in. Set the environment variable:

```bash
SKOPAQ_OLLAMA_ENABLED=true
```

Without this, Ollama is never checked, even if it is running locally.

## Setup

### Step 1: Install Ollama

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

### Step 2: Pull a Model

```bash
ollama pull mistral        # Default fallback (7B, fast)
ollama pull llama3.1:8b    # Good general purpose
ollama pull qwen2.5:14b    # Stronger reasoning, needs more RAM
```

### Step 3: Start the Server

```bash
ollama serve
```

Ollama runs at `http://localhost:11434` by default.

### Step 4: Configure SkopaqTrader

```bash
# Required: opt-in to Ollama
SKOPAQ_OLLAMA_ENABLED=true

# Optional: custom base URL
SKOPAQ_OLLAMA_BASE_URL=http://localhost:11434

# Optional: specify model (otherwise auto-detected)
SKOPAQ_OLLAMA_MODEL=mistral
```

## Model Auto-Detection

When `SKOPAQ_OLLAMA_MODEL` is not set (or set to `auto`), the system auto-detects the best available model:

```python
def _get_ollama_model() -> str:
    # 1. Check SKOPAQ_OLLAMA_MODEL env var
    # 2. Query Ollama API for installed models
    # 3. Use the first available model
    # 4. Default to "mistral" if nothing found
```

The detection queries `http://localhost:11434/api/tags` and picks the first installed model.

## Availability Check

On first use, the system pings the Ollama API to check if it is running:

```python
def _is_ollama_available() -> bool:
    # 1. Check SKOPAQ_OLLAMA_ENABLED is set
    # 2. HTTP GET to /api/tags
    # 3. Cache the result for the session
```

The result is cached -- the check only happens once per session.

## LangChain Integration

Ollama models are created via `langchain-ollama`:

```python
from langchain_ollama import ChatOllama

llm = ChatOllama(
    model="mistral",
    base_url="http://localhost:11434",
    temperature=0.3,
)
```

The `ChatOllama` class implements the same `BaseChatModel` interface as all other providers, so it works seamlessly with the LangGraph pipeline.

## When Ollama Activates

The fallback chain is evaluated left to right for each agent role:

```
market_analyst:
  1. Try Google (Gemini 3.8 Flash)
     → GOOGLE_API_KEY set? → Use it
  2. Try Ollama (auto)
     → SKOPAQ_OLLAMA_ENABLED=true AND Ollama running? → Use it
  3. No provider available → Error
```

Ollama will activate if:

- All cloud API keys for that role's chain are missing or empty
- `SKOPAQ_OLLAMA_ENABLED=true` is set
- Ollama is running and responds to the health check

## Supported Models

Any model available through Ollama works. Recommended models for trading analysis:

| Model | Size | RAM | Quality | Speed |
|-------|------|-----|---------|-------|
| `mistral` | 7B | 8 GB | Good | Fast |
| `llama3.1:8b` | 8B | 8 GB | Good | Fast |
| `qwen2.5:14b` | 14B | 16 GB | Better | Medium |
| `llama3.1:70b` | 70B | 48 GB | Best | Slow |
| `deepseek-r1:14b` | 14B | 16 GB | Strong reasoning | Medium |

!!! warning "Quality trade-off"
    Local models are significantly less capable than cloud models (Claude Opus, Gemini) for financial analysis. Use Ollama as a fallback for experimentation, not for production trading decisions.

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_OLLAMA_ENABLED` | `false` | Opt-in flag (must be `true`, `1`, or `yes`) |
| `SKOPAQ_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `SKOPAQ_OLLAMA_MODEL` | `auto` | Model name or `auto` for auto-detection |

## File Reference

| File | Purpose |
|------|---------|
| `skopaq/llm/model_tier.py` | Fallback chains, Ollama detection, model creation |
| `tests/unit/llm/test_ollama.py` | Unit tests for Ollama integration |
