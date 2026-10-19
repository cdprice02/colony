# colony

The LLM layer of a one-person homelab: an MCP server that gives AI agents search and
write access to a personal knowledge base, and small local models that answer narrow
questions only after they pass an eval. Everything runs on hardware at home: a Raspberry
Pi 4 running k3s hosts the server, and a desktop with an RTX 3070 serves the models through
Ollama.

| Part | What it is |
|---|---|
| [`hive/`](hive/) | **The Hive**, an MCP server (streamable HTTP, its own OAuth 2.1 authorization server). Semantic search over an Obsidian vault and a Google Drive mirror, note read and write tools with an audit log, and Scout. |
| [`hive/indexer.py`](hive/indexer.py) | The nightly indexer: embeds notes by heading, PDF pages as both text and image, pictures, and text files into SQLite, incrementally and resumably. |
| [`scout/`](scout/) | **Scout**'s bench. `scout_classify` asks a 4B model to pick one label from a closed list; a kind of question is trusted to it only once it passes. |
| [`guard/`](guard/) | **Guard**'s test set and runner. A local model triages Home Assistant's warnings into "push a notification" or "just log it", gated on a pass bar. |

## The Hive

Tools, all prefixed by the part that serves them:

- `forager_semantic_search`: search by meaning over note chunks, page text and page images,
  merged with reciprocal rank fusion (image scores run lower than text, so each kind ranks
  its own hits).
- `forager_related_notes`, `forager_get_page`: neighbours of a note; a Drive page or
  picture rendered as an image for the agent to read.
- `forager_read`, `forager_search`, `forager_search_frontmatter`, `forager_list`, and the
  write tools (`forager_write`, `forager_edit`, `forager_append`, `forager_move`, which
  repoints wikilinks as Obsidian does, `forager_delete` to `.trash/`, and daily notes).
  Writes are atomic and each is one JSON line in the audit log.
- `scout_classify`: one label for a text, from a small local model, logged for 90 days.

Auth is built in: clients register themselves (dynamic client registration), the owner signs
in once per client with a username and password, and each client gets its own token, stored
hashed and revocable from the command line. Sign-in failures and registrations are
rate-limited globally.

When the model host is asleep, search and Scout fail fast with a useful message, and can
publish an MQTT wake request (`WAKE_MQTT_HOST`) for a home automation to answer with
wake-on-LAN.

## Evals

Both model-backed tools are gated, not trusted:

- **Scout:** 144 labelled cases in four kinds. A kind passes at 30+ cases, 95% right, and
  zero flips across 3 repeats at temperature 0. Currently only extraction passes, so the
  server's instructions tell agents to use Scout for that and decide the rest themselves.
- **Guard:** 53 seeded and real events. Zero missed must-push events, at most 1 false push
  in 10; a fixed pre-filter handles known noise, and is scored so that it cannot buy a pass.

Each README records what moved the scores and what didn't, with the saved runs in `results/`.

## Run it

The scripts carry their dependencies inline, so [uv](https://docs.astral.sh/uv/) is all
that is needed:

```sh
ollama pull embeddinggemma-2:740m && ollama pull tev1:4b
VAULT_DIR=~/notes INDEX_DB=./index.db uv run --script hive/indexer.py
cp .env.example .env   # set the password and PUBLIC_URL
VAULT_DIR=~/notes INDEX_DB=./index.db AUTH_DB=./auth.db SCOUT_DB=./scout.db \
  AUDIT_LOG=./audit.jsonl uv run --script hive/server.py
```

Every setting is an environment variable, documented at the top of
[`hive/server.py`](hive/server.py) and [`hive/indexer.py`](hive/indexer.py).
[`compose.yaml`](compose.yaml) is an example deployment; the real one runs on k3s behind a
Tailscale Funnel. The Drive mirror is optional: point `MIRROR_DIR` at an
`rclone sync` of Drive, or leave it out for notes only.

Tests need no model host:

```sh
uv run pytest
```

## License

MIT
