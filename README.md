# WST — Wan Shi Tong

> *"He Who Knows Ten Thousand Things"*

WST is a book-to-knowledge-base extraction pipeline. It reads technical books (PDF, EPUB, AZW3, MOBI, CHM), extracts structured, actionable knowledge using a local LLM, and loads it into a semantic vector store (Fact Vault) where AI agents and search tools can retrieve it in real time.

The core idea: technical books contain thousands of specific, actionable facts — exact commands, decision trees, configuration patterns, diagnostic procedures — buried inside hundreds of pages of narrative. WST pulls those facts out, deduplicates them, and makes them semantically searchable.

---

## The Problem

Technical knowledge lives in books. But books are optimized for humans reading front-to-back, not for agents (or humans) who need a specific answer right now.

A DevOps engineer troubleshooting a Kubernetes pod crash at 2 AM doesn't need chapter 4 of "Kubernetes in Action" — they need the exact `kubectl debug` invocation for an OOMKilled container. A medical resident reviewing a drug interaction doesn't need the pharmacology textbook — they need the specific contraindication and the alternative. A lawyer researching precedent doesn't need the whole casebook — they need the holding and the distinguishing facts.

WST pre-processes your technical library into a searchable knowledge base. When you or an agent query it, you get back the specific fact, command, or decision tree — not a chapter summary.

---

## Architecture

```
books/          web_sources/
  *.pdf           structured_docs/
  *.epub          markdown_wikis/
  *.azw3
     |                |
     v                v
  [Stage 1: Ingest]  [ingest_web.py]
  extracted/*.txt    facts/*.json
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
          FAISS vector index
          semantic search API
                   |
                   v
            Agent / user queries
          "kubernetes OOMKilled debug"
          → exact procedure returned
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

WST also ingests structured web sources — any documentation repository with YAML, markdown, or structured data files. Sources with well-defined schemas (like YAML config references) can be parsed directly without LLM involvement. Unstructured markdown documentation goes through the full LLM extraction stage.

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

WST seeds into [Fact Vault](https://github.com/benolenick/fact-vault) — a lightweight semantic search API built on sentence-transformers and FAISS. It encodes each fact as a vector embedding using the `all-MiniLM-L6-v2` model and supports sub-second cosine similarity search across thousands of facts. Run it locally:

```bash
# See Fact Vault README for setup
FV_ENDPOINT=http://127.0.0.1:8000
```

### Ollama

WST uses a local LLM via [Ollama](https://ollama.com) for knowledge extraction:

```bash
ollama pull llama3:latest
# or any model you prefer — see OLLAMA_MODEL env var
```

---

## Usage

### Books Pipeline

```bash
# 1. Drop books into books/
cp ~/Downloads/kubernetes_in_action.pdf books/

# 2. Run the full pipeline
python3 pipeline.py run

# Or run stages individually:
python3 pipeline.py ingest    # Extract raw text
python3 pipeline.py extract   # LLM extracts structured facts
python3 pipeline.py dedup     # Remove duplicates vs FV
python3 pipeline.py seed      # Load into Fact Vault

# Check status
python3 pipeline.py status
```

### Web Sources

```bash
# Clone any structured documentation source
mkdir -p web_sources

# Run ingester (structured sources seed directly; prose queues for LLM)
python3 ingest_web.py

# Then process unstructured content through the LLM pipeline
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
| `OLLAMA_MODEL` | `llama3:latest` | Model for knowledge extraction |
| `SHAMAN_QUEUE` | *(empty)* | AI-Shaman priority queue URL (optional) |
| `WST_BULK_DELAY` | `2.0` | Seconds between LLM chunks (politeness) |

---

## The Extraction Prompt

The quality of extracted knowledge depends entirely on what you ask the LLM to look for. A naive "summarize this text" prompt produces useless output — vague descriptions of concepts that don't help anyone who needs a specific answer to a specific problem.

WST's prompt asks for two very specific things:

**Individual actionable facts:**
- Commands and procedures with exact syntax
- Configuration values, default ports, file paths, thresholds
- Decision criteria — the specific conditions under which you'd choose option A over option B
- Tool-specific tips with actual flags and parameters
- Error messages and their specific resolutions

**Decision trees** (IF/THEN conditional logic):
```
"K8s Pod Debugging: IF (Pod == CrashLoopBackOff) AND (Exit Code == 137)
  THEN check resource limits with kubectl describe pod
  → IF (Last State == OOMKilled)
    THEN increase memory limit or profile app memory usage
  → IF (limits seem adequate)
    THEN check for memory leaks with kubectl exec + profiler"

"Database Replication Lag: IF (replica_lag > 30s) AND (write_throughput == normal)
  THEN check replica IO thread status
  → IF (Seconds_Behind_Master increasing)
    THEN check slow query log on replica
  → IF (IO thread stopped)
    THEN check network + SHOW SLAVE STATUS for error"

"API Rate Limiting: IF (HTTP 429) AND (Retry-After header present)
  THEN implement exponential backoff with jitter
  → respect Retry-After value as minimum delay
  → IF (429 persists after backoff)
    THEN check if API key is tier-limited
    → request quota increase or implement request batching"
```

When an agent or user encounters condition A, querying Fact Vault returns the complete decision tree of what to check and try next. The knowledge is already structured as a procedure — no need to reason from first principles.

The prompt also filters out everything that isn't actionable: general theory, historical context, architecture overviews, basic definitions. A technical book might be 70% background and 30% specific techniques and procedures. WST extracts the 30%.

---

## Priority Queue Integration (Optional)

If you're running WST alongside other AI workloads on the same machine, you can configure the AI-Shaman priority queue (`SHAMAN_QUEUE`) so WST jobs run at `bulk` priority — automatically yielding to higher-priority work. Without the queue, WST calls Ollama directly.

WST also detects when a different model is loaded in Ollama and backs off rather than forcing a costly model swap.

---

## Directory Structure

```
wst/
├── pipeline.py         # Main pipeline (ingest/extract/dedup/seed)
├── ingest_web.py       # Web source ingester (structured docs + markdown)
├── requirements.txt
├── .gitignore
├── books/              # Drop your PDFs/EPUBs here (gitignored)
├── web_sources/        # Cloned documentation repos (gitignored)
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

query = "kubernetes pod crashloopbackoff OOMKilled"
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
K8s Pod Debugging: IF (Pod == CrashLoopBackOff) AND (Exit Code == 137) THEN check resource
limits with kubectl describe pod → IF (Last State == OOMKilled) THEN increase memory limit
or profile app memory usage

kubectl debug node/<node-name> -it --image=busybox — attach debug container to node for
host-level troubleshooting when pod-level access is insufficient
```

---

## Why This Is Hard (and Worth Doing)

Books are written for sequential reading. The knowledge in them is woven into explanations, examples, and narrative context. Extracting the actionable parts without losing precision is a genuine NLP challenge — you need an LLM that can distinguish "here is the exact command" from "here is a discussion about the concept behind the command."

The dedup problem is also non-trivial. Five books on the same subject will all cover the fundamentals. Without semantic deduplication, your knowledge base fills up with near-identical facts that dilute search quality. The 0.85 cosine similarity threshold WST uses is tuned to let through meaningfully different phrasings while blocking true duplicates.

The payoff: once your library is processed, you have a searchable knowledge base that any tool, agent, or script can query with a single HTTP call. The knowledge that took weeks to read becomes accessible in milliseconds.

---

## License

MIT
