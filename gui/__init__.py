"""
gui — Zara's presentation layer.

For now this package holds the animated GIF avatar. It is deliberately
independent of PyQt6: the avatar is a pure Tkinter component, so it can run
standalone, be embedded in any Tk host, or be driven from Zara's backend
threads through a thread-safe event bus.

Public surface (see gui.avatar):
    AvatarEventBus   — thread-safe state queue
    AvatarController — owns the Tk view + bus + after() pump
    AvatarView       — the Tk widget itself
    GifFrameStore    — PIL frame loader / resizer
    STATES           — the canonical state vocabulary
"""
from __future__ import annotations

__all__ = ["avatar"]
