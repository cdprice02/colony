"""Smoke tests that need no model host: the pure pieces of each tool."""

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import collect
import indexer
import run
import server
import vault


def test_server_imports_with_neutral_defaults():
    assert server.PUBLIC_URL == "http://localhost:8000"
    assert server.wake_model_host() is False  # no WAKE_MQTT_HOST: never wakes


def test_scout_offline_answers_at_once():
    with pytest.raises(ToolError, match="Scout offline"):
        server.scout_ask("Is this a question?", ["yes", "no"])


def test_scout_rejects_bad_labels():
    with pytest.raises(ToolError, match="labels"):
        server.scout_ask("text", ["yes", "yes"])


def test_login_refused_without_password():
    assert server.check_login("", "") is False


def test_sliding_limit():
    limit = server.SlidingLimit(2, 60)
    limit.record()
    assert limit.retry_after() == 0
    limit.record()
    assert limit.retry_after() > 0


def test_fuse_merges_a_page_text_and_image():
    rows = [
        {"item": 1, "kind": "text", "page": 1, "heading": None, "body": "page text",
         "doc": 1, "source": "drive", "path": "a.pdf", "drive_id": None},
        {"item": 2, "kind": "image", "page": 1, "heading": None, "body": None,
         "doc": 1, "source": "drive", "path": "a.pdf", "drive_id": None},
        {"item": 3, "kind": "note", "page": None, "heading": "H", "body": "note",
         "doc": 2, "source": "note", "path": "n.md", "drive_id": None},
    ]  # fmt: skip
    vecs = np.eye(3, dtype=np.float32)
    out = server.fuse(vecs, rows, np.ones(3, dtype=np.float32), lambda r: True, 10)
    assert sorted(e["path"] for e in out) == ["a.pdf", "n.md"]
    assert next(e for e in out if e["path"] == "a.pdf")["excerpt"] == "page text"


def test_vault_paths_stay_inside():
    with pytest.raises(ToolError, match="outside"):
        vault.resolve("../escape.md", must_exist=False)
    with pytest.raises(ToolError, match="aren't notes"):
        vault.resolve(".obsidian/app.json", must_exist=False)


def test_moment_formats():
    from datetime import date

    assert vault.moment("YYYY-MM-DD dddd Do", date(2026, 10, 11)) == "2026-10-11 Sunday 11th"


def test_note_chunks_at_headings(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "VAULT_DIR", tmp_path)
    (tmp_path / "n.md").write_text("---\ntype: x\n---\nintro\n# One\nfirst\n## Two\nsecond\n")
    items = list(indexer.note_items("n.md"))
    assert [(i[2], i[3]) for i in items] == [(None, "intro"), ("One", "first"), ("Two", "second")]


def test_guard_filters_and_render():
    case = {"kind": "log", "source": "homeassistant.components.ollama", "level": "ERROR", "text": "x"}
    rules = [{"source": r"^homeassistant\.components\.ollama$", "why": "owned elsewhere"}]
    assert run.filtered(case, rules) is rules[0]
    template, version = run.load_prompt(run.HERE / "prompt.yaml")
    assert version != "unversioned"
    assert "source: homeassistant.components.ollama" in run.render(template, case)


def test_collect_signature_strips_numbers():
    assert collect.signature("a", "took 12 s at 0xdeadbeef") == "a|took # s at #"
