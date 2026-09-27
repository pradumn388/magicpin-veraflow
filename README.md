# VeraFlow — Magicpin AI Challenge

A deterministic, context-grounded implementation of the Magicpin Vera challenge contract.

## What it implements

- `POST /v1/context` — versioned/idempotent context storage.
- `POST /v1/tick` — trigger routing, suppression/deduplication, merchant/customer message composition.
- `POST /v1/reply` — stateful multi-turn handling, explicit-intent handoff, auto-reply detection, graceful exit, and anti-repetition.
- `GET /v1/healthz` — liveness and context counts.
- `GET /v1/metadata` — bot identity and approach.
- `POST /v1/teardown` — optional state wipe supported by the brief.

## Design

The bot uses a deterministic routing layer rather than depending on a paid LLM at runtime. Each trigger type has a small composition policy that combines the category, merchant, trigger, and optional customer context. Messages favor verifiable numbers, existing offers, category vocabulary, and a single low-friction CTA. Customer outreach checks the supplied consent scope.

The reply layer specifically handles the two challenge pain points called out in the brief: repeated/boilerplate auto-replies and explicit action intent. A merchant who says they want to join or proceed is routed to the action path instead of being asked another qualification question.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then:

```bash
curl http://localhost:8080/v1/healthz
```

## Expand the provided seeds

```bash
python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded
```

## Challenge simulator

Edit the configuration at the top of `judge_simulator.py`, set `BOT_URL`, and provide an LLM judge key if required by the simulator. Then run:

```bash
python judge_simulator.py
```

## Deployment

The repository includes a `Dockerfile` and `render.yaml`. Any public host that can run the container on port 8080 can be used. The challenge requires a public base URL exposing `/v1/*`.

## Privacy

No external API is called by the bot. Context is kept in memory only and can be cleared with `/v1/teardown`.
