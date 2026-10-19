# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "mcp==2.3.0", "numpy==2.2.6", "paho-mqtt==2.1.0", "pillow==12.2.0", "pypdfium2==5.14.0",
#   "pyyaml==6.0.3",
# ]
# ///
"""The Hive's MCP server: one door for agents, with its own OAuth.

Forager: forager_semantic_search, forager_related_notes, forager_get_page over the
index the nightly indexer builds. Queries are embedded through Ollama on the model
host; when it is off, search can ask for it to be woken (an MQTT message, if
WAKE_MQTT_HOST is set) and says to retry in a couple of minutes. forager_get_page
works either way.

Forager's vault tools (vault.py): read and write an Obsidian vault kept on disk
beside the server. Writes are recorded in AUDIT_LOG; VAULT_WRITES=0 offers the
read tools only.

Scout: scout_classify, one label from a closed list, answered by a small local
model (SCOUT_MODEL). Every call is logged in SCOUT_DB for 90 days. When the model
host is off it answers at once that Scout is offline.

Auth: the server is its own OAuth authorization server. Clients register
themselves (dynamic registration), the owner signs in with one username and
password, and each client gets its own token that never expires. Tokens are
stored hashed in AUTH_DB. To list or cancel them, and to run the Scout bench:

  server.py clients
  server.py revoke <client_id>
  server.py scout-log [n]            the last n classify calls (default 20)
  server.py bench [repeat] < cases   the Scout bench (scout/)

Environment:
  PUBLIC_URL             where clients reach the server      http://localhost:8000
  INDEX_DB               the indexer's SQLite index          /index/index.db
  AUTH_DB                clients and tokens                  /index/auth.db
  MIRROR_DIR             rclone's mirror of Drive (optional) /index/drive
  SCOUT_DB               classify's log                      /index/scout.db
  SCOUT_MODEL            Scout's model                       tev1-guard (temperature 0)
  SCOUT_CTX              its context window                  2050
  VAULT_DIR              the vault                           /data/vault
  AUDIT_LOG              every vault write                   /data/audit/hive.jsonl
  VAULT_WRITES           1 to offer the write tools          1
  CONVENTIONS            the vault's conventions note        docs/conventions.md
  TZ                     the owner's, for the daily note
  VAULT_OAUTH_USERNAME   the sign-in
  VAULT_OAUTH_PASSWORD   (unset: nobody can sign in)
  PORT                   8000
  OLLAMA_URL             the model host's Ollama             http://localhost:11434
  WAKE_MQTT_HOST         broker for wake requests            (unset: never wake)
  WAKE_TOPIC             the wake request's topic            colony/models/wake
"""

import hashlib
import hmac
import html
import io
import json
import math
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import paho.mqtt.publish as mqtt_publish
import pypdfium2 as pdfium
from PIL import Image as PILImage

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import vault
from common import OllamaDown, chat, connect, embed, unpack

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
INDEX_DB = os.environ.get("INDEX_DB", "/index/index.db")
AUTH_DB = os.environ.get("AUTH_DB", "/index/auth.db")
MIRROR_DIR = Path(os.environ.get("MIRROR_DIR", "/index/drive"))
SCOUT_DB = os.environ.get("SCOUT_DB", "/index/scout.db")
SCOUT_MODEL = os.environ.get("SCOUT_MODEL", "tev1-guard")
# Match any other client of the same model: a different num_ctx makes Ollama
# reload it.
SCOUT_CTX = int(os.environ.get("SCOUT_CTX", "2050"))
USERNAME = os.environ.get("VAULT_OAUTH_USERNAME", "")
PASSWORD = os.environ.get("VAULT_OAUTH_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8000"))
WAKE_MQTT_HOST = os.environ.get("WAKE_MQTT_HOST", "")
WAKE_TOPIC = os.environ.get("WAKE_TOPIC", "colony/models/wake")
CONVENTIONS = os.environ.get("CONVENTIONS", "docs/conventions.md")

SCOPE = "hive"
CODE_TTL = 300
LOGIN_TTL = 600

