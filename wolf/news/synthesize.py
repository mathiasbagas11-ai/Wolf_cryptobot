"""AI synthesis of fresh headlines into one crypto-news brief.

Given the genuinely-new items a cycle surfaced, an LLM (DeepSeek by default)
condenses them into a short, punchy Telegram brief — grouping related stories
and flagging why they matter — instead of a flat list. Falls back to ``None``
when no narrator is configured, so the caller posts the plain card instead.

The model only ever phrases the supplied headlines; it must not invent stories.
Output is HTML-escaped by the caller.
"""

from __future__ import annotations

import logging
from typing import Optional

from wolf.ai.base import LLMClient, NullLLMClient
from wolf.news.base import NewsItem

log = logging.getLogger("wolf.news")

_SYSTEM = (
    "Lu kurator berita crypto. Dari DAFTAR HEADLINE di bawah, susun brief Telegram "
    "berbahasa Indonesia yang padat & tajam:\n"
    "- Kelompokkan headline yang setema; sebutkan kenapa penting buat trader.\n"
    "- Maksimal 5 bullet, tiap bullet 1-2 kalimat, pakai emoji (📰🔥⚠️📈).\n"
    "- WAJIB cuma pakai info dari DAFTAR. JANGAN ngarang berita/angka.\n"
    "- Jangan ulang headline mentah — sintesis & beri konteks.\n"
    "- Output teks polos: TANPA tag HTML/markdown."
)


class NewsSynthesizer:
    def __init__(self, narrator: Optional[LLMClient] = None, max_tokens: int = 700) -> None:
        self._narrator = narrator or NullLLMClient()
        self._max_tokens = max_tokens

    @property
    def available(self) -> bool:
        return self._narrator.available

    def build(self, items: list[NewsItem]) -> Optional[str]:
        if not items or not self._narrator.available:
            return None
        lines = [
            f"- {it.title} [{it.source}"
            + (f", {it.score} pts" if it.score else "") + f"] {it.url}"
            for it in items
        ]
        try:
            text = self._narrator.complete(_SYSTEM, "DAFTAR HEADLINE:\n" + "\n".join(lines),
                                           max_tokens=self._max_tokens)
        except Exception:  # synthesis must never break the news job
            log.exception("News synthesis failed — falling back to plain card")
            return None
        return (text or "").strip() or None
