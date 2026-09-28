"""
core/brand.py — the single source of truth for who this assistant is.

Every user-visible name, the credits line, the wake phrase and the default
voice used to be written out by hand in a dozen places, and they drifted apart:
the window said one thing, the system prompt said another, the footer credited
someone else entirely, and the voice that came out was the wrong one.

Nothing in this module is configurable and nothing here touches the config file
or the network. It is the floor everything else is built on, so it has to stay
importable before anything else has run — no Qt, no google-genai, no side
effects, no disk access.
"""
from __future__ import annotations

# ── Identity ────────────────────────────────────────────────────────────────
# The name the assistant answers to, the name printed on the window, and the
# one-liner under the header.
ASSISTANT_NAME  = "Zara"
ASSISTANT_TITLE = "ZARA"
ASSISTANT_TAGLINE = "Sophisticated AI Desktop Assistant"

# Names an older build may have written into config, a live session, or an
# autostart entry. They all resolve to Zara rather than showing up in the UI as
# a second, competing identity.
LEGACY_ASSISTANT_NAMES = ("JARVIS", "J.A.R.V.I.S", "MARK", "MARK LIV", "ZARA AI")


def resolve_name(configured: str | None) -> str:
    """Map whatever is stored in config onto the current name.

    A config written by an older build still holds "JARVIS". Showing that would
    put two different names on one screen, so an unset or legacy value falls
    back to the current default and only a name the user actually chose is
    honoured. Never raises — config is optional.
    """
    name = (configured or "").strip()
    if not name or name.upper() in LEGACY_ASSISTANT_NAMES:
        return ASSISTANT_NAME
    return name


def is_legacy_name(name: str | None) -> bool:
    """True when `name` is a pre-Zara identity that should be migrated."""
    return (name or "").strip().upper() in LEGACY_ASSISTANT_NAMES


# ── Official credits ────────────────────────────────────────────────────────
# Requested branding, shown in the header and the footer. The developer and
# studio are separated so a longer credit block can compose from the same parts
# instead of hard-coding the whole string a second time.
DEVELOPER = "Rustam"
STUDIO    = "Ex-KWK Creatives"

CREDITS         = f"Developed by {DEVELOPER} | {STUDIO}"
CREDITS_SHORT   = f"{DEVELOPER} · {STUDIO}"
CREDITS_COMPACT = f"{DEVELOPER}/{STUDIO}"


# ── Release name ────────────────────────────────────────────────────────────
# The product codename. The window title, the header badge, the dashboard and
# the readme all read it from here, which is the only reason they can be
# trusted to agree.
APP_VERSION  = "MARK LIV"
APP_PROTOCOL = APP_VERSION.split()[-1]


# ── Voice ───────────────────────────────────────────────────────────────────
# The wake phrase exactly as the user says it. The acoustic model that listens
# for it is named separately in core/wake_word.py — read WAKE_MODEL there for
# why the two differ.
WAKE_PHRASE = "Hey Zara"

# Prebuilt Gemini Live voices. Ordered female-first, and the default is female:
# the voice is part of the identity, not a preference that happened to ship
# defaulting to the wrong one.
PREFERRED_VOICE  = "Aoede"
FEMALE_VOICES    = ("Aoede", "Kore", "Puck")
AVAILABLE_VOICES = ("Aoede", "Kore", "Puck", "Charon", "Fenrir")

# Voices an older build may have stored. Migrated to PREFERRED_VOICE on sight
# unless the user has since picked a voice themselves — see get_voice().
MALE_LEGACY_VOICES = ("Charon", "Fenrir")


def is_female_voice(name: str | None) -> bool:
    """True when `name` is one of the prebuilt voices that read as female."""
    return (name or "").strip() in FEMALE_VOICES


def resolve_voice(configured: str | None, *, locked: bool = False) -> str:
    """Map a stored voice name onto a voice that is safe to send to the API.

    Unknown names collapse to the default so a bad value can never reach the
    Live endpoint and fail the whole session at connect time.

    `locked` means the user picked this voice deliberately in the picker, in
    which case it is honoured even if it is one of the older male defaults.
    Without it, a value that is merely the leftover default is upgraded to the
    female default — an existing install adopts Zara's voice on upgrade instead
    of keeping the voice it had before the rename.
    """
    name = (configured or "").strip()
    if not name:
        return PREFERRED_VOICE
    if name in AVAILABLE_VOICES:
        return PREFERRED_VOICE if (name in MALE_LEGACY_VOICES and not locked) else name
    return PREFERRED_VOICE


# ── System control ──────────────────────────────────────────────────────────
# Actions that touch the machine itself. Kept here so the prompt, the tool
# declaration and the confirmation copy cannot disagree about which ones put a
# button on the user's screen.
#
# lock_screen is deliberately NOT in this list: locking the workstation is
# instant and fully reversible by the person in front of it, so a prompt for it
# would only add latency to a request that is already unambiguous.
GATED_ACTIONS = ("restart", "shutdown", "toggle_wifi")
