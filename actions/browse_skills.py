"""Search or list Zara's AI Research Skills library (98 skills)."""
from __future__ import annotations


def browse_skills(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    query = str(params.get("query") or params.get("topic") or "").strip()
    try:
        limit = int(params.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 25))

    if player:
        player.write_log(f"[Skills] {query or 'list all'}")

    try:
        from core.skill_library import has_domain_signal, list_skills, search
    except Exception as e:
        return f"Skills library unavailable: {e}"

    if query and not has_domain_signal(query):
        msg = (f"'{query}' does not look like an AI or machine-learning topic, "
               f"so nothing in the library matches. Ask me to list all skills "
               f"to hear the categories.")
        if speak:
            speak(msg)
        return msg

    if query:
        hits = search(query, limit=limit)
        if not hits:
            msg = f"I found no skills matching {query}."
            if speak:
                speak(msg)
            return msg

        lines = [f"# Skills matching '{query}'", ""]
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. **{h.get('name')}** ({h.get('category')})")
            desc = (h.get("description") or "").strip()
            if desc:
                lines.append(f"   {desc}")
            lines.append("")
        body = "\n".join(lines).strip()
        if speak:
            spoken = ", ".join(h.get("name", "") for h in hits[:3])
            noun = "skill" if len(hits) == 1 else "skills"
            speak(f"I found {len(hits)} {noun}. The closest are {spoken}.")
        if player:
            try:
                player.show_content(f"Skills: {query}", body)
            except Exception:
                pass
        return body

    all_skills = list_skills()
    if not all_skills:
        return "The skills library is empty or not installed."

    by_cat: dict[str, list[dict]] = {}
    for s in all_skills:
        by_cat.setdefault(s.get("category") or "other", []).append(s)

    lines = [f"# AI Research Skills library ({len(all_skills)} skills)", ""]
    for cat in sorted(by_cat):
        names = ", ".join(s.get("name", "") for s in by_cat[cat])
        lines.append(f"## {cat} ({len(by_cat[cat])})")
        lines.append(names)
        lines.append("")
    body = "\n".join(lines).strip()

    if speak:
        cats = sorted(by_cat)
        shown = ", ".join(cats[:5])
        more = "" if len(cats) <= 5 else f", and {len(cats) - 5} more"
        speak(f"The library has {len(all_skills)} skills across {len(cats)} "
              f"categories, including {shown}{more}. The full list is on the "
              f"content panel.")
    if player:
        try:
            player.show_content("AI Research Skills library", body)
        except Exception:
            pass
    return body


TOOL = {
    "name": "browse_skills",
    "description": (
        "Searches or lists Zara's AI Research Skills library (about 98 "
        "machine-learning and AI engineering skills). Use this when the user "
        "asks what skills or knowledge Zara has, wants to browse the library, "
        "or wants to find a skill about an AI/ML topic. With no query it lists "
        "every category."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "AI/ML topic to search for, e.g. 'fine-tuning', 'vLLM', 'RAG'",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of matching skills to show (default 8)",
            },
        },
        "required": [],
    },
    "handler": browse_skills,
}
