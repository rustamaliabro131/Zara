"""Indexes the Orchestra Research AI-Research-SKILLs library for Zara.

The library ships under ``skills/ai-research-skills/`` as one folder per skill,
each with a YAML-front-mattered ``SKILL.md``. This module scans those files,
caches a small JSON index next to them, and exposes a dependency-free keyword
search so the research skill can pull the most relevant guidance into its
prompt.

Nothing here talks to the network and nothing here raises: a missing library
or an unreadable file just yields an empty result.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


_LIB_DIRNAME = "ai-research-skills"
_INDEX_NAME = ".zara_index.json"
_INDEX_VERSION = 2

# Folders in the repo that are not skills (docs, demos, tooling).
_SKIP_TOP = {
    "demos", "docs", "dev_data", "packages", "scripts", "video-promo",
    "anthropic_official_docs", ".github", ".claude-plugin",
}

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "that", "the", "their", "this",
    "to", "with", "you", "your", "use", "using", "used", "when", "how",
    "what", "which", "while", "can", "has", "have", "was", "were", "will",
    "all", "any", "each", "other", "such", "than", "then", "them", "these",
    "ai", "ml", "model", "models",
}

# The library is entirely AI/ML research engineering. Auto-retrieval only
# happens when the query actually carries an AI/ML signal, so an unrelated
# research request (a historical event, a recipe) never has ML docs injected
# just because one incidental word collided with a skill name.
_DOMAIN_HINTS = {
    "llm", "language", "transformer", "neural", "network", "deep", "learning",
    "machine", "train", "training", "tune", "tuning", "finetune", "lora",
    "qlora", "peft", "adapter", "dataset", "data", "tokenizer", "tokenization",
    "token", "embedding", "vector", "rag", "retrieval", "generation", "agent",
    "tool", "inference", "serving", "server", "vllm", "sglang", "tensorrt",
    "quantization", "quantize", "gpt", "llama", "mistral", "mixtral",
    "diffusion", "multimodal", "vision", "whisper", "clip", "llava", "audio",
    "speech", "reinforcement", "rlhf", "rl", "dpo", "grpo", "ppo",
    "distillation", "prune", "pruning", "sparsity", "evaluation", "benchmark",
    "harness", "pytorch", "torch", "tensorflow", "huggingface", "cuda", "gpu",
    "distributed", "deepspeed", "fsdp", "megatron", "optimization",
    "optimizer", "prompt", "mlops", "observability", "interpretability",
    "mechanistic", "activation", "sae", "attention", "flash", "context",
    "rope", "moe", "expert", "architecture", "diffusers", "stable", "segment",
    "sam", "blip", "faiss", "chroma", "qdrant", "pinecone", "langchain",
    "llamaindex", "crewai", "autogpt", "dspy", "instructor", "guidance",
    "outlines", "wandb", "mlflow", "tensorboard", "langsmith", "phoenix",
    "modal", "skypilot", "lambda", "paper", "thesis", "publication", "latex",
    "neurips", "icml", "iclr", "acl", "aaai", "colml", "conference",
    "plotting", "figure",
}


def library_root() -> Path:
    """Absolute path to the skills library directory."""
    return Path(__file__).resolve().parent.parent / "skills" / _LIB_DIRNAME


def has_domain_signal(query: str) -> bool:
    """True when the query carries an AI/ML term from the library's domain."""
    return bool(set(_tokenize(query)) & _DOMAIN_HINTS)


