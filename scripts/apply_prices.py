#!/usr/bin/env python3
"""Apply Acari Ciar / De Lux Dog wholesale price lists to the storefront.

Reads the private price lists from ``opt_price/`` (git-ignored) and writes
``assets/data/acari-prices.js`` with *retail* prices only:

* sizes and prices for catalogue products that exist in the price list,
  including the granule choice where the price list has one;
* extra products that are in the price list but not on the manufacturer's site.

Retail price = wholesale × (1 + markup), rounded up. Markup depends on the pack
weight and is read from the private ``opt_price/markup.json``.

Usage:  python3 -m pip install xlrd openpyxl && python3 scripts/apply_prices.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import xlrd

ROOT = Path(__file__).resolve().parents[1]
PRICE_DIR = ROOT / "opt_price"
CATALOG = ROOT / "assets/data/acari-catalog.json"
OUTPUT = ROOT / "assets/data/acari-prices.js"
REPORT = PRICE_DIR / "apply-report.txt"  # stays private: it lists wholesale prices

# Only the current sheets: the others are order forms or price lists from 2020–2021.
PRICE_SHEETS = {
    "2026_04_01 Acari Cat опт.xls": "от 1000",
    "2026_04_01 Acari Dog опт.xls": "от 1000",
    "2026_04_01 De Lux Dog опт.xls": "от 1000",
    "КОНС ОПТ.xlsx": "от 1000",
}

# Markup by pack weight lives next to the price lists: it is private, retail minus markup gives the wholesale price.
# opt_price/markup.json: {"tiers": [[max kg or null, markup], ...], "roundTo": 10}
MARKUP_FILE = PRICE_DIR / "markup.json"


def load_markup() -> tuple[tuple[tuple[float, float], ...], int]:
    if not MARKUP_FILE.exists():
        # Example values only: the real markup must not appear in the repository.
        sys.exit(f"Нет {MARKUP_FILE.relative_to(ROOT)} — формат: "
                 '{"tiers": [[до кг, наценка], ..., [null, наценка]], "roundTo": 10}, '
                 'например {"tiers": [[3, 0.5], [null, 0.2]], "roundTo": 10}')
    config = json.loads(MARKUP_FILE.read_text(encoding="utf-8"))
    tiers = tuple((math.inf if limit is None else float(limit), float(m)) for limit, m in config["tiers"])
    return tiers, int(config.get("roundTo", 10))


MARKUP_TIERS, ROUND_TO = load_markup()

# Granule codes stored in size entries; the page turns them into labels.
GRANULE_ORDER = ("S", "M", "L", "XS")  # the first one present is selected by default


def retail(wholesale: float, kg: float) -> int:
    markup = next(m for limit, m in MARKUP_TIERS if kg <= limit)
    return int(math.ceil(wholesale * (1 + markup) / ROUND_TO) * ROUND_TO)


# ——— reading the price lists ———

def sheet_rows(path: Path, sheet: str) -> list[list]:
    if path.suffix == ".xlsx":
        return [list(r) for r in openpyxl.load_workbook(path, data_only=True)[sheet].iter_rows(values_only=True)]
    s = xlrd.open_workbook(path).sheet_by_name(sheet)
    return [[c.value for c in s.row(r)] for r in range(s.nrows)]


def text(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_price_lists() -> list[dict]:
    """Rows of {name, variant, kg, price}; a product's first row carries its name."""
    items = []
    for filename, sheet in PRICE_SHEETS.items():
        rows = sheet_rows(PRICE_DIR / filename, sheet)
        cols = None
        name = variant = ""
        for row in rows:
            cells = [text(v) for v in row]
            low = [c.lower() for c in cells]
            if "номенклатура" in low:
                price_cols = [i for i, c in enumerate(low) if c.startswith("цена") and "стар" not in c]
                weight_cols = [i for i, c in enumerate(low) if "вес" in c and "общ" not in c]
                granule_cols = [i for i, c in enumerate(low) if "гранул" in c]
                cols = {
                    "name": low.index("номенклатура"),
                    "price": price_cols[0] if price_cols else None,
                    "weight": weight_cols[0] if weight_cols else None,
                    "granule": granule_cols[0] if granule_cols else None,
                }
                continue
            if not cols or cols["price"] is None or cols["weight"] is None:
                continue
            get = lambda key: row[cols[key]] if cols[key] is not None and cols[key] < len(row) else None
            n, w, p, g = text(get("name")), get("weight"), get("price"), text(get("granule"))
            if n and not is_number(w):
                continue  # section header
            if n:
                name, variant = n, g
            elif g:
                variant = g
            if name and is_number(w) and is_number(p) and p > 0:
                items.append({"name": name, "variant": variant, "kg": round(float(w), 3), "price": float(p)})
    return items


