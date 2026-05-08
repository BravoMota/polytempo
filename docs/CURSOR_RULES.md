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
