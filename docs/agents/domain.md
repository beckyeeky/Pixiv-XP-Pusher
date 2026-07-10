# Domain Docs

This is a single-context repository.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- Relevant architectural decisions under `docs/adr/`.

If these files do not exist, proceed silently. Domain-modeling skills create them lazily when terminology or architectural decisions are resolved.

## Expected layout

```text
/
├── CONTEXT.md
└── docs/
    └── adr/
```

## Vocabulary

Use the domain terminology defined in `CONTEXT.md`. If a needed concept is missing, reconsider whether it belongs in the model or note the gap for domain modeling.

## ADR conflicts

Explicitly identify output that conflicts with an existing ADR rather than silently overriding the decision.