# ——— matching names from the price list and the catalogue ———

def _has(t: str, pattern: str) -> bool:
    return re.search(pattern, t) is not None


def _protein(t: str) -> str:
    for code, pattern in (
        ("rabbit", r"кролик|раббит|rabbit"),
        ("duck", r"утк|\bдак\b|duck"),
        ("lamb", r"ягн|ламб|lamb"),
        ("turkey", r"индейк|туркей|turkey"),
        ("fish", r"рыб|фиш|fish|угр|калкан"),
        ("beef", r"телят|биф|beef|говяд"),
    ):
        if _has(t, pattern):
            return code
    return "?"


def product_key(name: str) -> str | None:
    """Same key for the same product however the name is spelled."""
    t = name.lower().replace("ё", "е").replace("’", "'").replace("`", "'")
    cat = (_has(t, r"кэт|\bcat\b|\bкет\b|кошек|котов") and not _has(t, r"собак|щенк")) or _has(t, r"для кошек")
    if _has(t, r"полувлаж"):
        p = "venison" if _has(t, "оленин") else "quail" if _has(t, "перепел") else "galeeny" if _has(t, "цесарк") else _protein(t)
        return "semi-" + p
    if _has(t, r"консерв|в желе|паштет|тушен|фарш|рубец|желудоч|сердеч|мусс|влажн|ломтик|овощ") and not _has(t, r"сухой"):
        form = next((k for k, p in (("tripe", "рубец"), ("gizzards", "желудоч"), ("hearts", "сердеч"), ("slices", "ломтик"),
                                    ("stew", "тушен"), ("pate", "паштет"), ("mince", "фарш"), ("veg", "овощ"), ("mousse", "мусс"),
                                    ("jelly", "в желе"))
                     if _has(t, p)), "?")
        p = ("chicken" if _has(t, "куриц") else "tuna" if _has(t, "тунец") else "pollock" if _has(t, "минтай")
             else "sardine" if _has(t, "сардин") else _protein(t))
        if form in ("tripe", "gizzards", "hearts"):
            p = ""
        grade = "hg" if _has(t, r"хьюман|human") else "sp" if _has(t, "супер") else ""
        return f"can-{'cat' if cat else 'dog'}-{form}-{p}-{grade}"
    if _has(t, "коллаген"):
        return "collagen"
    if _has(t, "спирулин"):
        return "spirulina"
    if _has(t, r"лосос.*масл"):
        return "salmon-oil"
    if cat:
        mc = _has(t, r"мейн|maine")
        if _has(t, r"запеч|бакед|baked"):
            return ("baked-cat-mc-" if mc else "baked-cat-") + _protein(t)
        if _has(t, r"гастро|gastro"):
            return "vetcat-gastro-" + _protein(t)
        if _has(t, r"дерма|derma"):
            return "vetcat-derma"
        if _has(t, r"пхн|vegan|трюфел|truff|гепатик"):
            return "vetcat-phn"
        if _has(t, r"мкб|уринари|urinary"):
            return "vetcat-urinary"
        if _has(t, r"стерил|стирил|steril"):
            return ("vetcat-mc-steril-" if mc else "vetcat-steril-") + _protein(t)
        if _has(t, r"стартер|starter"):
            return "cat-mc-starter" if mc else "cat-starter"
        return ("cat-mc-" if mc else "cat-") + _protein(t)
    rules = (
        (r"золот.*старт", "gold-starter"), (r"золот.*бамбин", "gold-bambino"),
    )
    for pattern, key in rules:
        if _has(t, pattern):
            return key
    if _has(t, r"запеч|бакед|baked"):
        if _has(t, r"юниор|junior|олен"):
            return "baked-dog-junior"
        if _has(t, r"стартер|starter|цесарк"):
            return "baked-dog-starter"
        return "baked-dog-" + _protein(t)
    if _has(t, r"гастро|gastro"):
        return "dog-gastro-quail" if _has(t, r"перепел|куэйл|quail") else "dog-gastro-lamb"
    rules = (
        (r"дерма|derma", "dog-derma"),
        (r"пхн|vegan|трюфел|hepatic", "dog-phn"),
        (r"мкб|уринари|urinary", "dog-urinary"),
    )
    for pattern, key in rules:
        if _has(t, pattern):
            return key
    if _has(t, r"стерил|стирил|steril"):
        return "dog-steril-" + ("turkey" if _protein(t) == "turkey" else "beef")
    rules = (
        (r"белоснеж", "dog-lamb-snow"), (r"белый ш", "dog-bombyx-snow"), (r"шелкопряд|бомбикс|bomb.x", "dog-bombyx"),
        (r"регуляр|regular", "regular"), (r"аврора (диета|лайт)|aurora lite", "aurora-diet"), (r"аврора|aurora", "aurora"),
        (r"супер ?актив|суперба|superba", "superba"), (r"фуа|фегато|fegato", "flagman-fegato"), (r"флагман|flagman", "flagman"),
        (r"тести|tasty", "testi"), (r"оптим|optima", "optima"),
    )
    for pattern, key in rules:
        if _has(t, pattern):
            return key
    if _has(t, r"витал|vital"):
        return "vital-" + ("turkey-rabbit" if _has(t, r"индейк|туркей|кролик") else "beef-lamb")
    if _has(t, r"волчь|power ?flock"):
        return "powerflock-" + ("duck" if _protein(t) == "duck" else "beef-lamb")
    for pattern, key in ((r"беби дог|baby ?dog", "baby-starter"), (r"бамбино|паппи|puppy", "bambino"), (r"юниор|junior", "junior")):
        if _has(t, pattern):
            return key
    if _has(t, r"гипоаллерг") and _protein(t) in ("fish", "lamb"):
        return "dog-hypo-" + _protein(t)
    return None


