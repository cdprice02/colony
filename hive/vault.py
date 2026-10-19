"""Forager's vault tools: read and write an Obsidian vault on disk (kept in sync
beside the server by Obsidian's own sync client, or any other), prefixed
forager_ like the rest of Forager's.

Writes are atomic (a temporary file, then a rename), land in the vault the sync
client watches, and are recorded one JSON line each in AUDIT_LOG, outside the
vault so it can be backed up on its own. .obsidian, .git and .trash are not
notes, so the tools never touch them. Deleting moves a file to .trash/, as
Obsidian does.
"""

import difflib
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "/data/vault"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/data/audit/hive.jsonl"))

HIDDEN = {".obsidian", ".git", ".trash"}
TEXT = {".md", ".canvas", ".base", ".txt", ".json", ".csv", ".yaml", ".yml"}
MAX_READ = 1_000_000  # bytes; the vault is markdown, attachments live in Drive
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
WIKILINK = re.compile(r"\[\[([^\]|#^]+)([#^][^\]|]*)?(\|[^\]]*)?\]\]")

_audit_lock = threading.Lock()
_fm_cache = {}  # path -> (mtime_ns, frontmatter)


# ---- paths and files -----------------------------------------------------------


def root():
    return VAULT_DIR.resolve()


def resolve(path, must_exist=True):
    """A vault-relative path, inside the vault and outside its hidden folders."""
    rel = str(path).strip().lstrip("/")
    if not rel:
        raise ToolError("Give a path inside the vault, for example notes/idea.md.")
    full = (root() / rel).resolve()
    if not full.is_relative_to(root()) or full == root():
        raise ToolError(f"{path!r} is outside the vault.")
    if HIDDEN & set(full.relative_to(root()).parts):
        raise ToolError(
            f"{path!r} is in .obsidian, .git or .trash, which aren't notes."
        )
    if must_exist and not full.exists():
        raise ToolError(f"No file {rel!r} in the vault.")
    return full


def rel(full):
    return full.relative_to(root()).as_posix()


def visible(full):
    return not HIDDEN & set(full.relative_to(root()).parts)


def notes(prefix=None, pattern=None):
    """Every text file under prefix (vault-relative), sorted, hidden folders skipped."""
    base = resolve(prefix) if prefix else root()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in HIDDEN)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.suffix.lower() in TEXT and (
                not pattern or fnmatch.fnmatch(name, pattern)
            ):
                yield full


def read_text(full):
    if not full.is_file():
        raise ToolError(f"{rel(full)!r} is a folder: use forager_list.")
    if full.suffix.lower() not in TEXT:
        raise ToolError(f"{rel(full)!r} isn't a text file.")
    size = full.stat().st_size
    if size > MAX_READ:
        raise ToolError(f"{rel(full)!r} is {size} bytes, over the {MAX_READ} limit.")
    return full.read_text(encoding="utf-8", errors="replace")


def split_frontmatter(text):
    m = FRONTMATTER.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    return (fm if isinstance(fm, dict) else None), text[m.end() :]


def join_frontmatter(fm, body):
    if not fm:
        return body
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{dumped}\n---\n{body}"


def frontmatter(full):
    mtime = full.stat().st_mtime_ns
    hit = _fm_cache.get(full)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        fm, _ = split_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        fm = None
    _fm_cache[full] = (mtime, fm)
    return fm


def jsonable(value):
    """Frontmatter values for a JSON reply: dates become ISO strings."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def write_atomic(full, text, create_dirs=True):
    if not full.parent.exists():
        if not create_dirs:
            raise ToolError(f"No folder {rel(full.parent)!r} (pass create_dirs=true).")
        full.parent.mkdir(parents=True)
    fd, tmp = tempfile.mkstemp(dir=full.parent, prefix=".hive-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o664)
        os.replace(tmp, full)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---- the audit log -------------------------------------------------------------


def audited(operation, caller, run, **where):
    """Run one write, recording it (or its failure) in AUDIT_LOG either way."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "operation": operation,
        "client_id": caller(),
        **where,
    }
    try:
        result, before, after = run()
        entry.update(
            operation_status="success",
            error=None,
            checksum_before=sha(before),
            checksum_after=sha(after),
            size_before=len(before.encode()) if before is not None else None,
            size_after=len(after.encode()) if after is not None else None,
        )
        return result
    except ToolError as e:
        entry.update(operation_status="error", error=str(e))
        raise
    finally:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock, AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


