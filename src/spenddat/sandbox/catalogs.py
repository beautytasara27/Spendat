"""Product catalogs for the bundled sandbox stores.

Fictional stores, realistic products. Variant products (shoes, clothing) use one entry
per size/colour sharing an ``item_group_id``, the way real product feeds do.
"""

from __future__ import annotations

from typing import Any

from spenddat.sandbox.reference_merchant import DEFAULT_CATALOG


def _variants(
    group: str,
    title: str,
    description: str,
    category: str,
    price_minor: int,
    colors: dict[str, str],
    sizes: list[str],
    sold_out: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for color, code in colors.items():
        for size in sizes:
            out.append(
                {
                    "id": f"{group}-{code}-{size.replace('.', '_')}",
                    "item_group_id": group,
                    "title": title,
                    "description": description,
                    "price_minor": price_minor,
                    "product_category": category,
                    "color": color,
                    "size": size,
                    "availability": "out_of_stock"
                    if (color, size) in (sold_out or set())
                    else "in_stock",
                }
            )
    return out


SHOE_SIZES = ["6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11", "12"]
TEE_SIZES = ["XS", "S", "M", "L", "XL", "XXL"]

SNEAKERS: list[dict[str, Any]] = [
    *_variants(
        "SAMBA-OG",
        "adidas Samba OG",
        "Classic leather indoor soccer-style sneakers shoes with gum sole",
        "footwear",
        10000,
        {"White": "WHT", "Black": "BLK", "Green": "GRN"},
        SHOE_SIZES,
        sold_out={("Green", "8.5"), ("Black", "12")},
    ),
    *_variants(
        "AF1-07",
        "Nike Air Force 1 '07",
        "Low-top leather basketball sneakers shoes",
        "footwear",
        11500,
        {"White": "WHT", "Black": "BLK"},
        SHOE_SIZES,
    ),
    *_variants(
        "NB-550",
        "New Balance 550",
        "Retro leather basketball sneakers shoes",
        "footwear",
        11000,
        {"White": "WHT", "Grey": "GRY"},
        SHOE_SIZES,
    ),
    *_variants(
        "CT-HI",
        "Converse Chuck Taylor All Star High Top",
        "Canvas high top sneakers shoes",
        "footwear",
        6500,
        {"White": "WHT", "Black": "BLK", "Red": "RED"},
        SHOE_SIZES,
    ),
    *_variants(
        "TEE-HW",
        "Heavyweight Cotton T-Shirt",
        "Boxy fit crew neck tee shirt",
        "clothing",
        2800,
        {"White": "WHT", "Black": "BLK", "Navy": "NVY"},
        TEE_SIZES,
    ),
    *_variants(
        "SOCK-3",
        "Crew Socks 3-Pack",
        "Cushioned cotton athletic socks",
        "clothing",
        1400,
        {"White": "WHT", "Black": "BLK"},
        ["S", "M", "L"],
    ),
]

CATALOGS: dict[str, list[dict[str, Any]]] = {
    "everyday": DEFAULT_CATALOG,
    "sneakers": SNEAKERS,
}
