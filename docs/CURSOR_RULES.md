# Cursor Rules

## Development style

Treat Cursor as a junior implementer, not as an architect.

Work one small task at a time.

## Rules

- Always show a plan before editing.
- Do not modify unrelated files.
- Do not create extra architecture.
- Do not add dependencies without asking.
- Do not implement future phases early.
- Always add or update tests for behavior changes.
- Prefer small pure functions.
- Prefer typed dataclasses or Pydantic models where useful.
- Keep modules boring and explicit.

## Current architecture rules

- Low-level modules should stay independent.
- `distribution.py` should not know about market prices.
- `edge.py` should not know how probabilities were created.
- `decision.py` should not fetch data or compute edge.
- The next layer is an analysis/use-case layer, not an agent or orchestrator.

## Forbidden unless explicitly requested

- LLM agent code
- Web dashboard
- Live trading
- Autonomous order placement
- Background schedulers
- Plugin systems
- Generic framework abstractions
- Large refactors
- New external APIs
- Database setup

## Cursor model usage

Use Composer 2 for normal implementation.

Use stronger limited models only for:

- architecture review
- debugging hard issues
- reviewing large diffs
- checking for overengineering

Do not use premium models for boilerplate.

## Python environment

Always use the local virtual environment.

Before running tests or commands, assume the project uses:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -e ".[dev]"

When running Python commands, prefer:

    .venv/bin/python -m pytest
    .venv/bin/python -m pip
    .venv/bin/python -m polytempo

Do not install dependencies globally.
Do not use system Python for project commands if `.venv` exists.
Ask before installing new dependencies.
