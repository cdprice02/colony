# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.2.6", "pillow==12.2.0", "pypdfium2==5.14.0"]
# ///
"""The Hive's nightly indexer.

Embeds what changed since the last run, file by file, through Ollama:
notes in chunks at headings, and the rclone mirror of Drive (each PDF page as an
image and as text, pictures as images, CSV and text files in chunks). Each file
commits on its own, so a run cut short by the deadline or by the model host going away
loses at most the file in progress; the next run carries on.

Environment:
  VAULT_DIR      the vault (read-only)                     /vault
  MIRROR_DIR     rclone's mirror of Drive (optional)       /index/drive
                 (absent: notes only)
  LISTING        `rclone lsjson -R` of Drive, same filters /index/drive.json
                 (absent: walk MIRROR_DIR, no Drive links; for local runs)
  DRIVE_ERROR    a marker the mirror job leaves on failure /index/drive-error
  INDEX_DB       the SQLite index                          /index/index.db
  STOP_AT        local time to stop starting new files     07:00
                 (already past, as in a run by hand: two hours from now)
"""

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from common import MODEL, OllamaDown, connect, embed, ollama_up, pack

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "/vault"))
MIRROR_DIR = Path(os.environ.get("MIRROR_DIR", "/index/drive"))
LISTING = Path(os.environ.get("LISTING", "/index/drive.json"))
INDEX_DB = os.environ.get("INDEX_DB", "/index/index.db")
DRIVE_ERROR = Path(os.environ.get("DRIVE_ERROR", "/index/drive-error"))


def deadline(stop_at):
    """Seconds on the monotonic clock at which to stop starting new files."""
    h, m = map(int, stop_at.split(":"))
    left = (datetime.now().replace(hour=h, minute=m, second=0) - datetime.now()).total_seconds()
    return time.monotonic() + (left if left > 0 else 2 * 3600)


DEADLINE = deadline(os.environ.get("STOP_AT", "07:00"))

MAX_BYTES = 100 * 1024 * 1024
CHUNK_CHARS = 2000  # about 500 tokens; long sections split at paragraphs
TEXT_BATCH = 16
IMAGE_BATCH = 4
DPI = 150  # tested: the right page ranked first at 150 dpi
MAX_SIDE = 1700
MIN_PAGE_TEXT = 20  # below this a page is a figure or a scan: image only

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_EXT = {".csv", ".txt", ".md"}


def log(*a):
    print(*a, flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- sources ------------------------------------------------------------------


def note_files():
    """(path, mtime, size, drive_id) for every note, skipping Obsidian's own dirs."""
    for p in sorted(VAULT_DIR.rglob("*.md")):
        rel = p.relative_to(VAULT_DIR)
        if any(part.startswith(".") for part in rel.parts):
            continue
        st = p.stat()
        yield str(rel), str(int(st.st_mtime)), st.st_size, None


def drive_files():
    """(path, mtime, size, drive_id) for every Drive file, newest first."""
    if not MIRROR_DIR.is_dir():
        return []
    if LISTING.exists():
        rows = [
            (e["Path"], e["ModTime"], e["Size"], e.get("ID"))
            for e in json.loads(LISTING.read_text())
            if not e.get("IsDir")
        ]
    else:
        rows = [
            (
                str(p.relative_to(MIRROR_DIR)),
                str(int(p.stat().st_mtime)),
                p.stat().st_size,
                None,
            )
            for p in MIRROR_DIR.rglob("*")
            if p.is_file()
        ]
    return sorted(rows, key=lambda r: r[1], reverse=True)


# ---- turning a file into items -------------------------------------------------
# An item is (kind, page, heading, body, input): input is the str or image bytes
# to embed, body is what search shows.


def split_long(text):
    """Split text into pieces of at most CHUNK_CHARS, at blank lines, then lines."""
    pieces, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > CHUNK_CHARS:
            cut = para.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            pieces.append(para[:cut])
            para = para[cut:].lstrip("\n")
        if cur and len(cur) + len(para) + 2 > CHUNK_CHARS:
            pieces.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def note_items(path):
    text = (VAULT_DIR / path).read_text(errors="replace")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # frontmatter
    title = Path(path).stem
    sections, heading, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            sections.append((heading, "\n".join(buf)))
            heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf)))
    for heading, body in sections:
        for piece in split_long(body):
            head = f"{title} > {heading}" if heading else title
            yield "note", None, heading, piece, f"{head}\n\n{piece}"


def to_jpeg(img):
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def pdf_items(file):
    pdf = pdfium.PdfDocument(file)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            text = page.get_textpage().get_text_range().strip()
            image = to_jpeg(page.render(scale=DPI / 72).to_pil())
            page.close()
            yield "image", i + 1, None, text[:CHUNK_CHARS], image
            if len(text) >= MIN_PAGE_TEXT:
                yield "text", i + 1, None, text[:CHUNK_CHARS], f"{file.stem}\n\n{text}"
    finally:
        pdf.close()


