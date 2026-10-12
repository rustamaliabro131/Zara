"""Loads the standalone Research & Writing skill for use as a core tool.

The skill ships as ``skills/research_writer.py.py`` in some checkouts (a
double-suffixed filename no normal import can resolve), so it is loaded by
path. A single instance is cached for the process. No API key is passed:
inside the running app ``core.gemini`` already holds the key and the model
ladder, and the skill prefers it over its own client.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional


_SKILL_FILENAMES = ("research_writer.py", "research_writer.py.py")
_MODULE_NAME = "zara_research_writer"
_cache = None


def load_skill_class():
    """Return ``ZaraResearchWriterSkill`` loaded from disk."""
    root = Path(__file__).resolve().parent.parent
    for name in _SKILL_FILENAMES:
        path = root / "skills" / name
        if path.is_file():
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.ZaraResearchWriterSkill
    raise ImportError("skills/research_writer.py(.py) not found")


def get_skill() -> "Optional[object]":
    """Return the shared skill instance, building it on first use."""
    global _cache
    if _cache is None:
        _cache = load_skill_class()(api_key=None)
    return _cache
