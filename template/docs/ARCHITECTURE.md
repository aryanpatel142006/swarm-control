# Architecture

## Components
<one line per component: name, responsibility, language, entry point>

## Data flow
<capture → processing → transport → UI, with the event names from docs/CONTRACTS.md>

## How to run

```
bash scripts/setup_worktree.sh   # once per worktree
bash scripts/verify_fast.sh      # lint + unit + smoke
```

## ml
<models, inputs, outputs, latency budget per stage, fallback tiers>