def granule(variant: str) -> str:
    v = variant.lower()
    for code, needle in (("XS", "декорат"), ("S", "мелк"), ("M", "средн"), ("L", "крупн")):
        if needle in v and "пород" not in v:  # «для крупных пород» у кошек — это не гранула
            return code
    return ""


def size_label(kg: float, variant: str) -> str:
    pieces = re.fullmatch(r"(\d+)\s*шт", variant.strip())
    if pieces:
        return f"{pieces.group(1)} шт"
    if kg < 1:
        return f"{round(kg * 1000):g} г"
    return f"{kg:g} кг".replace(".", ",")


# ——— building the output ———

def build_sizes(rows: list[dict]) -> list[dict]:
    by_size: dict[tuple, dict] = {}
    for r in rows:
        g = granule(r["variant"])
        by_size[(g, r["kg"])] = {"w": size_label(r["kg"], r["variant"]), "p": retail(r["price"], r["kg"]), **({"g": g} if g else {})}
    granules = sorted({g for g, _ in by_size}, key=lambda g: GRANULE_ORDER.index(g) if g else -1)
    return [by_size[k] for g in granules for k in sorted(by_size) if k[0] == g]


EXTRA_META = {
    # key prefix → (line, pet, feed type)
    "baked-cat": ("A Baked Cat", "cat", "сухой"),
    "vetcat": ("Vet A'Cat", "cat", "сухой"),
    "gold": ("De'Lux Dog", "dog", "сухой"),
    "baked-dog": ("De'Lux Dog", "dog", "сухой"),
    "dog-phn": ("Vet A'Dog", "dog", "сухой"),
    "can-cat": ("Влажные корма", "cat", "консервы"),
    "can-dog": ("Влажные корма", "dog", "консервы"),
    "semi": ("Влажные корма", "dog", "полувлажный"),
    "cat-mc": ("Maine Coon", "cat", "сухой"),
}
SUPPLEMENTS = {"collagen", "spirulina", "salmon-oil"}


