---
description: Python environment setup, dependency management with uv, and runtime configuration.
paths:
  - "**/*.py"
  - "**/*.sh"
  - "**/*.env*"
  - "**/requirements*.txt"
---
# Environment Setup Rules

1. **Package Manager — use `uv` (preferred)**:
   - Always install and manage dependencies with `uv`, not bare `pip`.
   - Install uv if not present: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Create venv and install: `uv venv && source .venv/bin/activate && uv pip install -r requirements.txt`
   - Add a single package: `uv pip install <package>` — never `pip install` directly.
   - If conda is unavoidable: `conda create -n intramind python=3.11.9 && conda activate intramind && pip install -r requirements.txt`

2. **Always activate the venv before running anything**:
   - `source .venv/bin/activate` (uv venv) or `conda activate intramind` (conda).
   - Verify the active interpreter with `which python` — it must point inside `.venv/` or the conda env, never the system Python.

3. **Environment file**:
   - Copy `.env_example` to `.env` before first run: `cp .env_example .env`
   - Never commit `.env` to version control.
   - Use nested delimiter `__` for nested settings (e.g. `LLM__MODEL=qwen3.5-9b`). Flat legacy keys (`LLM_PROVIDER`, `OPENAI_MODEL`) still work via the backward-compatibility validator in `api/config.py` but should not be used in new config.

4. **Requirements files**:
   - `requirements.txt` — full dev + prod dependencies.
   - `requirements_runtime.txt` — production-only subset (used in Docker images).