#web_search.py
import json
import re
import sys
import threading
import time
from pathlib import Path

import requests

# ── Gemini grounding quota circuit breaker ────────────────────────────────────
# The google_search grounding tool has its own small quota, separate from plain
# generation.  Once it is spent every call returns 429 — so retrying it at the
# top of every search only adds a dead round-trip before the DDG fallback runs.
# After a quota error, skip Gemini entirely for a cooldown period.
_QUOTA_COOLDOWN_SEC  = 900          # 15 minutes
_quota_blocked_until = 0.0
_quota_lock          = threading.Lock()


def _gemini_available() -> bool:
    with _quota_lock:
        return time.monotonic() >= _quota_blocked_until


def _note_gemini_error(exc: Exception) -> None:
    """Trip the breaker when the error is a quota / rate-limit rejection."""
    global _quota_blocked_until
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        with _quota_lock:
            already = time.monotonic() < _quota_blocked_until
            _quota_blocked_until = time.monotonic() + _QUOTA_COOLDOWN_SEC
        if not already:
            print(
                "[WebSearch] Gemini grounding quota exhausted — skipping it for "
                f"{_QUOTA_COOLDOWN_SEC // 60} min and serving results from DDG."
            )


class _QuotaCooldown(RuntimeError):
    """Raised instead of calling Gemini while the quota breaker is open."""


def _log_gemini_failure(context: str, exc: Exception) -> None:
    """Log a Gemini failure — silently when it is just the expected cooldown."""
    if isinstance(exc, _QuotaCooldown):
        return          # announced once when the breaker tripped; not a warning
    print(f"[WebSearch] \u26a0\ufe0f {context} failed ({exc}) — using DDG instead")


def _run_bounded(fn, timeout: float, label: str = "task"):
    """Run fn() in a daemon thread; return its result, or None if it overruns."""
    box = [None]

    def _run():
        try:
            box[0] = fn()
        except Exception as e:
            _log_gemini_failure(label, e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[WebSearch] {label} exceeded {timeout:.0f}s — moving on")
    return box[0]

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# This module logs with emoji (🔍 ⚠️ ❌) and those code points are not cp1252.
# On Windows a console left on the OEM code page, which is what `python main.py`
# from cmd.exe gives, raises UnicodeEncodeError out of the print — and inside an
# except block, so it replaced the intended "falling back to DDG" message with a
# crash and took the whole fallback ladder down with it. Reconfigure once, with
# errors="replace" so an unencodable character costs a glyph instead of the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _config() -> dict:
    """The whole api_keys.json, or {} when it is missing or unreadable.

    Every credential in this project lives in that one file, which is git-ignored
    — see the secrets block in .gitignore. It used to be reached through
    _get_api_key(), which indexed the dict directly and let a missing file raise
    KeyError/FileNotFoundError out of a search. A backend that cannot find its
    own credentials is a normal state, not a crash.
    """
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WebSearch] ⚠️ {API_CONFIG_PATH.name} unreadable ({e})")
        return {}


def _get_api_key() -> str:
    return _config().get("gemini_api_key", "")


