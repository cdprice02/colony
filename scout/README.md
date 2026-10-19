# Scout: the bench

Scout is `scout_classify`, a small local model answering closed questions (yes/no, pick a
category, which of these) for agents. An agent should hand a kind of question to Scout only
once that kind has passed this bench. The bench asks each question through `scout_classify`'s
own code path, so every call also lands in `scout.db`, the evidence a router can use later.

| File | What |
|---|---|
| `cases/yes-no.yaml` | Yes/no questions: is it a question, a bug fix, a date, a complaint |
| `cases/category.yaml` | One category: the subsystem, a code snippet's language, the kind of request |
| `cases/extract.yaml` | Extraction as a choice: which of the given dates, machines, people or ports |
| `cases/rung.yaml` | Which model tier a task needs (`scored: false`: evidence, not a verdict) |
| `results/` | One file per saved run, named by date, model and prompt version |

Each file is one `kind` with `questions`. A question has `labels`, optional `instructions`,
and `cases` of `{text, expected}`. Quote every label: YAML reads a bare `yes` as true.

The cases come from a real homelab: `clover` is a Raspberry Pi 4 running k3s, `linden` is a
Windows PC with the GPU that runs Ollama, and `the Mac` is the workstation. The "Platform",
"House" and "Colony" categories are that homelab's subsystems (cluster, home automation,
models and agents).

## The bar

A kind passes with at least 30 cases, 95% right, and no case flipping across 3 repeats at
temperature 0. A case is right only if every repeat is. The rung kind's answers are
judgments (a larger model's, checked by a person), so it is reported and never passed.

Don't edit a case to make it pass. A case whose expected answer is genuinely debatable is
fixed or dropped on its own merits, and the change says so.

## Run

```sh
for f in scout/cases/*.yaml; do echo ---; cat "$f"; done | \
  OLLAMA_URL=http://localhost:11434 SCOUT_DB=/tmp/scout.db \
  uv run --script hive/server.py bench 3
```

It prints every miss, then one line per kind, and exits 0 when every scored kind passes. Set
`SCOUT_MODEL` to compare models. `tev1-guard` is `tev1:4b` with `temperature 0` baked in:

```sh
curl -sSf "$OLLAMA_URL/api/create" \
  -d '{"model":"tev1-guard","from":"tev1:4b","parameters":{"temperature":0},"stream":false}'
```

## Results (tev1-guard, scout-v1)

Run 2026-10-19 on Ollama 0.40.2, RTX 3070, 3 repeats per case:

| Kind | Right | Verdict |
|---|---|---|
| extract | 32/32 | PASS |
| category | 31/35 | FAIL |
| yes-no | 30/35 | FAIL |
| rung | 35/42 | evidence |

0 flips in 432 calls; median about 370 ms per call.

The misses are in [`results/`](results/). Read with care:

- **yes-no:** most misses are "a specific date or time" called yes for durations ("every
  night", "90 days", "a couple of minutes"); a real weakness. "I wonder if the PC is still
  on" (expected no) is borderline.
- **category:** most misses are the kind of request, where "go with your recommendations"
  and "yes, merge it" are both approval and instruction; borderline cases, not clear model
  errors.
- **rung:** the model rates tasks too low (Haiku-sized tasks called local), the costly
  direction for a router.

## What moved the scores

The caller's question, not Scout's system prompt:

- **Rung instructions as ordered rules** ("local only if the whole answer is one word from a
  list; writing, editing, summarizing or running anything is never local ... when two fit,
  choose the bigger one"): 18/30 to 25/30, and most misses now err upward, the cautious
  direction. A softer "when unsure, choose the bigger" changed nothing. Twelve held-out
  cases, written after the rewording and never used to tune it, score 10/12, the same rate,
  so the rules generalize rather than fit.
- **A system prompt asking Scout to take the question literally** (scout-v2): no better on
  yes-no or category, and extract fell to 30/32. Reverted; scout-v1 stands.
- **Machine names carry no role.** With the PC renamed from a descriptive hostname to
  `linden`, extract fell to 30/32, both PC cases answered `clover`. A legend in the question
  ("linden is the Windows PC with the graphics card") brings it back; so does a role in the
  label.

So a caller asking Scout should word the question as concrete rules, the way the rung
question now is. A reworded question gets held-out cases before its score counts.
