"""Draft a document and save it to the notes folder (HUD avatar: Writing)."""
from __future__ import annotations

import os


def write_document(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    topic = str(params.get("topic") or params.get("subject") or "").strip()
    if not topic:
        return "Please tell me what the document should be about."
    doc_type = str(params.get("doc_type") or params.get("type")
                   or "document").strip().lower() or "document"
    tone = str(params.get("tone") or "professional").strip().lower() or "professional"

    # No initiating speak(): it is a full extra model generation. The model
    # announces the tool call itself; the completion line carries the result.
    if player:
        player.write_log(f"[Writing] {doc_type}: {topic}")

    try:
        from core.research_skill import get_skill
        draft, filepath = get_skill().execute_writing(
            content_type=doc_type, topic=topic, tone=tone)
    except Exception as e:
        if speak:
            speak(f"I ran into a problem drafting that {doc_type}.")
        return f"Writing failed: {e}"

    filename = os.path.basename(filepath) if filepath else None
    if speak:
        if filename:
            speak(f"Your {doc_type} draft is finished and saved as {filename} "
                  f"in your notes folder.")
        else:
            speak(f"Your {doc_type} draft is finished and printed to the terminal.")

    if player and draft:
        try:
            player.show_content(f"Draft: {topic}", draft)
        except Exception:
            pass

    result = draft
    if filepath:
        result += f"\n\nSaved to: {filepath}"
    return result


TOOL = {
    "name": "write_document",
    "description": (
        "Drafts a document (email, proposal, blog post, report, article) and "
        "saves it to Zara's notes folder. Use this whenever the user asks Zara "
        "to write or draft something. The saved file and its location are "
        "announced out loud."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": "The subject or content of the document",
            },
            "doc_type": {
                "type": "STRING",
                "description": "email, proposal, blog post, report, article, or document",
            },
            "tone": {
                "type": "STRING",
                "description": "professional, friendly, or formal",
            },
        },
        "required": ["topic"],
    },
    "handler": write_document,
}
