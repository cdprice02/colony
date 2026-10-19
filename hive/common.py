"""Shared by the indexer and the Hive's server: the index schema and the client
for the model host's Ollama (embeddings, and Scout's chat)."""

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.request

import numpy as np

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("EMBED_MODEL", "embeddinggemma-2:740m")
DIM = 768

# One row per source file (a note, a Drive file); its embedded pieces are items.
# kind: note (a note chunk), text (a page's text), image (a page or picture).
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- note | drive
    path TEXT NOT NULL,
    drive_id TEXT,
    mtime TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,          -- done | skipped
    reason TEXT,
    pages INTEGER,
    indexed_at TEXT NOT NULL,
    UNIQUE (source, path)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    page INTEGER,                  -- 1-based; NULL for notes and pictures
    heading TEXT,
    body TEXT,
    vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS items_doc ON items(doc_id);
"""


class OllamaDown(Exception):
    """The model host is off or not answering."""


def connect(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def _post(path, body, timeout):
    req = urllib.request.Request(
        OLLAMA_URL + path,
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OllamaDown(f"{OLLAMA_URL}: {e}") from e


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=10):
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def embed(inputs, timeout=180):
    """Embed a list of inputs: str for text, bytes for an image (PNG or JPEG).
    Returns unit-length float32 vectors, one per input."""
    payload = [
        {"image": base64.b64encode(x).decode()} if isinstance(x, bytes) else x
        for x in inputs
    ]
    r = _post("/api/embed", {"model": MODEL, "input": payload}, timeout)
    vecs = np.asarray(r["embeddings"], dtype=np.float32)
    if vecs.shape != (len(inputs), DIM):
        raise ValueError(f"embed returned {vecs.shape}, want ({len(inputs)}, {DIM})")
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def chat(body, timeout):
    """One /api/chat call, not streamed; OllamaDown when the model host doesn't answer."""
    return _post("/api/chat", {**body, "stream": False}, timeout)


def pack(vec):
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob):
    return np.frombuffer(blob, dtype=np.float32)
