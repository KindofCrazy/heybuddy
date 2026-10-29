"""Bounded, index-led topic-body search for explicit main-model requests."""

from personal_agent.memory.models import clip, digest
from personal_agent.memory.retrieval import MemoryLookup, terms
from personal_agent.memory.store import MemoryFS

SCAN_FILES = 16
SCAN_BYTES = 131072


def search_topics(fs: MemoryFS, query: str, *, max_results: int = 5) -> dict:
    lookup = MemoryLookup(fs)
    catalog = lookup.catalogue()
    ranked = lookup.rank(catalog, query)
    priority = {row["path"]: score for score, row in ranked}
    ordered = sorted(catalog, key=lambda row: (-priority.get(row["path"], 0), row["path"]))
    words = terms(query) or [query.strip().lower()]
    results = []
    scanned = used = 0
    with fs.io():
        for row in ordered[:SCAN_FILES]:
            path = fs.path(row["path"])
            if path.stat().st_size > SCAN_BYTES - used:
                break
            raw = fs.raw(path, min(lookup.policy.max_file_bytes, SCAN_BYTES - used))
            scanned += 1
            used += len(raw)
            text = raw.decode("utf-8")
            lower = text.lower()
            positions = [lower.find(word) for word in words if word in lower]
            score = priority.get(row["path"], 0) + sum(word in lower for word in words)
            if not score:
                continue
            start = max(0, min(positions, default=0) - 80)
            content = clip(text[start:], 512)
            results.append(
                {
                    "path": row["path"],
                    "hash": digest(raw),
                    "content": content,
                    "offset": start,
                    "truncated": start > 0 or len(content) < len(text),
                    "score": score,
                }
            )
    results.sort(key=lambda row: (-row["score"], row["path"]))
    return {
        "trust": "data_only",
        "results": results[:max_results],
        "matches_in_scope": len(results),
        "scanned_files": scanned,
        "scope_limited": scanned < len(catalog) or lookup.catalogue_limited,
        "truncated": len(results) > max_results,
    }
