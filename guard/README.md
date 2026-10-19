# Guard: test set and runner

Guard triages Home Assistant's own problem reports (system log warnings and errors, and
Repairs) that no fixed rule covers. A small local model, called through HA's AI Task, picks
the tier: **System** (push a notification) or **Informational** (log it). It never picks
Critical; that stays with hand-written rules.

These files prove a prompt and model against the pass bar before anything is deployed:
**zero misses on must-push cases (expected System), and at most 1 false push in 10
Informational cases.**

| File | What |
|---|---|
| `prompt.yaml` | The AI Task instructions: a template over `kind`, `source`, `level`, `text` |
| `filters.yaml` | The pre-filter: events a fixed rule records as Informational without the model |
| `run.py` | Sends each case to Ollama exactly as HA 2026.10.1's AI Task would, and scores it |
| `collect.py` | Drafts `cases/history.yaml` from HA's logs and Repairs (unlabelled) |
| `cases/seeded.yaml` | Hand-written hard and representative cases |
| `cases/history.yaml` | Real events, labelled by hand (from `collect.py`), lightly anonymized |
| `results/` | One JSONL per saved run: model, prompt version, options, answers, latency |

On the HA side, an automation `!include`s `prompt.yaml` and `filters.yaml`, so the runner
and production read the same files.

## Run

```sh
uv run guard/run.py --temperature 0 --repeat 5          # all labelled cases
uv run guard/run.py --temperature 0 --only 'mqtt|bluetooth'
uv run guard/run.py --temperature 0 --repeat 5 --save   # keep the evidence
uv run guard/run.py --model granite4.1:8b --temperature 0
```

`OLLAMA_URL` points at the model host (default `http://localhost:11434`); `GUARD_PROMPT` and
`GUARD_FILTERS` override the prompt and pre-filter paths. It exits 0 on PASS. A case fails if
any repeat misses, so a flaky answer shows as a failure. No verdict (timeout, bad JSON)
counts as System, as Guard treats it.

## Collect real history

HA's log reaches back only to its last start (`home-assistant.log`, and `.log.1` for the run
before), so history builds up by collecting every few days; `collect.py` keeps earlier
signatures and labels, and counts a record seen in two saved copies once. Copy
`home-assistant.log*` and `.storage/repairs.issue_registry` out of HA's config directory,
then:

```sh
uv run guard/collect.py ~/guard-history
```

Label each draft `expect` System or Informational with a `why`; `run.py` skips nulls. Scrub
names, addresses and device ids from the text before committing it.

## Findings

Measured on Ollama 0.40.2 with `tev1:4b`.

- **Temperature 0 is required.** HA's Ollama integration sends no temperature, and at the
  model's default the same case flips between tiers (prompt v1: 2 misses, 6 of 14 false
  pushes). At 0 most answers held across repeats, but not all, so keep `--repeat`. In
  production that means a Modelfile variant (`tev1-guard`: tev1 with `temperature 0`) chosen
  as the AI Task's model; `--temperature 0` stands in for it here.
- **Set the AI Task's context window to 2050.** HA sends `num_ctx` from its own settings
  (default 8192), overriding the model's. The largest prompt so far is 407 tokens.
- **Filter the model host's own errors before Guard.** A separate watch already owns the
  model host being off, and tev1 pushed `ollama-unreachable` under every wording.
- **A Repair reaches Guard as domain and issue id only:** the registry event carries nothing
  more, so that is what the Repair cases contain.
- **tev1 is sensitive to wording.** Dropping one true clause (v3) turned a correct
  `mqtt-one-disconnect` into a steady false push. Any prompt change reruns the full set.
- **House knowledge in the prompt backfires.** v5 told tev1 which systems matter (lights,
  cameras System; game console, TV Informational). Every "light unavailable" warning became
  System and `xbox-auth-failed` became Informational: tev1 matches words, it does not weigh
  them.
- **Name the service, not the machine.** v6 says "connection errors to Ollama"; wordings
  that named the PC that runs it cost one or two false pushes.
- **granite4.1:8b does worse** than tev1 on every prompt tried (7 of 13 false pushes on v3,
  22 of 31 on v4).

| Prompt | Model | Cases | Must-push missed | False pushes | Result |
|---|---|---|---|---|---|
| v1 | tev1:4b | seeded, default temperature, x3 | 2/18 | 6/14 | FAIL |
| v1 | tev1:4b | seeded, temperature 0, x3 | 0/18 | 2/14 | FAIL |
| v4 | tev1:4b | seeded, temperature 0, x5 | 0/18 | 1/13 | PASS |
| v4 | tev1:4b | seeded + history, no pre-filter | 0/19 | 6/31 | FAIL |
| v5 | tev1:4b | seeded + history | 1/19 | 20/31 | FAIL |
| v4 | granite4.1:8b | seeded + history | 0/19 | 22/31 | FAIL |
| v4 | tev1:4b | seeded + history, pre-filter, x5 | 0/19 | 1/27 | PASS |
| v7 | tev1:4b | seeded + history, pre-filter, x5 | 0/20 | 2/28 | PASS |
| v7 | tev1:4b | anonymized cases, pre-filter, x5 (2026-10-19) | 0/20 | 1/28 | PASS |

The last run, on the cases as published here: median 495 ms, max 964 ms, largest prompt 399
tokens.

Four of v4's six false pushes on real history were kinds a fixed rule can drop (the HACS
restart-pending Repair, HA core's own blocking-call warnings, game console and TV fetch
errors), so the pre-filter takes them. A filtered must-push case scores as a miss, and
filtered Informational cases leave the bar's denominator, so the filter can't buy a pass.
The remaining false push is `camera-stream-400` (a live-view RTSP error); whether that
deserves a push is a labelling call. The rules were fitted to a few days of history, so they
are reviewed as history grows.