def page_count(file):
    pdf = pdfium.PdfDocument(file)
    try:
        return len(pdf)
    finally:
        pdf.close()


def drive_items(file):
    ext = file.suffix.lower()
    if ext == ".pdf":
        yield from pdf_items(file)
    elif ext in IMAGE_EXT:
        with Image.open(file) as img:
            yield "image", None, None, None, to_jpeg(img)
    elif ext in TEXT_EXT:
        for piece in split_long(file.read_text(errors="replace")):
            yield "text", None, None, piece, f"{file.stem}\n\n{piece}"


def skip_reason(path, size):
    ext = Path(path).suffix.lower()
    if ext not in IMAGE_EXT | TEXT_EXT | {".pdf"}:
        return f"unsupported type ({ext or 'no extension'})"
    if size > MAX_BYTES:
        return "over 100 MB"
    if not (MIRROR_DIR / path).exists():
        return "not in the mirror"
    return None


# ---- the index -----------------------------------------------------------------


def embed_items(items):
    """Embed items in batches, text and images apart. Returns (item, vec) pairs."""
    out, texts, images = [], [], []

    def flush(batch):
        if batch:
            out.extend(zip(batch, embed([it[4] for it in batch])))
            batch.clear()

    for it in items:
        batch, size = (
            (images, IMAGE_BATCH) if isinstance(it[4], bytes) else (texts, TEXT_BATCH)
        )
        batch.append(it)
        if len(batch) >= size:
            flush(batch)
    flush(texts)
    flush(images)
    return out


def save(
    db,
    source,
    path,
    mtime,
    size,
    drive_id,
    status,
    reason=None,
    pages=None,
    embedded=(),
):
    with db:
        db.execute("DELETE FROM docs WHERE source = ? AND path = ?", (source, path))
        doc = db.execute(
            "INSERT INTO docs (source, path, drive_id, mtime, size, status, reason, pages, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, path, drive_id, mtime, size, status, reason, pages, now()),
        ).lastrowid
        db.executemany(
            "INSERT INTO items (doc_id, kind, page, heading, body, vec) VALUES (?, ?, ?, ?, ?, ?)",
            [(doc, k, pg, h, b, pack(v)) for (k, pg, h, b, _), v in embedded],
        )
        db.execute(
            "INSERT INTO meta VALUES ('generation', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (now(), now()),
        )


def run(db):
    model = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
    if model and model[0] != MODEL:
        log(f"model changed from {model[0]} to {MODEL}: re-indexing everything")
        with db:
            db.execute("DELETE FROM docs")
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (MODEL,))

    known = {
        (s, p): (m, z)
        for s, p, m, z in db.execute("SELECT source, path, mtime, size FROM docs")
    }
    sources = [("note", f) for f in note_files()] + [
        ("drive", f) for f in drive_files()
    ]
    seen = {(s, f[0]) for s, f in sources}
    gone = [k for k in known if k not in seen]
    with db:
        db.executemany("DELETE FROM docs WHERE source = ? AND path = ?", gone)

    todo = [(s, f) for s, f in sources if known.get((s, f[0])) != (f[1], f[2])]
    log(f"{len(seen)} files, {len(todo)} new or changed, {len(gone)} removed")

    done = items = skipped = 0
    for source, (path, mtime, size, drive_id) in todo:
        if time.monotonic() > DEADLINE:
            log(
                f"deadline reached: {len(todo) - done - skipped} files left for the next run"
            )
            break
        reason = skip_reason(path, size) if source == "drive" else None
        if reason:
            save(db, source, path, mtime, size, drive_id, "skipped", reason)
            skipped += 1
            log(f"skip  {path}: {reason}")
            continue
        t0 = time.monotonic()
        try:
            if source == "note":
                embedded, pages = embed_items(note_items(path)), None
            else:
                file = MIRROR_DIR / path
                embedded = embed_items(drive_items(file))
                pages = page_count(file) if file.suffix.lower() == ".pdf" else None
        except OllamaDown:
            raise
        except Exception as e:  # a broken file must not stop the run
            save(db, source, path, mtime, size, drive_id, "skipped", f"error: {e}")
            skipped += 1
            log(f"skip  {path}: error: {e}")
            continue
        save(
            db,
            source,
            path,
            mtime,
            size,
            drive_id,
            "done",
            pages=pages,
            embedded=embedded,
        )
        done += 1
        items += len(embedded)
        if source == "drive" or done % 100 == 0:
            log(f"done  {path}: {len(embedded)} items, {time.monotonic() - t0:.1f}s")
    log(f"indexed {done} files ({items} items), skipped {skipped}")


def main():
    if not ollama_up():
        log("Ollama is not answering: skipping this run")
        return
    db = connect(INDEX_DB)
    try:
        run(db)
    except OllamaDown as e:
        log(
            f"Ollama went away mid-run ({e}): progress is saved, the next run carries on"
        )
    finally:
        db.close()
    if DRIVE_ERROR.exists():
        log("Drive mirror failed this run (see the mirror job's log): failing the job")
        return 1


if __name__ == "__main__":
    sys.exit(main())
