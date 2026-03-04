#!/usr/bin/env python3
"""
WST — Wan Shi Tong
He Who Knows Ten Thousand Things

Book-to-FV knowledge extraction pipeline.
Ingests PDFs/EPUBs, extracts actionable security facts via LLM, seeds to Fact Vault.

Usage:
    python3 pipeline.py ingest          # Extract text from books/ into extracted/
    python3 pipeline.py extract         # LLM extracts facts from extracted/ into facts/
    python3 pipeline.py dedup           # Deduplicate against existing FV facts
    python3 pipeline.py seed            # Seed deduplicated facts to FV
    python3 pipeline.py run             # Full pipeline: ingest → extract → dedup → seed
    python3 pipeline.py status          # Show pipeline state

Environment variables:
    WST_HOME          — base directory (default: ~/wst)
    FV_ENDPOINT       — Fact Vault API (default: http://127.0.0.1:8000)
    OLLAMA_ENDPOINT   — Ollama API (default: http://127.0.0.1:11434)
    OLLAMA_MODEL      — model to use (default: llama3:latest)
    SHAMAN_QUEUE      — AI-Shaman priority queue endpoint (optional)
    WST_BULK_DELAY    — seconds between chunks (default: 2.0)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WST_HOME = Path(os.environ.get("WST_HOME", os.path.expanduser("~/wst")))
BOOKS_DIR = WST_HOME / "books"
EXTRACTED_DIR = WST_HOME / "extracted"
FACTS_DIR = WST_HOME / "facts"
LOGS_DIR = WST_HOME / "logs"
STATE_FILE = WST_HOME / "state.json"

FV_ENDPOINT = os.environ.get("FV_ENDPOINT", "http://127.0.0.1:8000")
OLLAMA_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3:latest")

# AI-Shaman queue endpoint — WST submits bulk jobs here for priority scheduling
# Leave empty to disable queue and call Ollama directly
SHAMAN_QUEUE = os.environ.get("SHAMAN_QUEUE", "")

# Chunk size in characters (~2000 tokens at ~4 chars/token)
CHUNK_SIZE = 8000
CHUNK_OVERLAP = 500

# Max facts per chunk (LLM asked to extract up to this many)
MAX_FACTS_PER_CHUNK = 30

# Backoff settings
BULK_DELAY = float(os.environ.get("WST_BULK_DELAY", "2.0"))  # seconds between chunks
MAX_BACKOFF = 300  # 5 min max backoff
BACKOFF_MULTIPLIER = 2


def ensure_dirs():
    for d in [BOOKS_DIR, EXTRACTED_DIR, FACTS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"books_processed": {}, "facts_seeded": 0, "total_facts_extracted": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Stage 1: Ingest — extract text from PDFs/EPUBs
# ---------------------------------------------------------------------------

def extract_pdf(path: Path) -> str:
    """Extract text from PDF using pdftotext (poppler) or PyPDF2 fallback."""
    # Try pdftotext first (better quality)
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback to PyPDF2
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(str(path))
        text = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text.append(t)
        return "\n\n".join(text)
    except ImportError:
        print(f"  [!] Need pdftotext or PyPDF2 for {path.name}", file=sys.stderr)
        return ""


def convert_to_epub(path: Path) -> Optional[Path]:
    """Convert azw3/mobi/etc to epub using ebook-convert (Calibre)."""
    out_path = path.with_suffix(".epub")
    if out_path.exists():
        return out_path
    try:
        result = subprocess.run(
            ["ebook-convert", str(path), str(out_path)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and out_path.exists():
            return out_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    print(f"  [!] ebook-convert failed for {path.name}", file=sys.stderr)
    return None


def extract_chm(path: Path) -> str:
    """Extract text from CHM using 7z + BeautifulSoup."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["7z", "x", str(path), f"-o{tmpdir}", "-y"],
                capture_output=True, timeout=60
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print(f"  [!] Need 7z for {path.name}", file=sys.stderr)
            return ""

        texts = []
        html_files = sorted(Path(tmpdir).rglob("*.htm")) + sorted(Path(tmpdir).rglob("*.html"))
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # Fallback: just strip tags crudely
            for hf in html_files:
                raw = hf.read_text(encoding="utf-8", errors="replace")
                texts.append(re.sub(r"<[^>]+>", " ", raw))
            return "\n\n".join(texts)

        for hf in html_files:
            raw = hf.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(raw, "html.parser")
            t = soup.get_text(separator="\n", strip=True)
            if t:
                texts.append(t)
        return "\n\n".join(texts)


