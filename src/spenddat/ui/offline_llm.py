"""Deterministic keyword-based stand-in for the LLM, for SANDBOX demos without a model key.

It produces the same tool-call payload the real model would, so its output still goes
through ``IntentParser``'s full deterministic validation. It is deliberately simple
(configure an OpenRouter or Anthropic key for real language understanding) and refuses
to exist in live mode.
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
    "digital_goods": ("thank-you note", "thank you note", "supporter pass", "digital"),
    "footwear": (
        "shoe",
        "sneaker",
        "trainer",
        "boot",
        "sandal",
        "samba",
        "air force",
        "af1",
        "chuck taylor",
        "converse",
        "new balance",
        "loafer",
        "slipper",
    ),
    "clothing": (
        "t-shirt",
        "t shirt",
        "tshirt",
        "tee",
        "shirt",
        "hoodie",
        "sweater",
        "jacket",
        "jeans",
        "pants",
        "shorts",
        "sock",
        "dress",
        "skirt",
    ),
    "electronics_accessories": (
        "case",
        "cable",
        "charger",
        "headphone",
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
        "paper",
        "desk mat",
        "marker",
        "folder",
    ),
    "books": ("book", "novel", "paperback"),
    "household": ("towel", "sponge", "detergent", "trash bag", "light bulb"),
    "personal_care": ("shampoo", "toothpaste", "soap", "lotion"),
}
_COLORS = (
    "white",
    "black",
    "green",
    "red",
    "blue",
    "navy",
    "grey",
    "gray",
    "brown",
    "beige",
    "pink",
    "purple",
    "yellow",
    "orange",
    "cream",
    "tan",
)
_VAGUE = {"something", "anything", "stuff", "things", "nice", "cool", "whatever", "item", "it"}
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
}
_LIMIT = re.compile(
    r"(?:under|below|less than|max(?:imum)?|up to|no more than|at most|budget(?: of)?)?\s*"
    r"\$\s?(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s?(?:dollars|usd|bucks)\b",
    re.I,
)
_SIZE = re.compile(
    r"\b(?:in\s+)?(?:a\s+)?(?:us\s+|mens?\s+|womens?\s+)?size\s+(?:us\s+)?"
    r"(\d{1,2}(?:\.5|½)?|xxs|xs|s|m|l|xl|xxl|xxxl|small|medium|large|x-large)\b",
    re.I,
)
_SIZE_WORDS = {"small": "S", "medium": "M", "large": "L", "x-large": "XL"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


class OfflineIntentLlm:
    def __init__(
        self, mode: RuntimeMode, vendor_ids: list[str], vendor_names: dict[str, str] | None = None
    ) -> None:
        if mode is not RuntimeMode.SANDBOX:
            raise RuntimeError("the offline intent parser is only permitted in sandbox mode")
        self._vendors = vendor_ids
        self._names = vendor_names or {}

    def _find_vendor(self, low: str) -> tuple[str, str | None]:
        """Returns (vendor id or 'any_allowed_vendor', matched text to strip)."""
        for vid in self._vendors:
            for label in {vid, vid.replace("_", " "), self._names.get(vid, "")}:
                label = label.lower().strip()
                if label and re.search(rf"\b{re.escape(label)}\b", low):
                    return vid, label
        fm = re.search(
            r"\b(?:from|at|on)\s+([a-z0-9][a-z0-9 &'.-]{1,30}?)"
            r"(?:[,.!?]|\s+(?:under|if|for|max|in|size)\b|$)",
            low,
        )
        if fm:
            return _slug(fm.group(1)), fm.group(1)
        return "any_allowed_vendor", None

    def extract(self, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        m = re.search(r"<request>\n(.*)\n</request>", user, re.S)
        text = (m.group(1) if m else user).strip()
        low = text.lower()

        vendor, vendor_text = self._find_vendor(low)

        category = None
        for cat, words in _CATEGORY_WORDS.items():
            if any(re.search(rf"\b{re.escape(w)}(?:s|es)?\b", low) for w in words):
                category = cat
                break

        amounts = [(a or b) for a, b in _LIMIT.findall(text)]
        max_price = float(amounts[0]) if amounts else None

        size = None
        sm = _SIZE.search(low)
        if sm:
            size = sm.group(1).replace("½", ".5")
            size = _SIZE_WORDS.get(size, size.upper() if size.isalpha() else size)
        elif re.search(r"[.,]\s*(\d{1,2}(?:\.5)?)\s*$", low):  # follow-up answer: "... . 8.5"
            size = re.search(r"[.,]\s*(\d{1,2}(?:\.5)?)\s*$", low).group(1)
        color = next((c for c in _COLORS if re.search(rf"\b{c}\b", low)), None)

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
        if vendor_text:
            desc = re.sub(rf"\b(?:from|at|on)\s+{re.escape(vendor_text)}\b", " ", desc)
            desc = desc.replace(vendor_text, " ")
        desc = _LIMIT.sub(" ", desc)
        desc = _SIZE.sub(" ", desc)
        desc = re.sub(r"[.,]\s*\d{1,2}(?:\.5)?\s*$", " ", desc)
        if color:
            desc = re.sub(rf"\b(?:in\s+)?{color}\b", " ", desc)
        desc = re.sub(r"\bif (?:they are|it is|it's|they're)\b", " ", desc)
        desc = re.sub(
            r"\b(please|can you|could you|i want to|i need|i'd like to|get me|order|buy|"
            r"purchase|pick up|me|some|a|an|the|pair of|pairs of)\b",
            " ",
            desc,
        )
        if qm:
            desc = re.sub(rf"\b{qm.group(1)}\b", " ", desc, count=1)
        desc = re.sub(
            r"\b(under|below|less than|max(?:imum)?|up to|no more than|at most|budget(?: of)?)"
            r"\s*$",
            " ",
            desc.strip(),
        )
        desc = re.sub(r"[^a-z0-9 &'-]+", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip(" -&")

        words = set(desc.split())
        if len(desc) < 2 or words <= _VAGUE:
            return {
                "status": "clarify",
                "clarifying_question": "What exactly would you like me to buy? A specific item "
                "(and optionally a store, size, colour and price limit) helps.",
                "missing_fields": ["product_description"],
            }
        if category is None:
            return {
                "status": "unsupported",
                "unsupported_reason": f"I don't have a category in your policy that covers "
                f'"{desc}". (The offline demo parser only knows a fixed word list; add an '
                "OpenRouter key for real language understanding.)",
            }
        return {
            "status": "ok",
            "vendor": vendor,
            "product_description": desc,
            "category": category,
            "quantity": qty,
            "max_price_usd": max_price,
            "size": size,
            "color": color,
            "constraints": [],
        }
