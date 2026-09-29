"""Deterministic keyword-based stand-in for the LLM, for SANDBOX demos without an API key.

It produces the same tool-call payload the real model would, so its output still goes
through ``IntentParser``'s full deterministic validation. It is deliberately simple and
refuses to exist in live mode.
"""

from __future__ import annotations

import re
from typing import Any

from spenddat.models import RuntimeMode

_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "gift_cards": ("gift card", "giftcard"),
    "subscriptions": ("subscription", "membership", "monthly plan"),
    "alcohol": ("wine", "beer", "vodka", "whiskey"),
    "tobacco": ("cigarette", "cigar", "vape"),
    "weapons": ("knife", "gun", "ammo"),
    "electronics_accessories": (
        "case",
        "cable",
        "charger",
        "headphone",
        "headphones",
        "earbuds",
        "mouse",
        "keyboard",
        "adapter",
        "usb",
        "hdmi",
        "screen protector",
        "monitor",
    ),
    "office_supplies": (
        "lamp",
        "stapler",
        "notebook",
        "pen",
        "pens",
        "paper",
        "desk mat",
        "marker",
        "folder",
    ),
    "books": ("book", "novel", "paperback"),
    "household": ("towel", "sponge", "detergent", "trash bag", "light bulb"),
    "personal_care": ("shampoo", "toothpaste", "soap", "lotion"),
}
_NUM_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "a": 1,
    "an": 1,
}
_LIMIT = re.compile(
    r"(?:under|below|less than|max(?:imum)?|up to|no more than|at most|budget(?: of)?)?\s*"
    r"\$\s?(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s?(?:dollars|usd|bucks)\b",
    re.I,
)


class OfflineIntentLlm:
    def __init__(self, mode: RuntimeMode, vendor_ids: list[str]) -> None:
        if mode is not RuntimeMode.SANDBOX:
            raise RuntimeError("the offline intent parser is only permitted in sandbox mode")
        self._vendors = vendor_ids

    def extract(self, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        m = re.search(r"<request>\n(.*)\n</request>", user, re.S)
        text = (m.group(1) if m else user).strip()
        low = text.lower()

        vendor = "any_allowed_vendor"
        for vid in self._vendors:
            if vid in low or vid.replace("_", " ") in low:
                vendor = vid
                break
        else:
            fm = re.search(
                r"\bfrom\s+([a-z0-9][a-z0-9 &'.-]{1,30}?)(?:[,.!?]|\s+under\b|"
                r"\s+for\b|\s+max\b|$)",
                low,
            )
            if fm:
                vendor = re.sub(r"[^a-z0-9]+", "_", fm.group(1).strip()).strip("_")

        category = None
        for cat, words in _CATEGORY_WORDS.items():
            if any(re.search(rf"\b{re.escape(w)}(?:s|es)?\b", low) for w in words):
                category = cat
                break

        amounts = [(a or b) for a, b in _LIMIT.findall(text)]
        max_price = float(amounts[0]) if amounts else None

        qty = 1
        qm = re.search(
            r"(?:^|\b(?:buy|order|get|purchase|need|want|grab)\s+(?:me\s+)?)"
            r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?!dollars|usd|bucks)[a-z]",
            low,
        )
        if qm:
            qty = int(qm.group(1)) if qm.group(1).isdigit() else _NUM_WORDS[qm.group(1)]

        desc = low
        desc = re.sub(r"\bfrom\s+[a-z0-9 &'.-]+?(?=[,.!?]|\s+under\b|\s+for\b|$)", " ", desc)
        desc = _LIMIT.sub(" ", desc)
        desc = re.sub(
            r"\b(please|can you|could you|i want to|i need|i'd like to|"
            r"get me|order|buy|purchase|pick up|me|some)\b",
            " ",
            desc,
        )
        if qm:
            desc = re.sub(rf"\b{qm.group(1)}\b", " ", desc, count=1)
        desc = re.sub(
            r"\b(under|below|less than|max(?:imum)?|up to|no more than|at most|"
            r"budget(?: of)?)\s*$",
            " ",
            desc.strip(),
        )
        desc = re.sub(r"[^a-z0-9 -]+", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip(" -")

        if category is None or len(desc) < 2:
            return {
                "status": "clarify",
                "clarifying_question": "What exactly would you like me to buy? A specific item "
                "(and optionally a store and price limit) helps.",
                "missing_fields": ["product_description"],
            }
        return {
            "status": "ok",
            "vendor": vendor,
            "product_description": desc,
            "category": category,
            "quantity": qty,
            "max_price_usd": max_price,
            "constraints": [],
        }
