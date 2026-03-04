# WST — Wan Shi Tong

> *"He Who Knows Ten Thousand Things"*

WST is a book-to-knowledge-base extraction pipeline for offensive security. It reads technical security books (PDF, EPUB, AZW3, MOBI, CHM), extracts actionable attack knowledge using a local LLM, and loads it into a semantic vector store (Fact Vault) where AI agents can search it in real time during penetration testing and CTF challenges.

---

## The Problem

AI agents doing penetration testing need deep, specific technical knowledge — exact commands, attack chains, default credentials, escalation paths. This knowledge exists in security books, but it's trapped inside 500-page narratives. An agent can't ctrl+F a book mid-attack.

WST solves this by pre-processing your security library into a searchable knowledge base. When an agent encounters a SUID binary, it queries FV and gets back the exact exploit command. When it finds MSSQL injection, it gets back the full `xp_cmdshell` escalation chain.

---

## Architecture

```
books/          web_sources/
  *.pdf           gtfobins/
  *.epub          hacktricks/
  *.azw3
     |                |
     v                v
  [Stage 1: Ingest]  [ingest_web.py]
  extracted/*.txt    facts/gtfobins.json
     |                |
     v                v
  [Stage 2: Extract]
  LLM chunks text → JSON facts
  facts/*.json
     |
     v
  [Stage 3: Dedup]
  Semantic similarity check vs FV
  facts/*.deduped.json
     |
     v
  [Stage 4: Seed]
  POST /memorize → Fact Vault
                   |
                   v
             [Fact Vault]
          sentence-transformers
          semantic search API
                   |
                   v
            AI Agent queries
          "suid binary exploit"
          → exact command returned
```

---

## Supported Formats

| Format | Tool Required | Notes |
|--------|--------------|-------|
| PDF | `pdftotext` (poppler) or `PyPDF2` | pdftotext gives better layout preservation |
| EPUB | `ebooklib` + `beautifulsoup4` | Native support |
| AZW3 / MOBI | Calibre `ebook-convert` | Converts to EPUB first |
| CHM | `7z` + `beautifulsoup4` | Extracts HTML, strips tags |

### Web Sources

- **GTFOBins** — 478+ Linux binaries with privilege escalation and shell escape techniques. Parsed directly from structured YAML — no LLM needed. Each binary function gets its own searchable fact.
- **HackTricks** — 960+ pages of community penetration testing knowledge. Converted to plain text and processed through the LLM extraction stage.

---

## Installation

### Dependencies

```bash
pip install ebooklib PyPDF2 beautifulsoup4 lxml pyyaml
```

