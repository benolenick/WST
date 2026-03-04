#!/usr/bin/env python3
"""
WST Web Source Ingester
Ingests structured web documentation sources into the WST knowledge pipeline.

Handles two source types:
  - Structured data (YAML/JSON schemas): parsed directly into facts without LLM involvement.
  - Unstructured prose (markdown docs/wikis): converted to plain text and queued for
    LLM extraction via the main pipeline.

Environment variables:
    WST_HOME      — base directory (default: ~/wst)
    MEMORIA_ENDPOINT   — Memoria API (default: http://127.0.0.1:8000)

Web sources are cloned into WST_HOME/web_sources/.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

WST_HOME = Path(os.environ.get("WST_HOME", os.path.expanduser("~/wst")))
WEB_SOURCES = WST_HOME / "web_sources"
EXTRACTED_DIR = WST_HOME / "extracted"
FACTS_DIR = WST_HOME / "facts"

MEMORIA_ENDPOINT = os.environ.get("MEMORIA_ENDPOINT", "http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# GTFOBins — direct YAML parse, no LLM needed
# ---------------------------------------------------------------------------

def ingest_gtfobins():
    """Parse GTFOBins YAML files into structured facts and seed directly to FV."""
    gtfo_dir = WEB_SOURCES / "gtfobins" / "_gtfobins"
    if not gtfo_dir.exists():
        print("[!] GTFOBins not cloned. Run:")
        print("    git clone https://github.com/GTFOBins/GTFOBins.github.io.git web_sources/gtfobins")
        return

    try:
        import yaml
    except ImportError:
        yaml = None

    binaries = sorted(f for f in gtfo_dir.iterdir() if f.is_file())
    print(f"  GTFOBins: {len(binaries)} binaries")

    facts = []
    for bin_file in binaries:
        name = bin_file.stem
        content = bin_file.read_text(encoding="utf-8", errors="replace")

        if yaml:
            try:
                # Strip YAML front matter markers
                if content.startswith("---"):
                    content = content[3:]
                    end = content.find("---")
                    if end > 0:
                        content = content[:end]
                data = yaml.safe_load(content)
            except Exception:
                data = None
        else:
            data = None

        if data and isinstance(data, dict) and "functions" in data:
            functions = data["functions"]
            for func_name, entries in functions.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    code = entry.get("code", "")
                    comment = entry.get("comment", "")
                    contexts = entry.get("contexts", {})

                    if not code and not contexts:
                        continue

                    # Build context-specific facts
                    ctx_list = list(contexts.keys()) if isinstance(contexts, dict) else []

                    # Check for context-specific code overrides
                    for ctx_name in ctx_list:
                        ctx_val = contexts[ctx_name]
                        if isinstance(ctx_val, dict) and "code" in ctx_val:
                            ctx_code = ctx_val["code"]
                            fact = f"GTFOBins {name} ({func_name}, {ctx_name}): {ctx_code}"
                            if comment:
                                fact += f" — {comment}"
                            facts.append(fact)

                    # General fact with all contexts
                    if code:
                        ctx_str = "/".join(ctx_list) if ctx_list else "unprivileged"
                        fact = f"GTFOBins {name} ({func_name}, works with: {ctx_str}): {code}"
                        if comment:
                            fact += f" — {comment}"
                        facts.append(fact)
        else:
            # Fallback: regex parse for code blocks
            code_blocks = re.findall(r'code:\s*\|?\-?\s*\n((?:\s{4,}.*\n?)+)', content)
            for block in code_blocks:
                code = block.strip()
                if code:
                    fact = f"GTFOBins {name}: {code}"
                    facts.append(fact)

    # Deduplicate
    facts = list(dict.fromkeys(facts))
    print(f"  Extracted {len(facts)} GTFOBins facts")

    # Save to facts dir
    facts_file = FACTS_DIR / "gtfobins.json"
    facts_file.write_text(json.dumps(facts, indent=2), encoding="utf-8")

    return facts


# ---------------------------------------------------------------------------
# HackTricks — extract markdown text for LLM processing
# ---------------------------------------------------------------------------

def ingest_hacktricks():
    """Convert HackTricks markdown files to extracted text for LLM pipeline."""
    ht_dir = WEB_SOURCES / "hacktricks"
    if not ht_dir.exists():
        print("[!] HackTricks not cloned. Run:")
        print("    git clone https://github.com/HackTricks-wiki/hacktricks.git web_sources/hacktricks")
        return

    md_files = sorted(ht_dir.rglob("*.md"))
    # Filter out non-content files
    skip_patterns = ["SUMMARY", "README", "CHANGELOG", ".github", "node_modules", "GLOSSARY"]
    md_files = [f for f in md_files if not any(s in str(f) for s in skip_patterns)]

    print(f"  HackTricks: {len(md_files)} markdown files")

    # Group by top-level directory for chunking
    sections = {}
    for md_file in md_files:
        rel = md_file.relative_to(ht_dir)
        parts = rel.parts
        section = parts[0] if len(parts) > 1 else "root"
        sections.setdefault(section, []).append(md_file)

    print(f"  Sections: {len(sections)}")
    for section, files in sorted(sections.items()):
        print(f"    {section}: {len(files)} files")

    # Concatenate each section into a single text file for the LLM pipeline
    total_chars = 0
    for section, files in sorted(sections.items()):
        slug = re.sub(r'[^a-z0-9]+', '_', section.lower()).strip('_')
        out_file = EXTRACTED_DIR / f"hacktricks_{slug}.txt"

        text_parts = []
        for md_file in sorted(files):
            content = md_file.read_text(encoding="utf-8", errors="replace")
            # Strip HTML tags but keep content
            content = re.sub(r'<[^>]+>', '', content)
            # Strip image references
            content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
            # Keep headers and content
            if content.strip():
                rel_path = md_file.relative_to(ht_dir)
                text_parts.append(f"=== {rel_path} ===\n{content}")

        combined = "\n\n".join(text_parts)
        if combined.strip():
            out_file.write_text(combined, encoding="utf-8")
            total_chars += len(combined)

    print(f"  Total HackTricks text: {total_chars:,} chars in {EXTRACTED_DIR}/hacktricks_*.txt")
    print("  These will be processed by the LLM pipeline on next 'extract' run.")


# ---------------------------------------------------------------------------
# Seed GTFOBins directly to FV (no LLM needed)
# ---------------------------------------------------------------------------

def seed_gtfobins_to_fv(facts: list):
    """Seed GTFOBins facts directly to FV."""
    if not facts:
        return

    try:
        resp = urllib.request.urlopen(f"{MEMORIA_ENDPOINT}/health", timeout=5)
        health = json.loads(resp.read())
        before = health.get("memory_facts", 0)
        print(f"  FV: {before} facts before seeding")
    except Exception:
        print("[!] FV offline, saving facts to file only")
        return

    ok = 0
    errors = 0
    for i, fact in enumerate(facts):
        if (i + 1) % 50 == 0:
            print(f"    seeded {i+1}/{len(facts)}...", flush=True)
        try:
            payload = json.dumps({"fact": fact}).encode()
            req = urllib.request.Request(
                f"{MEMORIA_ENDPOINT}/memorize", data=payload,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=30)
            ok += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"    [!] {e}", file=sys.stderr)

    try:
        resp = urllib.request.urlopen(f"{MEMORIA_ENDPOINT}/health", timeout=5)
        health = json.loads(resp.read())
        after = health.get("memory_facts", 0)
        print(f"  FV: {before} → {after} facts (+{after - before})")
    except Exception:
        pass

    print(f"  Seeded {ok}/{len(facts)}, errors: {errors}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    FACTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=== WST Web Source Ingester ===\n")

    print("--- GTFOBins ---")
    facts = ingest_gtfobins()
    if facts:
        print("\n--- Seeding GTFOBins to FV ---")
        seed_gtfobins_to_fv(facts)

    print("\n--- HackTricks ---")
    ingest_hacktricks()

    print("\n--- Done ---")
    print("GTFOBins: seeded directly to FV (structured data, no LLM needed)")
    print("HackTricks: text extracted to extracted/hacktricks_*.txt")
    print("Run 'python3 pipeline.py extract' to LLM-process HackTricks content")
    print("Then 'python3 pipeline.py dedup' and 'python3 pipeline.py seed' to finish")


if __name__ == "__main__":
    main()