def extract_epub(path: Path) -> str:
    """Extract text from EPUB using ebooklib + BeautifulSoup."""
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup

        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        text = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            t = soup.get_text(separator="\n", strip=True)
            if t:
                text.append(t)
        return "\n\n".join(text)
    except ImportError:
        print(f"  [!] Need ebooklib + beautifulsoup4 for {path.name}", file=sys.stderr)
        return ""


def ingest():
    """Stage 1: Extract raw text from books."""
    ensure_dirs()
    state = load_state()
    SUPPORTED = {".pdf", ".epub", ".azw3", ".mobi", ".azw", ".chm"}
    books = [f for f in BOOKS_DIR.iterdir() if f.suffix.lower() in SUPPORTED]

    if not books:
        print(f"No books found in {BOOKS_DIR}/")
        print("Drop PDFs, EPUBs, AZW3s, or CHMs there and re-run.")
        return

    for book in sorted(books):
        slug = book.stem.lower().replace(" ", "_").replace("-", "_")
        out_file = EXTRACTED_DIR / f"{slug}.txt"
        book_hash = hashlib.md5(book.read_bytes()[:4096]).hexdigest()

        # Skip if already extracted (same file hash)
        if slug in state.get("books_processed", {}) and \
           state["books_processed"][slug].get("hash") == book_hash and \
           out_file.exists():
            print(f"  [skip] {book.name} (already extracted)")
            continue

        print(f"  [ingest] {book.name} ...", end=" ", flush=True)
        ext = book.suffix.lower()
        if ext == ".pdf":
            text = extract_pdf(book)
        elif ext == ".epub":
            text = extract_epub(book)
        elif ext in (".azw3", ".mobi", ".azw"):
            epub_path = convert_to_epub(book)
            text = extract_epub(epub_path) if epub_path else ""
        elif ext == ".chm":
            text = extract_chm(book)
        else:
            continue

        if not text.strip():
            print("EMPTY (extraction failed)")
            continue

        out_file.write_text(text, encoding="utf-8")
        char_count = len(text)
        print(f"OK ({char_count:,} chars)")

        state.setdefault("books_processed", {})[slug] = {
            "file": book.name,
            "hash": book_hash,
            "chars": char_count,
            "extracted_at": time.strftime("%Y-%m-%d %H:%M"),
            "facts_extracted": False
        }
        save_state(state)

    print(f"\nExtracted text in {EXTRACTED_DIR}/")


# ---------------------------------------------------------------------------
# Stage 2: Chunk + LLM extract facts
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are an offensive security knowledge extractor for a Fact Vault used during penetration testing and CTF challenges.

Your job: read the following text from a technical book and extract ACTIONABLE facts useful for penetration testing.

EXTRACT these kinds of facts:

INDIVIDUAL FACTS:
- Specific commands with syntax (e.g., "impacket-secretsdump -just-dc-user Administrator DOMAIN/user@target")
- Default credentials, ports, paths, filenames
- Misconfigurations that lead to privilege escalation
- Attack techniques with step-by-step details
- Windows/Linux internals relevant to exploitation (token manipulation, service permissions, named pipes, etc.)
- Registry keys, file paths, or config locations an attacker would check
- Enumeration commands and what to look for in their output
- Tool-specific tips (nmap scripts, sqlmap tampers, hashcat modes, etc.)
- Vulnerability patterns (what makes code/config exploitable)
- Defensive controls and how to bypass them

