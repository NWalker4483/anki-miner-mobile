"""CC-CEDICT lookup — grounds card generation in real dictionary data.

The bundled `cedict_ts.u8` is parsed once (lazily, on first lookup) into an index
keyed by BOTH simplified and traditional headwords. A single hanzi can have several
readings, each with several senses, so every headword maps to a *list* of entries —
e.g. 行 -> [háng: row/line/trade…, héng: (in 道行)…, xíng: to walk/to go/OK…]. This
is exactly the "same character, different meanings by context" case we want the
card generator to see rather than guess at.

Line format (tone-number pinyin, ü written "u:"):
    Traditional Simplified [pin1 yin1] /sense 1/sense 2/.../

License: CC-CEDICT is CC BY-SA 4.0. The license header is preserved at the top of
cedict_ts.u8; attribution is noted in the project README.
"""
import os
import re
from dataclasses import dataclass

# Overridable so the parser can be pointed at a test fixture or a mounted copy.
_FILE = os.environ.get(
    "CEDICT_PATH", os.path.join(os.path.dirname(__file__), "cedict_ts.u8")
)
_LINE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/\s*$")


@dataclass
class Entry:
    traditional: str
    simplified: str
    pinyin: str          # space-separated tone-number pinyin, e.g. "yin2 hang2"
    senses: list[str]


_INDEX: dict[str, list[Entry]] | None = None


def _build() -> dict[str, list[Entry]]:
    index: dict[str, list[Entry]] = {}
    try:
        fh = open(_FILE, encoding="utf-8")
    except OSError:
        # Missing dictionary must not break the app — grounding just becomes a no-op.
        return index
    with fh:
        for line in fh:
            if not line or line.startswith("#"):
                continue
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            trad, simp, pinyin, body = m.groups()
            senses = [s for s in body.split("/") if s]
            entry = Entry(trad, simp, pinyin, senses)
            for key in {simp, trad}:  # set() so a simp==trad headword isn't double-added
                index.setdefault(key, []).append(entry)
    return index


def _index() -> dict[str, list[Entry]]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _build()
    return _INDEX


def lookup(word: str) -> list[Entry]:
    """Every dictionary entry whose headword is exactly `word` (simplified or traditional)."""
    return _index().get((word or "").strip(), [])


def format_for_prompt(word: str, max_entries: int = 6, max_senses: int = 8) -> str:
    """Compact, authoritative dictionary block for `word` to drop into an LLM prompt.

    Returns "" when the word is not in the dictionary (so the caller can tell the
    model there was no reference rather than inventing one).
    """
    entries = lookup(word)
    if not entries:
        return ""
    lines = [f"{word}:"]
    for e in entries[:max_entries]:
        senses = "; ".join(e.senses[:max_senses])
        lines.append(f"  [{e.pinyin}] {senses}")
    return "\n".join(lines)


def loaded_count() -> int:
    """Number of distinct headwords indexed (0 if the file could not be read)."""
    return len(_index())