# ---- daily notes ---------------------------------------------------------------

MOMENT = [  # longest first; the subset the vault's formats and templates use
    ("YYYY", "%Y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
    ("dddd", "%A"), ("ddd", "%a"), ("DD", "%d"), ("Do", None), ("D", None),
]  # fmt: skip


def moment(fmt, day):
    out, i = "", 0
    while i < len(fmt):
        for token, code in MOMENT:
            if fmt.startswith(token, i):
                if token == "Do":
                    n = day.day
                    suffix = (
                        "th"
                        if 11 <= n % 100 <= 13
                        else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
                    )
                    out += f"{n}{suffix}"
                elif token == "D":
                    out += str(day.day)
                else:
                    out += day.strftime(code)
                i += len(token)
                break
        else:
            out += fmt[i]
            i += 1
    return out


def daily_settings():
    """Obsidian's own daily-notes settings, so the path matches the app's."""
    try:
        s = json.loads((root() / ".obsidian" / "daily-notes.json").read_text())
    except (OSError, ValueError):
        s = {}
    return s.get("folder", ""), s.get("format") or "YYYY-MM-DD", s.get("template", "")


def daily_path(day):
    folder, fmt, _ = daily_settings()
    name = moment(fmt, day) + ".md"
    return (root() / folder / name) if folder else root() / name


def daily_template(day):
    """The daily template with its tp.date.now(...) calls filled in for day; any
    other Templater call is dropped, since Templater never sees this file."""
    _, _, template = daily_settings()
    if not template:
        return ""
    try:
        text = (root() / f"{template.removesuffix('.md')}.md").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""

    def fill(m):
        call = re.match(r'\s*tp\.date\.now\(\s*"([^"]*)"', m.group(1))
        return moment(call.group(1), day) if call else ""

    return re.sub(r"<%[-_]?\s*(.*?)\s*[-_]?%>", fill, text, flags=re.DOTALL)


def today():
    return datetime.now().date()  # in the server's TZ


# ---- links ---------------------------------------------------------------------


def repoint_links(moves):
    """Rewrite wikilinks that pointed at a moved note, as Obsidian does on a
    rename. moves maps old vault-relative paths (without .md) to new ones.
    Returns {path: (before, after)} for the notes that would change."""
    stems = {}
    for full in notes():
        if full.suffix == ".md":
            stems.setdefault(full.stem, []).append(full)
    old_names = {old.rsplit("/", 1)[-1]: old for old in moves}
    changed = {}
    for full in notes():
        if full.suffix != ".md":
            continue
        before = full.read_text(encoding="utf-8", errors="replace")

        def fix(m):
            target = m.group(1).strip()
            new = moves.get(target)
            bare = "/" not in target and target in old_names
            if new is None and bare and len(stems.get(target, [])) <= 1:
                # An unambiguous bare name stays bare unless another note
                # already has the new name.
                new = moves[old_names[target]]
                new_stem = new.rsplit("/", 1)[-1]
                clash = [
                    p
                    for p in stems.get(new_stem, [])
                    if rel(p)[:-3] != old_names[target]
                ]
                new = new if clash else new_stem
            if new is None:
                return m.group(0)
            return f"[[{new}{m.group(2) or ''}{m.group(3) or ''}]]"

        after = WIKILINK.sub(fix, before)
        if after != before:
            changed[rel(full)] = (before, after)
    return changed


# ---- the tools -----------------------------------------------------------------

RO = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
RW = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


def register(server, caller, writes):
    """Add the read tools to server, and the write tools when writes is true.
    caller() names the signed-in client for the audit log."""

    @server.tool(annotations=RO)
    def forager_read(path: str) -> dict:
        """Read a note from the Obsidian vault: its content, size, modified time,
        and parsed YAML frontmatter."""
        full = resolve(path)
        text = read_text(full)
        fm, _ = split_frontmatter(text)
        st = full.stat()
        return {
            "path": rel(full),
            "content": text,
            "frontmatter": jsonable(fm),
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec="seconds"
            ),
        }

    @server.tool(annotations=RO)
    def forager_batch_read(paths: list[str], include_content: bool = True) -> dict:
        """Read several notes in one call; a missing one is reported, not fatal.
        include_content=false returns frontmatter and sizes only."""
        out = []
        for p in paths[:50]:
            try:
                note = forager_read(p)
                if not include_content:
                    note.pop("content")
                out.append(note)
            except ToolError as e:
                out.append({"path": p, "error": str(e)})
        return {"notes": out}

    @server.tool(annotations=RO)
    def forager_list(
        path: str = "",
        depth: int = 1,
        include_files: bool = True,
        include_dirs: bool = True,
        pattern: str | None = None,
    ) -> dict:
        """List a vault folder ("" for the root) to a depth (1 to 5). pattern is a
        glob on names, for example "*.md". Skips .obsidian, .git and .trash."""
        base = resolve(path) if path.strip("/") else root()
        if not base.is_dir():
            raise ToolError(f"{path!r} is a file: use forager_read.")
        depth = max(1, min(depth, 5))
        entries = []
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            level = len(here.relative_to(base).parts)
            dirnames[:] = sorted(d for d in dirnames if d not in HIDDEN)
            if include_dirs:
                entries += [
                    {"path": rel(here / d), "type": "dir"}
                    for d in dirnames
                    if not pattern or fnmatch.fnmatch(d, pattern)
                ]
            if include_files:
                entries += [
                    {
                        "path": rel(here / f),
                        "type": "file",
                        "size": (here / f).stat().st_size,
                    }
                    for f in sorted(filenames)
                    if not pattern or fnmatch.fnmatch(f, pattern)
                ]
            if level + 1 >= depth:
                dirnames[:] = []
            if len(entries) > 2000:
                return {"entries": entries[:2000], "truncated": True}
        return {"entries": entries}

    @server.tool(annotations=RO)
    def forager_search(
        query: str,
        path_prefix: str | None = None,
        file_pattern: str | None = None,
        max_results: int = 20,
        context_lines: int = 2,
    ) -> dict:
        """Keyword search (case-insensitive) over note names and contents.
        Filename matches come first, then content matches with context lines
        and the note's frontmatter. For search by meaning, use forager_semantic_search."""
        q = query.strip().lower()
        if not q:
            raise ToolError("Give something to search for.")
        max_results = max(1, min(max_results, 100))
        context_lines = max(0, min(context_lines, 10))
        by_name, by_content = [], []
        for full in notes(path_prefix, file_pattern):
            path = rel(full)
            if q in path.lower():
                by_name.append({"path": path, "match_type": "filename"})
            if len(by_content) >= max_results:
                continue
            try:
                lines = read_text(full).splitlines()
            except ToolError:
                continue
            hits = [i for i, line in enumerate(lines) if q in line.lower()]
            if hits:
                i = hits[0]
                lo, hi = (
                    max(0, i - context_lines),
                    min(len(lines), i + context_lines + 1),
                )
                by_content.append(
                    {
                        "path": path,
                        "match_type": "content",
                        "line": i + 1,
                        "matches": len(hits),
                        "context": "\n".join(lines[lo:hi]),
                        "frontmatter": jsonable(frontmatter(full)),
                    }
                )
        return {"results": (by_name + by_content)[:max_results]}

    @server.tool(annotations=RO)
    def forager_search_frontmatter(
        field: str,
        value: str = "",
        match_type: str = "exact",
        path_prefix: str | None = None,
        max_results: int = 20,
    ) -> dict:
        """Find notes by a frontmatter field. match_type: "exact" (the value, or a
        list holding it), "contains" (substring, case-insensitive), or "exists"
        (the field is present; value ignored). Example: field "type", value
        "knowledge"; or field "tags", value "topic/rust", match_type "contains"."""
        if match_type not in ("exact", "contains", "exists"):
            raise ToolError('match_type must be "exact", "contains" or "exists".')
        max_results = max(1, min(max_results, 200))
        want = value.strip().lower()

        def hit(v):
            if match_type == "exists":
                return True
            items = v if isinstance(v, list) else [v]
            texts = [str(jsonable(i)).lower() for i in items if i is not None]
            if match_type == "exact":
                return want in texts
            return any(want in t for t in texts)

        out = []
        for full in notes(path_prefix, "*.md"):
            fm = frontmatter(full)
            if fm and field in fm and hit(fm[field]):
                out.append({"path": rel(full), "frontmatter": jsonable(fm)})
                if len(out) >= max_results:
                    break
        return {"results": out}

    @server.tool(annotations=RO)
    def forager_daily_note_read() -> dict:
        """Read today's daily note (the server's date, at the path Obsidian's
        daily-notes settings give). Says so, without creating it, if it's
        missing."""
        full = daily_path(today())
        if not full.exists():
            raise ToolError(f"No daily note yet at {rel(full)!r}.")
        return forager_read(rel(full))

    if not writes:
        return

    @server.tool(annotations=RW)
    def forager_write(
        path: str,
        content: str,
        overwrite: bool = False,
        merge_frontmatter: bool = False,
        create_dirs: bool = True,
    ) -> dict:
        """Write a whole note. Creates it, and refuses to replace an existing note
        unless overwrite=true; for small changes to an existing note use
        forager_edit or forager_append instead. merge_frontmatter=true keeps the
        existing note's frontmatter fields and adds or replaces those in content."""

        def run():
            full = resolve(path, must_exist=False)
            if full.is_dir():
                raise ToolError(f"{path!r} is a folder.")
            if full.suffix.lower() not in TEXT:
                raise ToolError(f"{path!r} isn't a text file (end it in .md).")
            before = read_text(full) if full.exists() else None
            if before is not None and not overwrite:
                raise ToolError(
                    f"{rel(full)!r} already exists: pass overwrite=true to replace it,"
                    " use forager_edit or forager_append, or choose another name."
                )
            text = content
            if merge_frontmatter and before is not None:
                old, _ = split_frontmatter(before)
                new, body = split_frontmatter(content)
                text = join_frontmatter({**(old or {}), **(new or {})}, body)
            write_atomic(full, text, create_dirs)
            return (
                {
                    "path": rel(full),
                    "created": before is None,
                    "size": len(text.encode()),
                },
                before,
                text,
            )

        return audited("forager_write", caller, run, target_path=path)

    @server.tool(annotations=RW)
    def forager_edit(path: str, edits: list[dict], dry_run: bool = False) -> dict:
        """Change parts of an existing note by exact text replacement, without
        resending it. edits is a list of {"old_text": ..., "new_text": ...};
        each old_text must appear exactly once in the note as the earlier edits
        leave it. dry_run=true returns the diff and writes nothing."""

        def run():
            full = resolve(path)
            before = read_text(full)
            text = before
            for n, e in enumerate(edits, 1):
                old, new = e.get("old_text"), e.get("new_text")
                if not isinstance(old, str) or not isinstance(new, str) or not old:
                    raise ToolError(
                        f"edit {n}: give old_text (non-empty) and new_text."
                    )
                count = text.count(old)
                if count != 1:
                    raise ToolError(
                        f"edit {n}: old_text appears {count} times; it must appear once."
                    )
                text = text.replace(old, new, 1)
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(True), text.splitlines(True), rel(full), rel(full)
                )
            )
            if not dry_run:
                write_atomic(full, text)
            return (
                {"path": rel(full), "applied": not dry_run, "diff": diff},
                before,
                text,
            )

        if dry_run:
            return run()[0]
        return audited("forager_edit", caller, run, target_path=path)

    def append(full, content, separator, create_dirs, seed=""):
        before = read_text(full) if full.exists() else None
        base = before if before is not None else seed
        text = (base.rstrip("\n") + separator + content) if base.strip() else content
        if not text.endswith("\n"):
            text += "\n"
        write_atomic(full, text, create_dirs)
        return {"path": rel(full), "created": before is None}, before, text

    @server.tool(annotations=RW)
    def forager_append(
        path: str, content: str, separator: str = "\n\n", create_dirs: bool = True
    ) -> dict:
        """Add content to the end of a note without resending it; creates the
        note if it doesn't exist."""

        def run():
            full = resolve(path, must_exist=False)
            if full.suffix.lower() not in TEXT:
                raise ToolError(f"{path!r} isn't a text file (end it in .md).")
            return append(full, content, separator, create_dirs)

        return audited("forager_append", caller, run, target_path=path)

    @server.tool(annotations=RW)
    def forager_daily_note_append(content: str) -> dict:
        """Add content to the end of today's daily note, creating it from the
        vault's daily template if it doesn't exist yet."""
        day = today()
        full = daily_path(day)

        def run():
            body = content.rstrip("\n")
            return append(full, body, "\n\n", True, seed=daily_template(day))

        return audited("forager_daily_note_append", caller, run, target_path=rel(full))

    @server.tool(annotations=RW)
    def forager_move(
        source: str, destination: str, create_dirs: bool = True, dry_run: bool = False
    ) -> dict:
        """Move or rename a note or folder. Wikilinks that pointed at a moved note
        are repointed, as Obsidian does on a rename. dry_run=true returns the
        notes and links that would change, and moves nothing."""

        def run():
            src = resolve(source)
            dst = resolve(destination, must_exist=False)
            if dst.exists():
                raise ToolError(f"{rel(dst)!r} already exists.")
            if dst.is_relative_to(src):
                raise ToolError("Can't move a folder into itself.")
            files = (
                [src]
                if src.is_file()
                else [p for p in src.rglob("*") if p.is_file() and visible(p)]
            )
            moves = {}
            for f in files:
                if f.suffix == ".md":
                    new = dst / f.relative_to(src) if src.is_dir() else dst
                    moves[rel(f)[:-3]] = (
                        new.relative_to(root()).as_posix().removesuffix(".md")
                    )
            changed = repoint_links(moves) if moves else {}
            summary = {
                "source": rel(src),
                "destination": rel(dst),
                "moved": not dry_run,
                "links_repointed": {
                    p: "".join(
                        difflib.unified_diff(
                            b.splitlines(True), a.splitlines(True), p, p
                        )
                    )
                    for p, (b, a) in changed.items()
                },
            }
            if dry_run:
                return summary, None, None
            if not dst.parent.exists():
                if not create_dirs:
                    raise ToolError(
                        f"No folder {rel(dst.parent)!r} (pass create_dirs=true)."
                    )
                dst.parent.mkdir(parents=True)
            os.replace(src, dst)
            for p, (_, after) in changed.items():
                # A note that moved itself is now at its new place.
                old = p[:-3]
                target = root() / (moves[old] + ".md") if old in moves else root() / p
                write_atomic(target, after)
            return summary, None, None

        if dry_run:
            return run()[0]
        return audited(
            "forager_move", caller, run, target_path=source, destination=destination
        )

    @server.tool(annotations=RW)
    def forager_delete(path: str, confirm: bool = False) -> dict:
        """Delete a note by moving it to the vault's .trash/ folder, as Obsidian
        does; nothing is erased. Requires confirm=true."""

        def run():
            if not confirm:
                raise ToolError("Pass confirm=true to delete (it moves to .trash/).")
            full = resolve(path)
            if not full.is_file():
                raise ToolError(f"{path!r} is a folder: delete its notes one by one.")
            before = read_text(full) if full.suffix.lower() in TEXT else None
            trash = root() / ".trash" / full.relative_to(root())
            n = 1
            while trash.exists():
                trash = trash.with_name(f"{full.stem} {n}{full.suffix}")
                n += 1
            trash.parent.mkdir(parents=True, exist_ok=True)
            os.replace(full, trash)
            return (
                {"path": rel(full), "trashed_to": trash.relative_to(root()).as_posix()},
                before,
                None,
            )

        return audited("forager_delete", caller, run, target_path=path)