**System tools** (install via your package manager):
- `pdftotext` — from poppler-utils (`apt install poppler-utils` / `brew install poppler`)
- `ebook-convert` — from [Calibre](https://calibre-ebook.com/download)
- `7z` — from p7zip (`apt install p7zip-full`)

### Fact Vault

WST seeds into [Fact Vault](https://github.com/benolenick/fact-vault) — a lightweight semantic search API built on sentence-transformers. Run it locally:

```bash
# See Fact Vault README for setup
FV_ENDPOINT=http://127.0.0.1:8000
```

### Ollama

WST uses a local LLM via [Ollama](https://ollama.com) for fact extraction:

```bash
ollama pull llama3:latest
# or any model you prefer — see OLLAMA_MODEL env var
```

---

## Usage

### Books Pipeline

```bash
# 1. Drop books into books/
cp ~/Downloads/hacking_book.pdf books/

# 2. Run the full pipeline
python3 pipeline.py run

# Or run stages individually:
python3 pipeline.py ingest    # Extract raw text
python3 pipeline.py extract   # LLM extracts facts
python3 pipeline.py dedup     # Remove duplicates vs FV
python3 pipeline.py seed      # Load into Fact Vault

# Check status
python3 pipeline.py status
```

### Web Sources

```bash
# Clone web sources
mkdir -p web_sources
git clone https://github.com/GTFOBins/GTFOBins.github.io.git web_sources/gtfobins
git clone https://github.com/HackTricks-wiki/hacktricks.git web_sources/hacktricks

# Run ingester (GTFOBins seeds directly; HackTricks queues for LLM)
python3 ingest_web.py

# Then process HackTricks through the LLM pipeline
python3 pipeline.py extract
python3 pipeline.py dedup
python3 pipeline.py seed
```

---

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WST_HOME` | `~/wst` | Base directory for all data |
| `FV_ENDPOINT` | `http://127.0.0.1:8000` | Fact Vault API endpoint |
| `OLLAMA_ENDPOINT` | `http://127.0.0.1:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3:latest` | Model for fact extraction |
| `SHAMAN_QUEUE` | *(empty)* | AI-Shaman priority queue URL (optional) |
| `WST_BULK_DELAY` | `2.0` | Seconds between LLM chunks (politeness) |

---

## The Extraction Prompt

The key innovation is the prompt design. WST doesn't ask the LLM to "summarize" — it asks for **actionable attack knowledge** in a specific format:

**Individual facts:**
- Commands with exact syntax
- Default credentials, ports, file paths
- Misconfigurations that enable privilege escalation
- Tool-specific tips (nmap scripts, sqlmap tampers, hashcat modes)

**Attack chains** (IF/THEN conditional logic):
```
"MSSQL-to-OS: IF (SQLi == MSSQL) AND (User == sa) THEN
  EXEC sp_configure 'show advanced options',1; RECONFIGURE →
  EXEC sp_configure 'xp_cmdshell',1; RECONFIGURE →
  get OS shell via xp_cmdshell"

"Linux Privesc: IF (sudo -l shows env_keep+=LD_PRELOAD) THEN
  compile malicious .so →
  sudo LD_PRELOAD=/tmp/evil.so /usr/bin/allowed_binary →
  root shell"

"Web-to-Shell: IF (FileUpload == Allowed) AND (Filter == ExtensionOnly) THEN
  try .php5/.phtml/.phar →
  upload reverse shell →
  check /uploads/ for execution"
```

When an agent encounters condition A during an attack, querying FV returns the complete decision tree of what to try next.

---

## Priority Queue Integration (Optional)

If you're running WST alongside active AI agents on the same machine, you can configure the AI-Shaman priority queue (`SHAMAN_QUEUE`) so WST jobs run at `bulk` priority — automatically yielding to live attack work. Without the queue, WST calls Ollama directly.

WST also detects when a different model is loaded in Ollama and backs off rather than forcing a costly model swap.

---

## Directory Structure

```
wst/
├── pipeline.py         # Main pipeline (ingest/extract/dedup/seed)
├── ingest_web.py       # Web source ingester (GTFOBins + HackTricks)
├── requirements.txt
├── .gitignore
├── books/              # Drop your PDFs/EPUBs here (gitignored)
├── web_sources/        # Cloned GTFOBins + HackTricks repos (gitignored)
├── extracted/          # Raw extracted text (gitignored)
├── facts/              # Extracted facts JSON (gitignored)
├── logs/               # LLM response logs (gitignored)
└── state.json          # Pipeline state tracking (gitignored)
```

---

## Querying Fact Vault

Once seeded, query your knowledge base from any agent or script:

```python
import urllib.request, json

query = "suid binary python privilege escalation"
payload = json.dumps({"query": query, "top_k": 5}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8000/search",
    data=payload,
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req) as resp:
    results = json.loads(resp.read())
    for r in results:
        print(r["fact"])
```

Example results:
```
GTFOBins python (suid, works with: suid): python -c 'import os; os.execl("/bin/sh", "sh", "-p")'
GTFOBins python3 (suid, works with: suid): python3 -c 'import os; os.execl("/bin/sh", "sh", "-p")'
```

---

## License

MIT
