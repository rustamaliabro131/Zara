"""Research a topic and save a structured brief (HUD avatar: Researching)."""
from __future__ import annotations

import os


def research_topic(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    topic = str(params.get("topic") or params.get("query") or "").strip()
    if not topic:
        return "Please tell me what you would like me to research."

    # No initiating speak(): `speak` sends a client turn, which is a whole extra
    # model generation. The model already announces tool calls from its own
    # turn, and the completion line below carries the result.
    if player:
        player.write_log(f"[Research] {topic}")

    try:
        from core.research_skill import get_skill
        brief, filepath = get_skill().execute_research(topic)
    except Exception as e:
        if speak:
            speak("I ran into a problem completing that research.")
        return f"Research failed: {e}"

    filename = os.path.basename(filepath) if filepath else None
    if speak:
        if filename:
            speak(f"Research complete. I have saved the brief as {filename} "
                  f"in your notes folder.")
        else:
            speak("Research complete. I have displayed the detailed report "
                  "in your console.")

    if player and brief:
        try:
            player.show_content(f"Research: {topic}", brief)
        except Exception:
            pass

    result = brief
    if filepath:
        result += f"\n\nSaved to: {filepath}"
    return result


TOOL = {
    "name": "research_topic",
    "description": (
        "Researches a topic and writes a structured brief to Zara's notes "
        "folder. Use this whenever the user asks Zara to research, look into, "
        "or gather information on a subject. The saved file and its location "
        "are announced out loud."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": "The topic or question to research",
            },
        },
        "required": ["topic"],
    },
    "handler": research_topic,
}