def _norm_token(t: str) -> str:
    """Very light singular form, so 'embeddings' matches 'embedding'."""
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def _tokenize(text: str) -> list[str]:
    out = []
    for raw in re.findall(r"[a-z0-9]+", (text or "").lower()):
        t = _norm_token(raw)
        if len(t) >= 2 and t not in _STOP:
            out.append(t)
    return out


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    """Split leading ``---`` YAML front matter from the Markdown body."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    block = raw[3:end]
    body = raw[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip().lower()] = val.strip().strip('"').strip("'")
    return meta, body


def _as_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.strip().strip("[]").split(",") if t.strip()]
    return []


def build_index(root: Path | None = None) -> list[dict]:
    """Scan every SKILL.md and return index entries."""
    root = root or library_root()
    entries: list[dict] = []
    if not root.is_dir():
        return entries
    for path in sorted(root.glob("**/SKILL.md")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _SKIP_TOP:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, _body = _parse_front_matter(raw)
        name = str(meta.get("name") or path.parent.name).strip()
        description = str(meta.get("description") or "").strip()
        tags = _as_tags(meta.get("tags"))
        category = rel.parts[0] if len(rel.parts) > 1 else ""
        blob = f"{name} {description} {' '.join(tags)} {category}"
        entries.append({
            "name": name,
            "description": description,
            "tags": tags,
            "category": category,
            "path": str(rel).replace("\\", "/"),
            "tokens": sorted(set(_tokenize(blob))),
        })
    return entries


def _index_path(root: Path) -> Path:
    return root / _INDEX_NAME


def _is_stale(root: Path, index_path: Path) -> bool:
    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        return True
    for path in root.glob("**/SKILL.md"):
        try:
            if path.stat().st_mtime > index_mtime:
                return True
        except OSError:
            continue
    return False


def load_index(rebuild: bool = False, root: Path | None = None) -> list[dict]:
    """Load the cached index, rebuilding it when missing or older than a skill."""
    root = root or library_root()
    index_path = _index_path(root)
    if not rebuild and index_path.is_file() and not _is_stale(root, index_path):
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if (isinstance(data, dict)
                    and data.get("version") == _INDEX_VERSION
                    and isinstance(data.get("entries"), list)):
                return data["entries"]
        except Exception:
            pass
    entries = build_index(root)
    try:
        index_path.write_text(
            json.dumps({"version": _INDEX_VERSION, "entries": entries}),
            encoding="utf-8")
    except OSError:
        pass
    return entries


def search(query: str, limit: int = 3) -> list[dict]:
    """Rank skills against a free-text query (keyword TF-IDF, no deps)."""
    entries = load_index()
    q_tokens = _tokenize(query)
    if not q_tokens or not entries:
        return []

    df: dict[str, int] = {}
    entry_token_sets = []
    for e in entries:
        toks = set(e.get("tokens") or [])
        entry_token_sets.append(toks)
        for t in set(q_tokens):
            if t in toks:
                df[t] = df.get(t, 0) + 1

    n = len(entries)
    q_lower = (query or "").lower().strip()
    q_set = set(q_tokens)
    scored: list[tuple[float, dict]] = []
    for e, toks in zip(entries, entry_token_sets):
        score = 0.0
        matched = 0
        for t in q_tokens:
            if t in toks:
                matched += 1
                score += math.log(1.0 + n / (1.0 + df.get(t, 0)))
        name_hit = bool(set(_tokenize(e.get("name", ""))) & q_set)
        # A lone incidental match (a description or a generic tag happening to
        # contain "best", say) is not enough to inject a whole skill into the
        # prompt: the name must match, or at least two query terms must.
        if not (name_hit or matched >= 2):
            continue
        if name_hit:
            score += 1.5
        if q_lower and q_lower in (e.get("description") or "").lower():
            score += 2.0
        scored.append((score, e))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [e for _score, e in scored[:max(1, limit)]]


def _body(entry: dict, root: Path) -> str:
    path = root / entry.get("path", "")
    try:
        _meta, body = _parse_front_matter(path.read_text(encoding="utf-8",
                                                         errors="replace"))
        return body
    except OSError:
        return ""


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n…"


def build_context(query: str, limit: int = 3, max_chars: int = 2200,
                  excerpt_chars: int = 1400, root: Path | None = None) -> tuple[str, list[dict]]:
    """Return ``(prompt_text, used_entries)`` for the top matching skills."""
    root = root or library_root()
    if not has_domain_signal(query):
        return "", []
    hits = search(query, limit=limit)
    if not hits:
        return "", []
    parts = []
    used = []
    for entry in hits:
        excerpt = _clip(_body(entry, root), excerpt_chars)
        parts.append(
            f"### {entry.get('name')} ({entry.get('category')})\n"
            f"{entry.get('description')}\n\n{excerpt}"
        )
        used.append({"name": entry.get("name"), "path": entry.get("path")})
    header = ("Reference material from Zara's AI Research Skills library — "
              "apply this methodology/engineering guidance and cite the skill "
              "names you use:\n\n")
    text = header + "\n\n".join(parts)
    return _clip(text, max_chars), used


def list_skills() -> list[dict]:
    """All indexed skills as ``{name, category, description, path}``."""
    return [
        {"name": e.get("name"), "category": e.get("category"),
         "description": e.get("description"), "path": e.get("path")}
        for e in load_index()
    ]