DPI = 150  # as the indexer renders
MAX_SIDE = 1700
RRF_K = 60  # the usual constant; ranks below a few dozen add little
PER_KIND = 50  # hits each kind contributes to the fusion
EXCERPT = 300

SCOUT_PROMPT = "scout-v1"  # bump with any change to SCOUT_SYSTEM or scout_prompt()
SCOUT_SYSTEM = "You classify text. Answer with exactly one of the given labels."
SCOUT_TIMEOUT = 30  # a model swap on the model host takes a few seconds
SCOUT_KEEP = 90 * 86400  # the log is kept 90 days, not backed up
BENCH_MIN = 30  # cases a kind needs before it can pass
BENCH_BAR = 0.95  # and the share it must get right, with no flips


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---- auth store ----------------------------------------------------------------

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info TEXT NOT NULL,            -- OAuthClientInformationFull as JSON
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (  -- authorize requests waiting for the form
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY,
    data TEXT NOT NULL,            -- AuthorizationCode as JSON
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,         -- sha256 of the token; the token is never stored
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used INTEGER
);
"""


def auth_db():
    db = sqlite3.connect(AUTH_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(AUTH_SCHEMA)
    return db


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class SlidingLimit:
    """At most `limit` events in any `window` seconds, across all callers.
    Global, not per address: a guesser can switch addresses."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.events = deque()
        self.lock = threading.Lock()

    def retry_after(self):
        with self.lock:
            now = time.monotonic()
            while self.events and self.events[0] <= now - self.window:
                self.events.popleft()
            if len(self.events) < self.limit:
                return 0
            return max(1, math.ceil(self.events[0] + self.window - now))

    def record(self):
        with self.lock:
            self.events.append(time.monotonic())


# Far above what the owner does, far below what guessing needs. Someone
# hammering the form can hold it shut for a while; issued tokens keep working.
login_failures = SlidingLimit(10, 15 * 60)
registrations = SlidingLimit(20, 60 * 60)


def check_login(username, password):
    if not PASSWORD:
        return False
    user_ok = hmac.compare_digest(username.encode(), USERNAME.encode())
    pass_ok = hmac.compare_digest(password.encode(), PASSWORD.encode())
    return user_ok and pass_ok


def cli(argv):
    """`clients` lists registered clients and their tokens; `revoke <client_id>`
    deletes a client and its tokens (it must register and sign in again);
    `scout-log [n]` prints the last n classify calls as JSON lines; `bench
    [repeat]` runs the Scout bench's cases from stdin (see bench())."""
    if argv[:1] == ["bench"] and len(argv) <= 2:
        return bench(sys.stdin, int(argv[1]) if len(argv) == 2 else 3)
    if argv[:1] == ["scout-log"] and len(argv) <= 2:
        n = int(argv[1]) if len(argv) == 2 else 20
        db = scout_db()
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        for row in reversed(rows):
            print(json.dumps(dict(row)))
        return 0
    db = auth_db()
    if argv[:1] == ["clients"]:
        rows = db.execute(
            "SELECT c.client_id, c.info, c.created_at, count(t.hash), max(t.last_used)"
            " FROM clients c LEFT JOIN tokens t USING (client_id)"
            " GROUP BY c.client_id ORDER BY c.created_at"
        ).fetchall()
        for cid, info, created, n, used in rows:
            name = json.loads(info).get("client_name") or "-"
            fmt = (
                lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
                if t
                else "never"
            )
            print(
                f"{cid}  {name!r}  registered {fmt(created)}  tokens {n}  last used {fmt(used)}"
            )
        return 0
    if len(argv) == 2 and argv[0] == "revoke":
        with db:
            n = db.execute(
                "DELETE FROM tokens WHERE client_id = ?", (argv[1],)
            ).rowcount
            c = db.execute(
                "DELETE FROM clients WHERE client_id = ?", (argv[1],)
            ).rowcount
        print(f"revoked {n} token(s); client {'deleted' if c else 'not found'}")
        return 0 if c else 1
    print(
        "usage: server.py [clients | revoke <client_id> | scout-log [n] | bench [repeat]]",
        file=sys.stderr,
    )
    return 2


# ---- the index -------------------------------------------------------------------