def short_name(name: str) -> str:
    name = re.split(r"\.\s+(Сбалансированн|Беззернов|Запеченный сбалансир)", name)[0]
    name = re.sub(r"\s+\d+\s*(гр|г|шт)\.?$", "", name.strip())
    name = re.sub(r"\s*,?\s*для (кошек|собак)\s*$", "", name)
    name = re.sub(r"^Де'?\s*\.?\s*Люкс Дог", "De Lux Dog", name)
    name = re.sub(r"\bКЕТ\b", "Кэт", name)
    name = re.sub(r"СТАРТЕР\s*собак", "стартер для щенков", name)
    name = re.sub(r",?\s*(фарш) кошек", r" \1", name)
    name = re.sub(r"\s+,", ",", name)
    if name.isupper():
        name = name.capitalize()
    return re.sub(r"\s+", " ", name).strip(" ,.")


def extra_product(key: str, rows: list[dict]) -> dict:
    full = max((r["name"] for r in rows), key=len)
    if key in SUPPLEMENTS:
        line, pet, feed = "Добавки", "both", "добавка"
    else:
        line, pet, feed = next(v for prefix, v in EXTRA_META.items() if key.startswith(prefix))
    tags = [t for t, needle in (("мейн-кун", "мейн"), ("хьюман грейд", "хьюман"), ("для котят", "стартер"),
                                ("для щенков", "бамбино"), ("кожа и шерсть", "дерма"), ("запечённый", "запеч"))
            if needle in full.lower()]
    return {
        "id": f"price-{key}",
        "brand": "acari",
        "pet": pet,
        "line": line,
        "name": short_name(full),
        "sub": feed,
        "cls": "холистик" if "холистик" in full.lower() else "",
        "grain": "беззерновой" if "беззернов" in full.lower() else "",
        "tags": tags or [feed],
        "hue": "#5CC8F2",
        "desc": full,
        "comp": "Состав уточняйте у продавца.",
        "an": {},
        "norm": "Норма кормления указана на упаковке.",
        "sizes": build_sizes(rows),
        "photo": "",
        "normPhoto": "",
        "source": "",
        "storage": "",
        "feedType": feed,
    }


def main() -> None:
    items = read_price_lists()
    by_key: dict[str, list[dict]] = defaultdict(list)
    unknown = []
    for item in items:
        key = product_key(item["name"])
        (by_key[key].append(item) if key and "?" not in key else unknown.append(item["name"]))

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))["products"]
    priced: dict[str, dict] = {}
    report = []
    used = set()
    for product in catalog:
        key = None if product["line"] == "Вет. диета" else product_key(product["name"])
        rows = by_key.get(key or "")
        if not rows:
            report.append(f"нет в прайсе: {product['name']} ({', '.join(s['w'] for s in product['sizes'])})")
            continue
        used.add(key)
        sizes = build_sizes(rows)
        priced[product["id"]] = {"sizes": sizes}
        before, after = [s["w"] for s in product["sizes"]], list(dict.fromkeys(s["w"] for s in sizes))
        if before != after:
            report.append(f"фасовки: {product['name']}: {', '.join(before)} → {', '.join(after)}")
        report.append(f"цены: {product['name']}: " + "; ".join(
            f"{s.get('g', '') + ' ' if s.get('g') else ''}{s['w']} = {s['p']}" for s in sizes))

    extra = [extra_product(key, rows) for key, rows in by_key.items() if key not in used]
    for product in extra:
        report.append(f"новый товар: {product['name']} ({', '.join(dict.fromkeys(s['w'] for s in product['sizes']))})")
    for name in dict.fromkeys(unknown):
        report.append(f"не распознано в прайсе: {name}")

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # The file is public: no markup or wholesale data, otherwise the wholesale price is easy to derive.
    meta = {"generatedAt": generated, "priced": len(priced), "extra": len(extra)}
    OUTPUT.write_text(
        "/* Розничные цены по оптовым прайсам ACARI CIAR — собирается scripts/apply_prices.py. */\n"
        f"window.ACARI_PRICES = {json.dumps({'meta': meta, 'products': priced, 'extra': extra}, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"цены проставлены: {len(priced)}, новых товаров: {len(extra)}, "
          f"без цены: {sum(r.startswith('нет в прайсе') for r in report)}, не распознано: {len(set(unknown))}")
    print(f"отчёт: {REPORT.relative_to(ROOT)}")
    if unknown:
        sys.exit(1)


if __name__ == "__main__":
    main()