def _gemini_search(query: str) -> str:
    if not _gemini_available():
        raise _QuotaCooldown("Gemini grounding is in quota cooldown")

    from core import gemini

    # Grounded search reads a live page, so it gets a longer deadline than the
    # default — but it still HAS one, and it still walks the fallback ladder.
    try:
        response = gemini.call(query, tier=gemini.SEARCH,
                               config={"tools": [{"google_search": {}}]},
                               timeout_ms=30_000)
        if response is None:
            raise RuntimeError("every Gemini model on the ladder failed")
    except Exception as e:
        _note_gemini_error(e)
        raise

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _get_ddgs():
    """
    Returns the DDGS class.  The package was renamed duckduckgo-search -> ddgs;
    the legacy package's endpoints are now rejected by DuckDuckGo (news() gets a
    403 Ratelimit, text() silently returns zero results), so warn loudly if we
    end up on it instead of failing in silence.
    """
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        print(
            "[WebSearch] ⚠️ Using the deprecated 'duckduckgo-search' package — "
            "DuckDuckGo blocks its endpoints, so every search will come back "
            "empty.  Fix with:  pip install -U ddgs"
        )
        return DDGS


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("href",   ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG text() failed: {e}")
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
    # Also covers the legacy-package case, where news() returns an empty list
    # instead of raising.
    if not results:
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Google Custom Search — the last rung ───────────────────────────────────────
# A third backend, behind Gemini and DDG. It goes last on purpose: the free tier
# allows 100 queries a day per key, so it is the one most expensive to spend and
# the one whose quota is easiest to exhaust by accident. It is worth having as a
# floor because both other rungs can be out at the same time — grounding runs on
# a small separate quota, and DDG rate-limits — and a search that returns nothing
# is a dead end for the user.
#
# The key and the engine id come from config/api_keys.json, never from a literal
# in this file. They were once hardcoded in skills/web_search.py.py, a stray
# file nothing imported; wiring search up is exactly the moment that stops
# being harmless, so they moved into the git-ignored config instead.
_CSE_URL = "https://www.googleapis.com/customsearch/v1"


def _cse_credentials() -> tuple[str, str]:
    cfg = _config()
    return ((cfg.get("google_cse_api_key")   or "").strip(),
            (cfg.get("google_cse_engine_id") or "").strip())


def google_search(query: str, num_results: int = 3) -> str:
    """Raw snippets from Google Custom Search, numbered. '' when unavailable.

    Numbered on purpose: this is DATA handed back to the model, never spoken.
    core/prompt.txt tells it to work out what a result means and say that in its
    own words, and to never read the shape of the result out. So a bare list of
    titles here is correct — an empty string, by contrast, is the model given
    nothing to work with and no way to tell that apart from a search that
    genuinely found nothing.
    """
    key, cx = _cse_credentials()
    if not key or not cx:
        print("[WebSearch] Google CSE not configured "
              "(set google_cse_api_key + google_cse_engine_id in "
              f"{API_CONFIG_PATH.name}) — rung unavailable")
        return ""

    num = max(1, min(int(num_results or 3), 10))   # the API itself caps at 10
    try:
        resp = requests.get(_CSE_URL, timeout=5, params={
            "key": key, "cx": cx, "q": query, "num": num,
        })
        resp.raise_for_status()
        items = (resp.json() or {}).get("items") or []
    except Exception as e:
        print(f"[WebSearch] ⚠️ Google CSE failed: {e}")
        return ""

    lines: list[str] = []
    for i, item in enumerate(items[:num], 1):
        title   = (item.get("title")   or "").strip()
        snippet = (item.get("snippet") or "").strip()
        lines.append(f"{i}. {title}\n   {snippet}" if title else f"{i}. {snippet}")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from core import gemini

    response = gemini.call(
        f"Current world news: {n} headlines. Numbered list, titles only.",
        tier=gemini.SEARCH,
        config={"tools": [{"google_search": {}}]},
        timeout_ms=30_000,
    )
    if response is None:
        return [], ""

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, then DDG, then Google CSE.

    Gemini and DDG keep the order and the behaviour they always had; Google CSE is
    appended as a third rung and only runs when DDG came back with nothing. The
    one change to the old flow is that _format_ddg() is no longer called
    unconditionally: it turns an empty result list into the string "No results
    found", which used to end the ladder, so falling through now means checking
    the list itself first.
    """
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini search", e)

    results = _ddg_search(query)
    if results:
        return _format_ddg(query, results)

    cse = google_search(query)
    if cse:
        return f"Search results for: {query}\n\n{cse}"

    return f"No results found for: {query}"


def _news(query: str) -> str:
    """
    DDG first, Gemini as backup.

    The old version raced both backends in parallel and kept the first answer.
    That burned one google_search grounding call on *every* news request —
    including the startup briefing — even when DDG had already won the race.
    Grounding has a small quota, so it ran dry after a handful of launches and
    then 429'd for everything else (research/compare), which are the modes that
    actually need a synthesised answer.

    DDG news returns in well under a second and gives raw headlines, which is
    exactly what the briefing wants, so it goes first and Gemini is only touched
    when DDG comes back empty.
    """
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    def _ddg_attempt() -> str:
        return _format_news(ddg_query, _ddg_news(ddg_query, max_results=8))

    text = _run_bounded(_ddg_attempt, timeout=5.0, label="DDG news")
    if text and len(text) > 60 and not text.startswith("No news found"):
        return text

    text = _run_bounded(
        lambda: _gemini_search(gemini_query), timeout=6.0, label="Gemini news"
    )
    if text and len(text) > 60:
        return text

    return f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        return _gemini_search(research_query)
    except Exception as e:
        _log_gemini_failure("Gemini research", e)
        results = _ddg_search(query, max_results=10)
        return _format_ddg(query, results)


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        _log_gemini_failure("Gemini price", e)
        results = _ddg_search(f"{query} price buy", max_results=6)
        return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini compare", e)

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Voice pipeline ─────────────────────────────────────────────────────────────
# The tool path above hands its result back to the Gemini Live model, which
# synthesises the spoken reply itself and has already been told in
# core/prompt.txt how a search result must be turned into speech. Nothing below
# is on that path.
#
# This is the standalone path: a caller that wants a finished sentence plus audio
# without going through a Live session. It deliberately does NOT replace the tool
# path. Read the warning on process_zara_research_intent() before wiring it into
# a running session.

VOICE_SUMMARY_SYSTEM_PROMPT = """You are Zara, a direct, natural-sounding voice assistant.
Your job is to read search results and synthesize a SINGLE spoken sentence that answers the user's question directly.

STRICT CONSTRAINTS:
1. Speak in exactly 1 or 2 concise sentences (maximum 30 words total).
2. Do NOT use markdown (*, **, #), bullet points, URLs, or special characters.
3. Do NOT say phrases like "Based on my search results", "According to the web", or "Here is what I found".
4. State the core factual answer immediately and naturally.
5. If the search results do not contain the answer, say: "I couldn't find a clear answer for that right now."
"""

NO_ANSWER = "I couldn't find a clear answer for that right now."

# A URL as it actually turns up in search snippets: usually a bare domain with no
# scheme ("example.com/page"), sometimes "www.", usually with a path and query
# string behind it. Matching only https?:// left the common case untouched, and
# the common case is the one a TTS engine would read character by character.
# The TLD list is deliberate rather than a catch-all \S+ : a generic pattern eats
# ordinary prose the moment a sentence happens to contain a dot ("config.py",
# "3.5 points"), and silently deleting half a sentence is worse than leaving a
# stray token in.
_URL_RE = re.compile(
    r"\b(?:(?:https?://|www\.)[^\s]+"
    r"|[a-z0-9-]+(?:\.[a-z0-9-]+)*"
    r"\.(?:com|net|org|io|co|dev|ai|info|biz|me|uk|us|de|fr|jp|xyz)\b[^\s]*)",
    re.IGNORECASE,
)


def _clean_spoken(text: str) -> str:
    """Strip the formatting a text model reaches for even when told not to.

    The prompt forbids it; this removes it anyway. The cost of a leftover '**' is
    that a TTS engine pronounces it, so the instruction is not enough on its own.
    Fenced blocks and URLs go entirely rather than being unwrapped — there is no
    sentence to salvage from either, and the rest of the line survives.
    """
    if not text:
        return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"[*_`#>|]+", " ", text)
    text = re.sub(r"^\s*[-–—+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+",   " ", text)
    return text.strip(" -*_`\n\t")


def generate_zara_spoken_response(user_query: str, search_snippets: str) -> str:
    """Turn raw snippets into one or two speakable sentences.

    Goes through core.gemini rather than a bespoke client because that module
    already owns the model ladder, the per-model cooldown and the API key — a
    second HTTP path here would have to re-implement all three to fail the same
    way. Returns '' on failure; the caller decides what an empty answer means.
    """
    snippets = (search_snippets or "").strip()
    if not snippets:
        # Nothing was fetched. Say so rather than handing an empty prompt to the
        # model, which would otherwise answer from its own memory and present a
        # guess as something the search found.
        return ""

    from core import gemini

    user_prompt = (
        f"User Question: {user_query}\n\n"
        f"Raw Search Results:\n{snippets}\n\n"
        "Synthesize a natural spoken answer:"
    )
    try:
        raw = gemini.text(
            user_prompt,
            tier=gemini.SMART,
            config={
                "system_instruction": VOICE_SUMMARY_SYSTEM_PROMPT,
                "temperature": 0.2,      # low, so the answer stays on the snippets
            },
            timeout_ms=20_000,
        )
    except Exception as e:
        print(f"[WebSearch] ⚠️ spoken summary failed: {e}")
        return ""

    spoken = _clean_spoken(raw)
    if not spoken:
        print("[WebSearch] ⚠️ spoken summary came back empty after cleaning")
    return spoken


# Leading noise the phrasing is built around. Longest first, so "find out about"
# is not shadowed by a shorter prefix that happens to match the same opening.
_QUERY_PREFIXES = (
    "find out about", "look things up about", "search the web for",
    "google search for", "search online for", "search up on",
    "search for", "look up", "look up on", "google", "search",
)

_QUERY_TRIM = " \t?!.,:;\"'"


def _extract_query(command: str) -> str:
    """Pull the query out of a spoken research request.

    The tail of the sentence is kept in its original capitalisation. Lowercasing
    the whole command — the obvious implementation — throws away the casing of
    every proper noun in it, and a search for "who is the ceo of openai" finds
    less than "who is the ceo of OpenAI".
    """
    q = (command or "").strip()
    low = q.lower()
    for prefix in _QUERY_PREFIXES:
        if low.startswith(prefix):
            q = q[len(prefix):].strip()
            break
    return q.strip(_QUERY_TRIM)


def _looks_like_explicit_search(command: str) -> bool:
    """True when the command names a search outright.

    Shares _QUERY_PREFIXES with _extract_query deliberately. The recogniser and
    the extractor have to agree on what counts as a search request, and a second
    hand-written list is exactly how the two drift — Case A then pulls a query out
    of a command the recogniser did not consider a search, or searches on a phrase
    it then fails to strip.
    """
    text = (command or "").strip().lower()
    return any(text.startswith(prefix) for prefix in _QUERY_PREFIXES)


def _speak_off_thread(text: str, player=None) -> None:
    """Hand `text` to the offline TTS player without blocking the caller.

    TTSPlayer.speak() is a blocking call by design (core/tts.py) — it holds a
    lock across synthesis and playback. Calling it inline would stall whatever
    thread asked for the answer, so it goes to a daemon thread.
    """
    if not text:
        return
    try:
        if player is None:
            from core.tts import create_tts_player
            player = create_tts_player(_config())
        threading.Thread(target=player.speak, args=(text,),
                         daemon=True, name="zara-voice-out").start()
    except Exception as e:
        print(f"[WebSearch] ⚠️ TTS handoff failed: {e}")


def process_zara_research_intent(command: str, player=None) -> str:
    """Fetch snippets, reduce them to one spoken sentence, play it.

    Returns the spoken text as well as speaking it, so a caller that renders to
    a panel or logs the turn does not have to wait on the audio to know what was
    said.

    WARNING — this is not the path a Live session should use. During a session the
    Live model is already speaking, in its own voice, and its own TTS output is
    mixed into the same output stream that player.speak() writes to. Calling this
    mid-session therefore produces two voices and, if the caller is also feeding
    the result back as a tool response, the answer twice. It is for use when there
    is no open session, or when the caller has muted Live audio deliberately.
    """
    query = _extract_query(command)
    if not query:
        return "Please provide a search query."

    print(f"[WebSearch] 🎙️  voice pipeline — query={query!r}")
    snippets = google_search(query, num_results=3)
    spoken   = generate_zara_spoken_response(query, snippets) or NO_ANSWER

    print(f"[WebSearch] 🔊 {spoken}")
    _speak_off_thread(spoken, player)
    return spoken


# ── Search context memory ──────────────────────────────────────────────────────
# The last search and its RAW SNIPPETS, so a follow-up can be answered from what
# was actually retrieved rather than fetched a second time.
#
# This is not memory/memory_manager.py. That one stores facts worth keeping across
# sessions — preferences, identity, decisions — and deliberately does not keep
# document text. This holds the evidence behind one conversation's search, for
# minutes, and is thrown away. Storing snippets there instead would outlive the
# conversation that produced them and quietly answer a question from last week's
# results.
#
# WHY SNIPPETS AND NOT THE PREVIOUS SPOKEN ANSWER: a pronoun resolves against the
# evidence, not against what was said last turn. Handing the model its own prior
# sentence invites it to restate that sentence — including any part of it that was
# wrong — and then to present the restatement as something the sources support.
# The previous answer is kept for bookkeeping only and is never sent as grounding.

MEMORY_TTL_SEC = 600.0        # 10 minutes


class ZaraMemory:
    """The previous search: its query, its raw snippets, what was said about it.

    `last_spoken_response` is the most recent utterance, which after a follow-up
    is NOT the answer to `last_query` — it is the answer to the follow-up. That is
    deliberate: the snippets stay pinned to the search that produced them so a
    later turn still grounds against the right evidence, and the spoken line just
    tracks the conversation. note_follow_up() is the only thing that moves it.
    """

    def __init__(self, ttl_seconds: float = MEMORY_TTL_SEC):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self.last_query            = None
        self.last_snippets         = None
        self.last_spoken_response  = None
        self._stored_at            = 0.0
        self._followed_up_at        = 0.0

    # ── writes ────────────────────────────────────────────────────────────────
    def update_search_context(self, query: str, snippets: str,
                              spoken_response: str) -> None:
        """Pin a fresh search and everything derived from it."""
        with self._lock:
            self.last_query           = query
            self.last_snippets        = snippets or None
            self.last_spoken_response = spoken_response or None
            now = time.monotonic()
            self._stored_at     = now
            self._followed_up_at = now

    def note_follow_up(self, spoken_response: str) -> None:
        """Record a follow-up answer. The snippets are NOT re-pinned."""
        with self._lock:
            self.last_spoken_response = spoken_response or None
            self._followed_up_at = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self.last_query           = None
            self.last_snippets        = None
            self.last_spoken_response = None
            self._stored_at           = 0.0
            self._followed_up_at      = 0.0

    # ── reads ─────────────────────────────────────────────────────────────────
    def is_fresh(self) -> bool:
        """True while the pinned snippets are still recent enough to reuse.

        Without this a follow-up forty minutes later, on a different subject,
        gets answered from three snippets fetched for something else entirely —
        and the model, told those snippets are the context, has no way to tell
        the difference.
        """
        with self._lock:
            if not self.last_snippets:
                return False
            return (time.monotonic() - self._stored_at) <= self.ttl_seconds

    def age(self) -> float:
        with self._lock:
            return 0.0 if not self._stored_at else time.monotonic() - self._stored_at

    def has_context(self) -> bool:
        return self.is_fresh()

    def context_block(self) -> str:
        """The follow-up grounding block, or '' when there is nothing usable.

        Gated on is_fresh(). This is the only thing generate_memory_aware_response
        reads, so a freshness check that lived only in is_fresh() would not
        actually stop a stale block from being sent — the TTL would be advisory.

        Deliberately omits last_spoken_response — see WHY SNIPPETS AND NOT THE
        PREVIOUS SPOKEN ANSWER above.
        """
        with self._lock:
            if not self.last_snippets or not self.last_query:
                return ""
            if (time.monotonic() - self._stored_at) > self.ttl_seconds:
                return ""
            return (f"Previous Query: {self.last_query}\n"
                    f"Previous Search Snippets:\n{self.last_snippets}")


zara_memory = ZaraMemory()      # module-level: one context per process


VOICE_MEMORY_SYSTEM_PROMPT = """
You are Zara, a direct, natural-sounding voice assistant.
Answer the user's current question directly in spoken prose.

STRICT CONSTRAINTS:
1. Speak in exactly 1 or 2 concise sentences (maximum 30 words total).
2. Do NOT use markdown (*, **, #), bullet points, URLs, or special characters.
3. Do NOT mention searching, memory, or metadata (e.g., avoid "Based on my memory").
4. State the core factual answer immediately and naturally.
5. A pronoun in the question refers back to the Previous Query below. Resolve it
   there and answer the question actually asked — do not restate the previous
   answer, and do not re-answer the previous query.
6. If the snippets do not contain the answer, say: "I couldn't find a clear answer
   for that right now." Never fill the gap from your own knowledge and never
   substitute the nearest related thing the snippets do mention.
"""


def generate_memory_aware_response(user_query: str,
                                   current_snippets: str = None) -> str:
    """Answer from current snippets, or from the pinned previous search.

    Returns '' when there is nothing to ground on — no current snippets and no
    fresh memory. That is a distinct outcome from "I couldn't find a clear
    answer", and conflating the two is how a follow-up ends up answered from the
    model's own recollection: an empty context still looks like a context, and the
    prompt does not forbid answering without one. The caller must decide what an
    ungrounded question becomes — normally a real search.
    """
    snippets = (current_snippets or "").strip()
    context  = (f"Current Search Snippets:\n{snippets}\n" if snippets
                else zara_memory.context_block())

    if not context.strip():
        return ""

    from core import gemini

    user_prompt = f"{context}\nUser Question: {user_query}\n\nSynthesize a natural spoken answer:"
    try:
        raw = gemini.text(
            user_prompt,
            tier=gemini.SMART,
            config={
                "system_instruction": VOICE_MEMORY_SYSTEM_PROMPT,
                "temperature": 0.2,
            },
            timeout_ms=20_000,
        )
    except Exception as e:
        print(f"[WebSearch] ⚠️ memory-aware summary failed: {e}")
        return ""

    spoken = _clean_spoken(raw)
    if not spoken:
        print("[WebSearch] ⚠️ memory-aware summary came back empty after cleaning")
    return spoken


# ── Follow-up detection ────────────────────────────────────────────────────────
# Tokens that cannot be resolved without something said just before. Kept to
# pronouns and demonstratives ONLY.
#
# "who", "where", "why" and "more" were in the first draft of this list and had
# to come out: they appear in standalone questions constantly ("who is the CEO of
# OpenAI", "where do I go next"), and treating those as follow-ups means the
# model can never run a second search and is handed the previous search's
# snippets instead. It answers them confidently, from the wrong evidence, with
# nothing in the prompt telling it that is what happened.
_FOLLOW_UP_TOKENS = frozenset({
    "he", "she", "it", "they", "him", "her", "them", "his", "hers", "their",
    "theirs", "that", "this", "those", "these", "then", "there", "they're",
})

# A leading continuation word is the other reliable signal.
_FOLLOW_UP_LEAD = re.compile(
    r"^\s*(?:and\s+then|and\s+what\s+about|and\s+how\s+about|but\s+what\s+about|"
    r"what\s+about|how\s+about|tell\s+me\s+more|say\s+more|go\s+on|anything\s+else)\b",
    re.IGNORECASE,
)

# Pronoun matching cannot tell a deictic that refers outwards ("how much is it")
# from one the sentence answers itself ("what time is it"). The second is a clock
# or identity question that has nothing to do with the pinned search, and it is
# common enough that routing it to three web snippets is a visible failure rather
# than an edge case. Short list, and it is a patch on a heuristic, not a fix —
# there is no list that makes pronoun detection sound. Anything not caught here
# and still ambiguous resolves to Case C, where the normal handlers take over,
# which is the safe direction to be wrong in.
_SELF_CONTAINED = re.compile(
    r"^\s*(?:what(?:'s|\s+is|\s+s)?\s+(?:the\s+)?time"
    r"|time\s+is\s+it"
    r"|who\s+am\s+i"
    r"|what(?:'s|\s+is|\s+are)?\s+my\s+name"
    r"|where\s+am\s+i"
    r"|what\s+day\s+is\s+it)\b",
    re.IGNORECASE,
)

# ...but a system command is not a question, however many pronouns it contains.
# "close it" and "mute it" both carry a deictic and both must never be answered
# from web snippets, so any action verb here vetoes the follow-up outright.
_SYSTEM_VERBS = frozenset({
    "open", "close", "launch", "run", "start", "stop", "quit", "exit", "kill",
    "mute", "unmute", "pause", "resume", "play", "volume", "brightness",
    "set", "increase", "decrease", "raise", "lower", "toggle", "restart",
    "shutdown", "sleep", "lock", "unlock", "screenshot", "type", "click",
    "scroll", "copy", "paste", "save", "delete", "rename", "move", "undo",
    "wifi", "bluetooth", "shutdown_zara", "scroll_down", "scroll_up",
})

_WORD_RE = re.compile(r"[a-z0-9_'-]+", re.IGNORECASE)


def is_follow_up(command: str) -> bool:
    """True when `command` can only be answered against the pinned search.

    Two conditions, both required: there is fresh context to answer from, and the
    command itself is unanswerable on its own. When memory is empty or stale this
    returns False, so the command falls through to a real search instead of being
    answered from nothing.
    """
    if not zara_memory.has_context():
        return False

    text = (command or "").strip()
    if not text:
        return False

    words = _WORD_RE.findall(text.lower())
    if not words:
        return False

    if _FOLLOW_UP_LEAD.match(text):
        return True

    if _SELF_CONTAINED.match(text):
        return False

    # An action verb means this is a system command, not a question.
    if any(w in _SYSTEM_VERBS for w in words):
        return False

    return any(w in _FOLLOW_UP_TOKENS for w in words)


def process_zara_command(command: str, player=None):
    """Route a spoken command: new search, contextual follow-up, or not ours.

    Returns the spoken text, or None when the command is none of this module's
    business and the caller should hand it to the normal handlers. None and '' are
    different on purpose: '' means a search ran and produced nothing speakable,
    None means no search was attempted at all.

    The old `pass` in the fallback branch left the caller with no way to tell
    "handled" from "ignored", which is the whole decision this function exists to
    make.
    """
    if not (command or "").strip():
        return None

    query = _extract_query(command)

    # ── Case A: an explicit new search ────────────────────────────────────────
    # _extract_query, not str.replace on a lowercased copy — that one threw away
    # the casing of every proper noun in the query.
    if query and _looks_like_explicit_search(command):
        print(f"[WebSearch] 🎙️  new search — {query!r}")
        raw    = google_search(query, num_results=3)
        spoken = generate_memory_aware_response(query, current_snippets=raw) or NO_ANSWER
        zara_memory.update_search_context(query, raw, spoken)
        print(f"[WebSearch] 🔊 {spoken}")
        _speak_off_thread(spoken, player)
        return spoken

    # ── Case B: a follow-up against the pinned search ──────────────────────────
    if is_follow_up(command):
        age = zara_memory.age()
        print(f"[WebSearch] 🎙️  follow-up ({age:.0f}s old context) — {command!r}")
        spoken = generate_memory_aware_response(command)
        if not spoken:
            # The context existed a moment ago and has gone unusable. Fall through
            # to a real search on the command itself rather than answering from
            # whatever the model happens to know.
            print("[WebSearch] ⚠️ follow-up had no usable context — searching instead")
        else:
            zara_memory.note_follow_up(spoken)
            print(f"[WebSearch] 🔊 {spoken}")
            _speak_off_thread(spoken, player)
            return spoken

    # ── Case C: not ours ──────────────────────────────────────────────────────
    return None


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "web_search",
    "description": "Searches the web. Use for ANY question about current facts, events, prices, or topics — always prefer this over guessing. Modes: 'search' (default), 'news' (latest headlines on a topic), 'research' (deep comprehensive answer), 'price' (product cost lookup), 'compare' (side-by-side comparison of items).",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Search query or topic"
            },
            "mode": {
                "type": "STRING",
                "description": "search | news | research | price | compare"
            },
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING"
                },
                "description": "Items to compare (compare mode)"
            },
            "aspect": {
                "type": "STRING",
                "description": "Comparison aspect: price | specs | reviews | features"
            }
        },
        "required": [
            "query"
        ]
    },
    "handler": web_search,
}