class Index:
    """The item vectors in memory, reloaded when the indexer commits."""

    def __init__(self):
        self.generation = None
        self.lock = threading.Lock()
        self.vecs = np.zeros((0, 768), dtype=np.float32)
        self.rows = []

    def current(self):
        db = connect(INDEX_DB)
        try:
            gen = db.execute(
                "SELECT value FROM meta WHERE key = 'generation'"
            ).fetchone()
            gen = gen[0] if gen else None
            with self.lock:
                if gen != self.generation:
                    rows = db.execute(
                        "SELECT i.id, i.kind, i.page, i.heading, i.body, i.vec,"
                        " d.id, d.source, d.path, d.drive_id"
                        " FROM items i JOIN docs d ON d.id = i.doc_id WHERE d.status = 'done'"
                    ).fetchall()
                    self.vecs = (
                        np.stack([unpack(r[5]) for r in rows])
                        if rows
                        else np.zeros((0, 768), dtype=np.float32)
                    )
                    self.rows = [
                        {
                            "item": r[0],
                            "kind": r[1],
                            "page": r[2],
                            "heading": r[3],
                            "body": r[4],
                            "doc": r[6],
                            "source": r[7],
                            "path": r[8],
                            "drive_id": r[9],
                        }
                        for r in rows
                    ]
                    self.generation = gen
                    log(f"index loaded: {len(rows)} items (generation {gen})")
                return self.vecs, self.rows
        finally:
            db.close()


INDEX = Index()


def wake_model_host():
    """Ask for the model host to be started, as an MQTT message on WAKE_TOPIC for
    whatever listens there (a home automation sending a wake-on-LAN packet, say).
    Not retained, so a broker restart can't replay it. True when the request was
    published; False when WAKE_MQTT_HOST is unset or the broker is unreachable."""
    if not WAKE_MQTT_HOST:
        return False
    try:
        mqtt_publish.single(WAKE_TOPIC, "hive", hostname=WAKE_MQTT_HOST)
        return True
    except OSError as e:
        log(f"wake request failed: {e}")
        return False


def query_vector(text):
    try:
        return embed([text], timeout=30)[0]
    except OllamaDown:
        if wake_model_host():
            raise ToolError(
                "Semantic search is unavailable: the model host, which embeds queries,"
                " was off, and it is being started now. Try again in about 2 minutes, or use the"
                " keyword search (forager_search) meanwhile."
            ) from None
        raise ToolError(
            "Semantic search is unavailable: the model host, which embeds queries, is"
            " off or not answering. Use keyword search (forager_search)"
            " instead, or try again once it is on."
        ) from None


def entry(row, score):
    out = {"source": row["source"], "path": row["path"]}
    if row["page"] is not None:
        out["page"] = row["page"]
    if row["heading"]:
        out["heading"] = row["heading"]
    if row["drive_id"]:
        out["link"] = f"https://drive.google.com/open?id={row['drive_id']}"
    out["similarity"] = round(float(score), 3)
    if row["body"]:
        body = " ".join(row["body"].split())
        out["excerpt"] = body[:EXCERPT] + ("..." if len(body) > EXCERPT else "")
    elif row["kind"] == "image":
        out["excerpt"] = "(picture or page with no text: forager_get_page shows it)"
    return out


def fuse(vecs, rows, q, mask, limit):
    """Reciprocal rank fusion over note chunks, page text, and page images: each
    kind ranks its own hits, since image scores run lower than text scores. A
    page's text and image merge into one entry."""
    scores = vecs @ q
    fused, best = {}, {}
    for kind in ("note", "text", "image"):
        idx = [i for i, r in enumerate(rows) if r["kind"] == kind and mask(r)]
        if not idx:
            continue
        idx = np.asarray(idx)
        top = idx[np.argsort(-scores[idx])[:PER_KIND]]
        for rank, i in enumerate(top):
            r = rows[i]
            key = (
                ("page", r["doc"], r["page"])
                if r["kind"] != "note"
                and (r["page"] is not None or r["kind"] == "image")
                else ("item", r["item"])
            )
            fused[key] = fused.get(key, 0.0) + 1 / (RRF_K + rank + 1)
            prev = best.get(key)
            if prev is None:
                best[key] = (r, scores[i])
            else:
                # Show the text half of a page when there is one: it has the excerpt.
                shown = r if r["body"] and not prev[0]["body"] else prev[0]
                best[key] = (shown, max(scores[i], prev[1]))
    order = sorted(fused, key=fused.get, reverse=True)[:limit]
    return [entry(*best[k]) for k in order]