ATTACK CHAINS (critical — extract these whenever you see a multi-step technique):
- Use conditional logic format: IF (condition) THEN (action) → (next step) → (outcome)
- Decision trees: "If you find X, try Y. If Y fails, try Z."
- Escalation paths: "Service A runs as SYSTEM → exploit B gives write access → overwrite C → get SYSTEM shell"

Examples of good attack chain facts:
- "Web-to-Shell: IF (FileUpload == Allowed) AND (Filter == ExtensionOnly) THEN try .php5/.phtml/.phar → upload reverse shell → check /uploads/ for execution"
- "AD-to-Admin: IF (UserAccess == DomainUser) THEN run BloodHound → identify shortest path to DA → IF path includes Kerberoasting THEN run GetUserSPNs.py"
- "MSSQL-to-OS: IF (SQLi == MSSQL) AND (User == sa) THEN EXEC sp_configure 'show advanced options',1; RECONFIGURE → EXEC sp_configure 'xp_cmdshell',1; RECONFIGURE → get OS shell"
- "Linux Privesc: IF (sudo -l shows env_keep+=LD_PRELOAD) THEN compile malicious .so → sudo LD_PRELOAD=/tmp/evil.so /usr/bin/allowed_binary → root shell"

The goal: if an agent finds condition A during an attack, searching FV should return the full chain of what to try next.

DO NOT extract:
- General theory or history ("Windows NT was developed in...")
- Architecture diagrams or high-level overviews with no actionable detail
- API documentation unless it reveals attack surface
- Definitions of basic terms

FORMAT: Return a JSON array of strings. Each string is one self-contained fact.
Each fact should be 1-3 sentences and make sense without context from the book.
Include the technique name, tool, or concept at the start of each fact for searchability.
Attack chains can be longer (3-5 sentences) to capture the full decision tree.

If the text has nothing useful for offensive security, return an empty array: []

TEXT:
{chunk}

Return ONLY the JSON array, no other text."""


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks


def _try_shaman_queue(prompt: str, timeout: int = 180) -> Optional[str]:
    """Submit job to AI-Shaman queue if available. Returns response or None."""
    if not SHAMAN_QUEUE:
        return None
    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "priority": "bulk",
            "consumer": "wst",
            "endpoint": "/api/generate",
            "timeout": timeout,
        }).encode()
        req = urllib.request.Request(
            f"{SHAMAN_QUEUE}/submit",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            job_id = data.get("job_id")
            if not job_id:
                return None

        # Poll for result
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                req2 = urllib.request.Request(
                    f"{SHAMAN_QUEUE}/result/{job_id}", method="GET"
                )
                with urllib.request.urlopen(req2, timeout=10) as resp:
                    result = json.loads(resp.read())
                    state = result.get("state", result.get("status", ""))
                    if state == "done" or state == "completed":
                        inner = result.get("result", result)
                        return inner.get("response", "")
                    elif state in ("failed", "error"):
                        print(f"    [!] Queue job failed: {result.get('error', 'unknown')}", file=sys.stderr)
                        return None
                    # else: pending/running — keep polling
            except urllib.error.HTTPError as e:
                if e.code == 202:
                    pass  # Still processing
                else:
                    break
            except Exception:
                pass
        print("    [!] Queue job timed out", file=sys.stderr)
        return None
    except Exception:
        return None  # Queue not available, fall through to direct


def _check_ollama_busy() -> bool:
    """Check if Ollama is busy with another model (avoid forcing a model swap)."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_ENDPOINT}/api/ps", timeout=5) as resp:
            data = json.loads(resp.read())
            running = data.get("models", [])
            if running:
                running_name = running[0].get("name", "")
                if running_name and running_name != OLLAMA_MODEL:
                    return True  # Another model is loaded
    except Exception:
        pass
    return False


