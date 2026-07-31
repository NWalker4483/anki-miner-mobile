"""Gemini pipeline for the Screenshot → Anki flow.

All LLM I/O is JSON, and every JSON contract is a Pydantic model passed to the
google-genai SDK as `response_schema`; the SDK validates the model output and
returns typed instances (`response.parsed`).

Two LLM stages live here (stage 2 — deck search — is AnkiConnect and lives in
app.py):

  1. analyze_image()  — image -> ImageAnalysis (what it shows + Mandarin terms)
  3. generate_cards() — new terms + known vocab -> GeneratedCards
"""
import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import cedict

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


# --- JSON contracts -------------------------------------------------------

class MandarinTerm(BaseModel):
    """A single Mandarin keyword or short phrase found in / relevant to an image."""
    hanzi: str = Field(description="The word or short phrase in Chinese characters.")
    pinyin: str = Field(default="", description="Pinyin with tone marks.")
    meaning: str = Field(description="Concise English meaning.")


class ImageAnalysis(BaseModel):
    """Stage 1 output: what the image is about + the Mandarin vocabulary in it."""
    description: str = Field(description="One or two sentences on what the image shows.")
    terms: list[MandarinTerm] = Field(
        description="Key Mandarin words/phrases present or clearly relevant, learner-useful."
    )


class GeneratedCard(BaseModel):
    """Stage 3 output: one flashcard for a new word, shaped for the HSK note type."""
    simplified: str = Field(description="The target word in simplified Chinese characters.")
    traditional: str = Field(default="", description="Traditional-character form (same as simplified when there is no difference).")
    pinyin: str = Field(description="Pinyin of the word, with tone marks (diacritics).")
    meaning: str = Field(description="Concise English definition.")
    part_of_speech: str = Field(default="", description="Part of speech, e.g. noun, verb, adjective, measure word.")
    example: str = Field(description="A natural example sentence in simplified Chinese that uses the word.")
    example_pinyin: str = Field(description="Pinyin of the example sentence, with tone marks (diacritics).")
    example_meaning: str = Field(description="English translation of the example sentence.")


class GeneratedCards(BaseModel):
    cards: list[GeneratedCard]


# --- Gemini calls ---------------------------------------------------------

_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    # Cache the client for the process lifetime. A per-call client gets
    # garbage-collected mid-request and its finalizer closes the httpx pool
    # ("Cannot send a request, as the client has been closed").
    global _CLIENT
    if _CLIENT is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        _CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return _CLIENT


_ANALYZE_PROMPT = (
    "You are helping a Mandarin learner who CANNOT yet read or understand the "
    "content shown in this image. The flashcards you produce are what will let them "
    "comprehend it, so aim to cover everything needed to interpret the message.\n"
    "1. Briefly describe what the image shows or contains.\n"
    "2. Consider the overall meaning and intent of the text, then break it into the "
    "meaningful units a learner must know to understand it. Extract not just single "
    "words but ALSO multi-character compounds, set phrases, and the common "
    "sub-phrases that appear or are clearly implied — don't skip a compound or "
    "phrase just because its individual characters look simple, since its combined "
    "meaning may not be obvious. Prefer the meaningful chunk over its isolated "
    "characters. Avoid extracting entire full sentences verbatim and trivial "
    "standalone particles.\n"
    "For each term give the hanzi, pinyin (with tone marks), and a concise English meaning."
)


def analyze_image(image_bytes: bytes, mime_type: str) -> ImageAnalysis:
    resp = _client().models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            _ANALYZE_PROMPT,
        ],
        config={"response_mime_type": "application/json", "response_schema": ImageAnalysis},
    )
    if resp.parsed is None:
        raise RuntimeError(f"Gemini returned no parseable analysis: {resp.text!r}")
    return resp.parsed


def generate_cards(target_words: list[str], known_vocab: list[str]) -> GeneratedCards:
    """Generate a card per target word, leaning example sentences on known vocab.

    Each target word is grounded in CC-CEDICT: its real readings and senses are
    handed to the model as the source of truth, so pinyin/meaning come from the
    dictionary (with the context-appropriate sense chosen) rather than being guessed.
    """
    known = "、".join(known_vocab) if known_vocab else "(none known yet)"
    targets = "\n".join(f"- {w}" for w in target_words)

    ref_blocks = [b for w in target_words if (b := cedict.format_for_prompt(w))]
    references = "\n".join(ref_blocks) if ref_blocks else "(no dictionary entries matched the target words)"

    prompt = (
        "You are creating Mandarin vocabulary flashcards for a learner.\n\n"
        "Create exactly one card for each of these NEW target words:\n"
        f"{targets}\n\n"
        "AUTHORITATIVE DICTIONARY DATA (CC-CEDICT) — treat this as the source of truth "
        "for pinyin and meaning. Pinyin is given with tone numbers (e.g. 'hang2'); render "
        "it on the card with tone marks (e.g. 'háng'). A word may list several readings "
        "and senses; pick the reading and sense that best fit a natural example sentence, "
        "and make the card's meaning reflect these real definitions rather than inventing "
        "one. If a word has no entry below, fall back to your own knowledge.\n"
        f"{references}\n\n"
        "The learner ALREADY KNOWS these words — prefer to reuse them in your example "
        "sentences so the examples stay comprehensible, and don't turn them into cards:\n"
        f"{known}\n\n"
        "For each new word produce these fields:\n"
        "- simplified: the word in simplified characters\n"
        "- traditional: the traditional form (repeat simplified if identical)\n"
        "- pinyin: the word's pinyin with tone marks\n"
        "- meaning: a concise English definition\n"
        "- part_of_speech: e.g. noun, verb, adjective, measure word\n"
        "- example: one short, natural sentence in simplified Chinese using the word, "
        "leaning on the known vocabulary where it fits\n"
        "- example_pinyin: the example sentence's pinyin with tone marks\n"
        "- example_meaning: an English translation of the example sentence\n"
        "Keep everything everyday and beginner-friendly."
    )
    resp = _client().models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": GeneratedCards},
    )
    if resp.parsed is None:
        raise RuntimeError(f"Gemini returned no parseable cards: {resp.text!r}")
    return resp.parsed