def mirror_file(path):
    """A path from search results, resolved inside the mirror (no escaping it)."""
    root = MIRROR_DIR.resolve()
    file = (root / path).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        raise ToolError(
            f"No Drive file {path!r} in the mirror (use a path from forager_semantic_search)."
        )
    return file


def jpeg(img):
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


# ---- Scout -----------------------------------------------------------------------

SCOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    client_id TEXT,
    text TEXT NOT NULL,
    labels TEXT NOT NULL,          -- JSON list, in the order given
    instructions TEXT,
    answer TEXT,                   -- NULL when there was no answer (see error)
    expected TEXT,                 -- for scoring; never shown to the model
    latency_ms INTEGER,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    error TEXT                     -- too long | offline | bad output
);
CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
"""


def scout_db():
    db = sqlite3.connect(SCOUT_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCOUT_SCHEMA)
    return db


def scout_log(**call):
    """Record one classify call, and drop what is past the 90 days."""
    now = int(time.time())
    call["labels"] = json.dumps(call["labels"])
    db = scout_db()
    try:
        with db:
            db.execute("DELETE FROM calls WHERE at < ?", (now - SCOUT_KEEP,))
            db.execute(
                f"INSERT INTO calls (at, model, prompt_version, {', '.join(call)})"
                f" VALUES (?, ?, ?, {', '.join('?' * len(call))})",
                (now, SCOUT_MODEL, SCOUT_PROMPT, *call.values()),
            )
    finally:
        db.close()


def scout_prompt(text, labels, instructions):
    parts = [instructions.strip()] if instructions and instructions.strip() else []
    parts += [f"Labels: {', '.join(labels)}", f"Text:\n{text}"]
    return "\n\n".join(parts)


def caller():
    token = get_access_token()
    return token.client_id if token else None


# ---- the server ------------------------------------------------------------------


class Provider:
    """OAuthAuthorizationServerProvider backed by AUTH_DB. No refresh tokens:
    access tokens never expire and are cancelled with `revoke`."""

    async def get_client(self, client_id):
        db = auth_db()
        try:
            row = db.execute(
                "SELECT info FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        finally:
            db.close()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info):
        if registrations.retry_after():
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Too many registrations; try again later.",
            )
        registrations.record()
        db = auth_db()
        try:
            with db:
                db.execute(
                    "INSERT INTO clients VALUES (?, ?, ?)",
                    (
                        client_info.client_id,
                        client_info.model_dump_json(),
                        int(time.time()),
                    ),
                )
        finally:
            db.close()
        log(f"client registered: {client_info.client_id} ({client_info.client_name})")

    async def authorize(self, client, params: AuthorizationParams):
        login_id = secrets.token_urlsafe(24)
        db = auth_db()
        try:
            with db:
                db.execute(
                    "DELETE FROM logins WHERE expires_at < ?", (int(time.time()),)
                )
                db.execute(
                    "INSERT INTO logins VALUES (?, ?, ?, ?)",
                    (
                        login_id,
                        client.client_id,
                        params.model_dump_json(),
                        int(time.time()) + LOGIN_TTL,
                    ),
                )
        finally:
            db.close()
        return f"{PUBLIC_URL}/login?request={login_id}"

    async def load_authorization_code(self, client, authorization_code):
        db = auth_db()
        try:
            row = db.execute(
                "SELECT data FROM codes WHERE code = ?", (authorization_code,)
            ).fetchone()
        finally:
            db.close()
        if not row:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client, authorization_code):
        token = secrets.token_urlsafe(32)
        db = auth_db()
        try:
            with db:
                # Single use: a replayed code finds nothing.
                db.execute(
                    "DELETE FROM codes WHERE code = ?", (authorization_code.code,)
                )
                db.execute(
                    "INSERT INTO tokens VALUES (?, ?, ?, ?, NULL)",
                    (
                        token_hash(token),
                        client.client_id,
                        " ".join(authorization_code.scopes),
                        int(time.time()),
                    ),
                )
        finally:
            db.close()
        log(f"token issued to {client.client_id}")
        return OAuthToken(access_token=token, scope=" ".join(authorization_code.scopes))

    async def load_refresh_token(self, client, refresh_token):
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        raise NotImplementedError  # never reached: no refresh token loads

    async def load_access_token(self, token):
        h = token_hash(token)
        db = auth_db()
        try:
            row = db.execute(
                "SELECT client_id, scopes FROM tokens WHERE hash = ?", (h,)
            ).fetchone()
            if row:
                with db:
                    db.execute(
                        "UPDATE tokens SET last_used = ? WHERE hash = ?",
                        (int(time.time()), h),
                    )
        finally:
            db.close()
        if not row:
            return None
        return AccessToken(
            token=token, client_id=row[0], scopes=row[1].split(), expires_at=None
        )

    async def revoke_token(self, token):
        db = auth_db()
        try:
            with db:
                db.execute(
                    "DELETE FROM tokens WHERE hash = ?", (token_hash(token.token),)
                )
        finally:
            db.close()


server = MCPServer(
    name="hive",
    instructions=(
        "The Hive, the door to the user's own server for agents. Semantic search over"
        " the user's Obsidian vault and, if mirrored, Google Drive: use"
        " forager_semantic_search to find notes and Drive pages by meaning, forager_related_notes for"
        " notes close to a given one, and forager_get_page to look at a Drive page or picture."
        " Read a note with forager_read, by the path returned here; forager_search finds"
        " exact words, forager_search_frontmatter finds notes by a frontmatter field,"
        " and the other forager_ tools write. Before writing to the vault, read"
        f" {CONVENTIONS} (forager_read) if it exists; it is the single source of"
        " truth for layout, frontmatter, tags and templates. Search before creating"
        " a note."
        " scout_classify asks Scout, a small local model, to pick one"
        " label for a text. Its bench has passed one kind only: picking which of the"
        " given candidates a text names (a date, a machine, a person, a number), with"
        " the candidates as labels. Use it for that kind, or when asked to; decide"
        " other questions yourself."
    ),
    auth_server_provider=Provider(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL),
        resource_server_url=AnyHttpUrl(PUBLIC_URL + "/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=False,
    ),
)


PAGE_STYLE = """<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;
color:#1f2328;background:#fff}
@media (prefers-color-scheme:dark){body{color:#e6edf3;background:#0d1117}
input{background:#161b22;color:#e6edf3;border-color:#30363d}}
input{display:block;width:100%;box-sizing:border-box;padding:.5rem;margin:.2rem 0 .8rem;
border:1px solid #d0d7de;border-radius:6px;font:inherit}
button{padding:.5rem 1.2rem;font:inherit;border-radius:6px;border:0;background:#1f6feb;color:#fff}
.err{color:#cf222e}</style>"""


def page(title, body, status=200, headers=None):
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>{PAGE_STYLE}</head><body>{body}</body></html>",
        status_code=status,
        headers=headers,
    )


@server.custom_route("/", methods=["GET"])
async def home(request: Request) -> Response:
    return page(
        "hive",
        "<h1>hive</h1><p>A private search service for one person's notes and"
        " Google Drive, used through Claude. It reads Drive with read-only access and"
        " never changes or shares anything. There is nothing to sign up for here.</p>"
        "<p><a href=/privacy>Privacy policy</a></p>",
    )


@server.custom_route("/privacy", methods=["GET"])
async def privacy(request: Request) -> Response:
    return page(
        "Privacy policy",
        "<h1>Privacy policy</h1><p>hive is run by one person for their own use."
        " With read-only Google Drive access, it copies that person's Drive files to"
        " their own server and builds a search index there. Pages and text are"
        " sent to a computer on the same private network to compute the index; nothing"
        " is sent to any other party, sold, or used for advertising.</p>"
        "<p>Only the owner can sign in. Removing the app at"
        " <a href='https://myaccount.google.com/connections'>Google account"
        " connections</a> ends Drive access; the copy and index are deleted with the"
        " service.</p>",
    )


def login_form(request_id, error="", status=200, headers=None):
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    return page(
        "Sign in: hive",
        f"<h1>Sign in</h1><p>An app asks to use the Hive: your vault, Drive and"
        f" Scout.</p>{err}"
        f"<form method=post action=/login><input type=hidden name=request"
        f" value='{html.escape(request_id)}'>"
        f"<label>Username<input name=username autocomplete=username required></label>"
        f"<label>Password<input name=password type=password"
        f" autocomplete=current-password required></label>"
        f"<button>Allow</button></form>",
        status,
        headers,
    )


@server.custom_route("/login", methods=["GET", "POST"])
async def login(request: Request) -> Response:
    form = await request.form() if request.method == "POST" else request.query_params
    request_id = str(form.get("request", ""))
    db = auth_db()
    try:
        row = db.execute(
            "SELECT client_id, params FROM logins WHERE id = ? AND expires_at >= ?",
            (request_id, int(time.time())),
        ).fetchone()
        if not row:
            return page(
                "Expired",
                "<p>This sign-in link has expired. Start again from the app.</p>",
                400,
            )
        if not PASSWORD:
            log("refusing to sign in: VAULT_OAUTH_PASSWORD is not set")
            return page("Not configured", "<p>Sign-in is not configured.</p>", 503)
        if request.method == "GET":
            return login_form(request_id)

        wait = login_failures.retry_after()
        if wait:
            # Refuse even the right password, or the brake only slows a guesser.
            log(f"sign-in refused: too many failures, {wait}s left")
            return login_form(
                request_id,
                f"Too many failed sign-ins. Try again in {max(1, wait // 60)} minute(s).",
                429,
                {"Retry-After": str(wait)},
            )
        if not check_login(
            str(form.get("username", "")), str(form.get("password", ""))
        ):
            login_failures.record()
            log("sign-in failed")
            return login_form(request_id, "Incorrect username or password.")

        client_id, params = row[0], AuthorizationParams.model_validate_json(row[1])
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        with db:
            db.execute("DELETE FROM logins WHERE id = ?", (request_id,))
            db.execute("DELETE FROM codes WHERE expires_at < ?", (int(time.time()),))
            db.execute(
                "INSERT INTO codes VALUES (?, ?, ?)",
                (code.code, code.model_dump_json(), int(code.expires_at)),
            )
    finally:
        db.close()
    log(f"signed in for {client_id}")
    return RedirectResponse(
        construct_redirect_uri(
            str(params.redirect_uri), code=code.code, state=params.state
        ),
        status_code=302,
    )


@server.tool(annotations={"readOnlyHint": True})
def forager_semantic_search(query: str, limit: int = 10, source: str = "all") -> dict:
    """Find notes and Google Drive pages about a topic, by meaning rather than exact
    words. Each result is a note chunk (with its heading) or a Drive page (PDFs,
    Docs, Slides by page; Sheets and text files by chunk; pictures), with a
    similarity score, an excerpt, and for Drive a link.

    Results always come back, relevant or not: judge by similarity. On-topic text
    scored about 0.8 in testing and unrelated text about 0.6; pictures and
    page images score about 0.1 lower than text.

    query: what to look for, in plain words.
    limit: results to return (1 to 50).
    source: "all", "notes", or "drive".
    """
    if source not in ("all", "notes", "drive"):
        raise ToolError('source must be "all", "notes", or "drive".')
    vecs, rows = INDEX.current()
    if not rows:
        raise ToolError("The index is empty: the indexer has not run yet.")
    q = query_vector(query)
    want = {"all": None, "notes": "note", "drive": "drive"}[source]
    results = fuse(
        vecs,
        rows,
        q,
        lambda r: want is None or r["source"] == want,
        max(1, min(limit, 50)),
    )
    return {"results": results}


@server.tool(annotations={"readOnlyHint": True})
def forager_related_notes(path: str, limit: int = 10) -> dict:
    """Notes closest in meaning to a given note, for suggesting links. Does not
    need the model host.

    path: the note's path in the vault, as forager_semantic_search returns it
          (for example "projects/idea.md").
    limit: notes to return (1 to 50).
    """
    vecs, rows = INDEX.current()
    mine = [
        i for i, r in enumerate(rows) if r["source"] == "note" and r["path"] == path
    ]
    if not mine:
        raise ToolError(f"No indexed note {path!r} (new notes are indexed nightly).")
    q = vecs[mine].mean(axis=0)
    q /= np.linalg.norm(q)
    scores = vecs @ q
    best = {}
    for i, r in enumerate(rows):
        if r["source"] == "note" and r["path"] != path:
            if r["path"] not in best or scores[i] > best[r["path"]][1]:
                best[r["path"]] = (r, scores[i])
    top = sorted(best.values(), key=lambda b: b[1], reverse=True)[
        : max(1, min(limit, 50))
    ]
    return {"results": [entry(r, s) for r, s in top]}


@server.tool(annotations={"readOnlyHint": True})
def forager_get_page(path: str, page: int | None = None) -> list:
    """Look at a Drive page or picture: returns it as an image, to read a figure,
    slide, or scan. Does not need the model host.

    path: the Drive path, as forager_semantic_search returns it.
    page: the page number for a PDF, Doc, or Slides file (from 1); omit for a picture.
    """
    file = mirror_file(path)
    ext = file.suffix.lower()
    if ext == ".pdf":
        pdf = pdfium.PdfDocument(file)
        try:
            n = len(pdf)
            if page is None or not 1 <= page <= n:
                raise ToolError(f"{path} has {n} page(s): give a page from 1 to {n}.")
            p = pdf[page - 1]
            data = jpeg(p.render(scale=DPI / 72).to_pil())
            p.close()
        finally:
            pdf.close()
        return [f"{path}, page {page} of {n}", Image(data=data, format="jpeg")]
    try:
        with PILImage.open(file) as img:
            data = jpeg(img)
    except OSError:
        raise ToolError(
            f"{path} is not a PDF or a picture: open it from Drive."
        ) from None
    return [path, Image(data=data, format="jpeg")]


@server.tool()
def scout_classify(
    text: str,
    labels: list[str],
    instructions: str | None = None,
    expected: str | None = None,
) -> dict:
    """Ask Scout, a small local model, to pick one label
    for a text: a closed question (yes/no, pick a category, which of these)
    answered in a fraction of a second. No confidence score: a model this size isn't
    calibrated. Every call is logged for 90 days with its text, so leave out
    anything sensitive.

    If the model host is off, this answers at once that Scout is offline (and
    asks for it to be started): decide yourself rather than wait.

    text: what to judge. Too long for Scout's context is an error, never cut.
    labels: 2 to 10 distinct answers to choose from, for example ["yes", "no"].
    instructions: the question or criteria, in plain words (optional).
    expected: the right label when it is known (a bench case): logged for
              scoring, never shown to the model.
    """
    label, latency_ms = scout_ask(text, labels, instructions, expected, caller())
    return {"label": label, "latency_ms": latency_ms}


def scout_ask(text, labels, instructions=None, expected=None, client_id=None):
    """classify's work, shared with `bench`: returns (label, latency_ms) and logs
    the call, or logs it and raises ToolError."""
    labels = [str(label).strip() for label in labels]
    if not 2 <= len(labels) <= 10 or "" in labels or len(set(labels)) != len(labels):
        raise ToolError("labels: give 2 to 10 distinct, non-empty labels.")
    if expected is not None and expected not in labels:
        raise ToolError("expected must be one of the labels.")
    prompt = scout_prompt(text, labels, instructions)
    call = {
        "client_id": client_id,
        "text": text,
        "labels": labels,
        "instructions": instructions,
        "expected": expected,
    }
    # Prose runs about 4 characters a token; 3 is a conservative bound, and
    # Ollama's own count is checked below.
    if (len(SCOUT_SYSTEM) + len(prompt)) / 3 > SCOUT_CTX - 64:
        scout_log(**call, error="too long")
        raise ToolError(
            f"Too long for Scout ({len(prompt)} characters; about"
            f" {3 * (SCOUT_CTX - 64)} fit): judge it yourself, or ask about a shorter part."
        )
    start = time.monotonic()
    try:
        reply = chat(
            {
                "model": SCOUT_MODEL,
                "messages": [
                    {"role": "system", "content": SCOUT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "format": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                    "required": ["label"],
                },
                "think": False,
                "keep_alive": -1,
                "options": {"num_ctx": SCOUT_CTX, "temperature": 0},
            },
            timeout=SCOUT_TIMEOUT,
        )
    except OllamaDown:
        woken = wake_model_host()
        scout_log(**call, error="offline")
        raise ToolError(
            "Scout offline; decide yourself."
            + (" (The model host is being started.)" if woken else "")
        ) from None
    latency_ms = round((time.monotonic() - start) * 1000)
    if reply.get("prompt_eval_count", 0) >= SCOUT_CTX - 16:
        scout_log(**call, latency_ms=latency_ms, error="too long")
        raise ToolError("Too long for Scout's context: judge it yourself.")
    try:
        label = json.loads(reply["message"]["content"])["label"]
    except (KeyError, TypeError, ValueError):
        label = None
    if label not in labels:
        scout_log(**call, latency_ms=latency_ms, error="bad output")
        raise ToolError("Scout gave no usable answer; decide yourself.")
    scout_log(**call, answer=label, latency_ms=latency_ms)
    return label, latency_ms


def bench(stream, repeat=3):
    """Run the Scout bench (scout/): YAML documents on `stream`, each
    `kind`, `scored` (default true) and `questions`, every question with
    `labels`, optional `instructions` and `cases` of `{text, expected}`. Each
    case is asked `repeat` times and logged as client `bench`. A case is right
    only if every repeat is; it flips if the repeats disagree. Prints the misses
    and one line per kind; exits 0 when every scored kind passes."""
    import yaml

    kinds = {}
    for doc in yaml.safe_load_all(stream):
        if not doc:
            continue
        kind = kinds.setdefault(
            doc["kind"], {"scored": doc.get("scored", True), "results": []}
        )
        for q in doc["questions"]:
            for case in q["cases"]:
                answers, ms = [], []
                for _ in range(repeat):
                    try:
                        label, latency = scout_ask(
                            case["text"],
                            q["labels"],
                            q.get("instructions"),
                            case["expected"],
                            "bench",
                        )
                    except ToolError as e:
                        if "offline" in str(e):
                            print(f"Scout offline: {e}", file=sys.stderr)
                            return 3
                        label, latency = f"error: {e}", None
                    answers.append(label)
                    ms.append(latency)
                kind["results"].append((case, answers, ms))
    ok_all = True
    for name, kind in kinds.items():
        results = kind["results"]
        right = sum(all(a == c["expected"] for a in ans) for c, ans, _ in results)
        flips = sum(len(set(ans)) > 1 for _, ans, _ in results)
        times = sorted(t for _, _, ms in results for t in ms if t is not None)
        for case, ans, _ in results:
            if any(a != case["expected"] for a in ans):
                text = " ".join(case["text"].split())[:90]
                print(f"  miss {name}: expected {case['expected']}, got {ans}: {text}")
        accuracy = right / len(results)
        passed = len(results) >= BENCH_MIN and accuracy >= BENCH_BAR and not flips
        verdict = ("PASS" if passed else "FAIL") if kind["scored"] else "EVIDENCE"
        ok_all &= passed or not kind["scored"]
        median = times[len(times) // 2] if times else None
        print(
            f"{name:10} {verdict:8} {right}/{len(results)} right ({accuracy:.0%}),"
            f" {flips} flipped, median {median} ms  [{SCOUT_MODEL}, {SCOUT_PROMPT},"
            f" x{repeat}]"
        )
    return 0 if ok_all else 1


vault.register(server, caller, writes=os.environ.get("VAULT_WRITES", "1") == "1")


if __name__ == "__main__" and len(sys.argv) > 1:
    sys.exit(cli(sys.argv[1:]))

if __name__ == "__main__":
    import uvicorn

    host = PUBLIC_URL.split("://", 1)[1]
    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=[host, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[PUBLIC_URL, "https://claude.ai", "http://localhost:*"],
        ),
        host="0.0.0.0",
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