def query_ollama(prompt: str, timeout: int = 180) -> Optional[str]:
    """Query Ollama with queue support and backoff."""
    # Try AI-Shaman queue first (if configured)
    result = _try_shaman_queue(prompt, timeout)
    if result is not None:
        return result

    # Direct Ollama with backoff
    backoff = BULK_DELAY
    for attempt in range(5):
        # Check if another model is running — don't force a swap
        if _check_ollama_busy():
            wait = min(backoff, MAX_BACKOFF)
            print(f"\n    [backoff] Another model loaded, waiting {wait:.0f}s...", file=sys.stderr, flush=True)
            time.sleep(wait)
            backoff *= BACKOFF_MULTIPLIER
            continue

        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,
            }
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_ENDPOINT}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            start = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                elapsed = time.time() - start
                if elapsed > 60:
                    print(f"\n    [slow] {elapsed:.0f}s response, increasing delay", file=sys.stderr)
                return data.get("response", "")
        except urllib.error.HTTPError as e:
            if e.code == 503:
                retry_after = int(e.headers.get("Retry-After", backoff))
                wait = min(retry_after, MAX_BACKOFF)
                print(f"\n    [503] Service unavailable, retrying in {wait}s...", file=sys.stderr, flush=True)
                time.sleep(wait)
                backoff *= BACKOFF_MULTIPLIER
                continue
            print(f"    [!] Ollama HTTP error: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"    [!] Ollama error: {e}", file=sys.stderr)
            return None

    print("    [!] Max retries exceeded, skipping chunk", file=sys.stderr)
    return None


def parse_facts_json(raw: str) -> list[str]:
    """Parse JSON array of facts from LLM response, tolerating markdown fences."""
    raw = raw.strip()
    # Strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
        raw = raw.strip()

    # Strip <think>...</think> blocks (some reasoning models emit these)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    try:
        facts = json.loads(raw)
        if isinstance(facts, list):
            return [f.strip() for f in facts if isinstance(f, str) and f.strip()]
    except json.JSONDecodeError:
        pass

    # Try to find a JSON array in the response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        try:
            facts = json.loads(match.group())
            if isinstance(facts, list):
                return [f.strip() for f in facts if isinstance(f, str) and f.strip()]
        except json.JSONDecodeError:
            pass

    return []


def extract():
    """Stage 2: LLM extracts facts from chunked text."""
    ensure_dirs()
    state = load_state()
    texts = sorted(EXTRACTED_DIR.glob("*.txt"))

    if not texts:
        print(f"No extracted text in {EXTRACTED_DIR}/. Run 'ingest' first.")
        return

    # Check Ollama is up
    try:
        urllib.request.urlopen(f"{OLLAMA_ENDPOINT}/api/tags", timeout=5)
    except Exception:
        print(f"[!] Ollama not reachable at {OLLAMA_ENDPOINT}", file=sys.stderr)
        print("    Start Ollama or set OLLAMA_ENDPOINT", file=sys.stderr)
        return

    for txt_file in texts:
        slug = txt_file.stem
        facts_file = FACTS_DIR / f"{slug}.json"

        # Skip if already extracted
        book_info = state.get("books_processed", {}).get(slug, {})
        if book_info.get("facts_extracted") and facts_file.exists():
            print(f"  [skip] {slug} (facts already extracted)")
            continue

        print(f"\n  [extract] {slug}")
        text = txt_file.read_text(encoding="utf-8")
        chunks = chunk_text(text)
        print(f"    {len(chunks)} chunks ({len(text):,} chars)")

        all_facts = []
        for i, chunk in enumerate(chunks):
            print(f"    chunk {i+1}/{len(chunks)} ...", end=" ", flush=True)
            prompt = EXTRACTION_PROMPT.replace("{chunk}", chunk)
            response = query_ollama(prompt)
            if response is None:
                print("FAIL (ollama error)")
                continue

            facts = parse_facts_json(response)
            print(f"{len(facts)} facts")
            all_facts.extend(facts)

            # Log raw response for debugging
            log_file = LOGS_DIR / f"{slug}_chunk_{i:04d}.txt"
            log_file.write_text(response, encoding="utf-8")

            # Bulk delay — be a polite consumer
            if i < len(chunks) - 1:
                time.sleep(BULK_DELAY)

        # Deduplicate within book (exact match)
        unique_facts = list(dict.fromkeys(all_facts))

        facts_file.write_text(json.dumps(unique_facts, indent=2), encoding="utf-8")
        print(f"    Total: {len(unique_facts)} unique facts (from {len(all_facts)} raw)")

        state.setdefault("books_processed", {})[slug] = {
            **book_info,
            "facts_extracted": True,
            "fact_count": len(unique_facts),
            "extracted_facts_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        state["total_facts_extracted"] = sum(
            b.get("fact_count", 0) for b in state["books_processed"].values()
        )
        save_state(state)

    print(f"\nFacts saved in {FACTS_DIR}/")


# ---------------------------------------------------------------------------
# Stage 3: Deduplicate against existing FV
# ---------------------------------------------------------------------------

def dedup():
    """Stage 3: Remove facts that are too similar to existing FV entries."""
    ensure_dirs()
    facts_files = sorted(FACTS_DIR.glob("*.json"))

    if not facts_files:
        print(f"No facts in {FACTS_DIR}/. Run 'extract' first.")
        return

    # Check FV
    try:
        resp = urllib.request.urlopen(f"{FV_ENDPOINT}/health", timeout=10)
        health = json.loads(resp.read())
        existing_count = health.get("memory_facts", health.get("fact_count", health.get("facts", 0)))
        print(f"  FV online: {existing_count} existing facts")
    except Exception:
        print("[!] FV not reachable. Skipping dedup (will seed all facts).", file=sys.stderr)
        return

    for facts_file in facts_files:
        slug = facts_file.stem
        dedup_file = FACTS_DIR / f"{slug}.deduped.json"

        facts = json.loads(facts_file.read_text())
        if not facts:
            continue

        print(f"\n  [dedup] {slug}: {len(facts)} facts to check")
        kept = []
        dupes = 0

        for i, fact in enumerate(facts):
            if (i + 1) % 50 == 0:
                print(f"    checked {i+1}/{len(facts)}...", flush=True)

            # Search FV for similar facts
            try:
                payload = json.dumps({"query": fact[:200], "top_k": 1}).encode()
                req = urllib.request.Request(
                    f"{FV_ENDPOINT}/search",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    results = json.loads(resp.read())
                    hits = results if isinstance(results, list) else results.get("results", [])

                    if hits and len(hits) > 0:
                        top_score = hits[0].get("score", hits[0].get("similarity", 0))
                        if top_score > 0.85:
                            dupes += 1
                            continue
            except Exception:
                pass  # FV error → keep the fact

            kept.append(fact)

        dedup_file.write_text(json.dumps(kept, indent=2), encoding="utf-8")
        print(f"    Kept {len(kept)}, removed {dupes} duplicates")


# ---------------------------------------------------------------------------
# Stage 4: Seed to FV
# ---------------------------------------------------------------------------

def seed():
    """Stage 4: Seed deduplicated facts to FV."""
    ensure_dirs()
    state = load_state()

    # Prefer .deduped.json, fall back to .json
    facts_files = sorted(FACTS_DIR.glob("*.deduped.json"))
    if not facts_files:
        facts_files = sorted(f for f in FACTS_DIR.glob("*.json") if ".deduped." not in f.name)

    if not facts_files:
        print(f"No facts to seed. Run 'extract' first.")
        return

    # Check FV
    try:
        resp = urllib.request.urlopen(f"{FV_ENDPOINT}/health", timeout=10)
        health = json.loads(resp.read())
        before_count = health.get("memory_facts", health.get("fact_count", health.get("facts", 0)))
        print(f"  FV online: {before_count} facts before seeding")
    except Exception:
        print("[!] FV not reachable. Cannot seed.", file=sys.stderr)
        return

    total_seeded = 0
    for facts_file in facts_files:
        slug = facts_file.stem.replace(".deduped", "")
        facts = json.loads(facts_file.read_text())

        if not facts:
            continue

        print(f"\n  [seed] {slug}: {len(facts)} facts")
        seeded = 0
        errors = 0

        for i, fact in enumerate(facts):
            if (i + 1) % 25 == 0:
                print(f"    seeded {i+1}/{len(facts)}...", flush=True)

            try:
                payload = json.dumps({"fact": fact}).encode()
                req = urllib.request.Request(
                    f"{FV_ENDPOINT}/memorize",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    seeded += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"    [!] Seed error: {e}", file=sys.stderr)

        total_seeded += seeded
        print(f"    Seeded {seeded}, errors {errors}")

    state["facts_seeded"] = state.get("facts_seeded", 0) + total_seeded
    save_state(state)

    # Check FV after
    try:
        resp = urllib.request.urlopen(f"{FV_ENDPOINT}/health", timeout=10)
        health = json.loads(resp.read())
        after_count = health.get("memory_facts", health.get("fact_count", health.get("facts", 0)))
        print(f"\n  FV: {before_count} → {after_count} facts (+{after_count - before_count})")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status():
    """Show pipeline state."""
    ensure_dirs()
    state = load_state()
    SUPPORTED = {".pdf", ".epub", ".azw3", ".mobi", ".azw"}
    books = [f for f in BOOKS_DIR.iterdir() if f.suffix.lower() in SUPPORTED] if BOOKS_DIR.exists() else []
    texts = list(EXTRACTED_DIR.glob("*.txt"))
    facts_files = list(FACTS_DIR.glob("*.json"))

    print("=== WST — Wan Shi Tong ===")
    print(f"  Home: {WST_HOME}")
    print(f"  Books:     {len(books)} files in books/")
    print(f"  Extracted: {len(texts)} files in extracted/")
    print(f"  Facts:     {len(facts_files)} files in facts/")
    print(f"  Total facts extracted: {state.get('total_facts_extracted', 0)}")
    print(f"  Total facts seeded:    {state.get('facts_seeded', 0)}")

    # FV status
    try:
        resp = urllib.request.urlopen(f"{FV_ENDPOINT}/health", timeout=5)
        health = json.loads(resp.read())
        fv_count = health.get("memory_facts", health.get("fact_count", health.get("facts", 0)))
        print(f"  FV status: ONLINE ({fv_count} facts)")
    except Exception:
        print(f"  FV status: OFFLINE")

    # Ollama status
    try:
        resp = urllib.request.urlopen(f"{OLLAMA_ENDPOINT}/api/tags", timeout=5)
        models = json.loads(resp.read()).get("models", [])
        names = [m["name"] for m in models]
        print(f"  Ollama: ONLINE ({', '.join(names[:5])})")
    except Exception:
        print(f"  Ollama: OFFLINE")

    # Per-book status
    if state.get("books_processed"):
        print("\n  Books:")
        for slug, info in state["books_processed"].items():
            status_str = "extracted" if info.get("facts_extracted") else "text only"
            count = info.get("fact_count", 0)
            print(f"    {slug}: {status_str}, {count} facts, {info.get('chars', 0):,} chars")


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

def run():
    """Run full pipeline."""
    print("=== WST Full Pipeline ===\n")
    print("--- Stage 1: Ingest ---")
    ingest()
    print("\n--- Stage 2: Extract ---")
    extract()
    print("\n--- Stage 3: Dedup ---")
    dedup()
    print("\n--- Stage 4: Seed ---")
    seed()
    print("\n--- Done ---")
    status()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="WST — Wan Shi Tong: Book-to-FV knowledge extraction pipeline"
    )
    parser.add_argument("command", choices=["ingest", "extract", "dedup", "seed", "run", "status"],
                        help="Pipeline stage to run")
    args = parser.parse_args()

    {"ingest": ingest, "extract": extract, "dedup": dedup, "seed": seed,
     "run": run, "status": status}[args.command]()


if __name__ == "__main__":
    main()
