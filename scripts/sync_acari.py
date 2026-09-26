#!/usr/bin/env python3
"""Build the local Acari Ciar catalogue from the manufacturer's public website."""

from __future__ import annotations

import json
import re
import ssl
import time
import unicodedata
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from lxml import html
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://acariciarecocorm.ru/"
CATEGORIES = (
    "korm-dlya-sobak/",
    "korm-dlya-koshek/",
    "korm-i-konservy-delux-dog/",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
)
SSL_CONTEXT = ssl.create_default_context()
# The manufacturer's server currently returns an incomplete certificate chain.
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

IMAGE_DIR = ROOT / "assets/images/products/acari"
DATA_DIR = ROOT / "assets/data"
PHOTO_SIZE = 800  # px, the longest side of catalogue photos


def fetch(url: str, *, binary: bool = False) -> bytes | str:
    parts = urlsplit(url)
    url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path), parts.query, parts.fragment))
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            if binary
            else "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=35, context=SSL_CONTEXT) as response:
                body = response.read()
                return body if binary else body.decode("utf-8", errors="replace")
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise RuntimeError(f"Не удалось загрузить {url}: HTTP {exc.code}") from exc
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" \n\r\t.")


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "product"


def text_after_label(doc, label: str) -> str:
    needles = doc.xpath(
        "//b[contains(translate(normalize-space(.), "
        "'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',"
        "'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'), $label)]",
        label=label.lower(),
    )
    if not needles:
        return ""
    parent = needles[0].getparent()
    value = clean(parent.text_content())
    return clean(re.sub(rf"^{re.escape(clean(needles[0].text_content()))}\s*", "", value, flags=re.I))


def block_after_label(doc, label: str) -> str:
    for bold in doc.xpath("//b"):
        if label.lower() not in clean(bold.text_content()).lower():
            continue
        block = bold
        while block.getparent() is not None and block.tag.lower() != "div":
            block = block.getparent()
        text = clean(block.text_content())
        return clean(re.sub(rf"^{re.escape(clean(bold.text_content()))}\s*", "", text, flags=re.I))
    return ""


def parse_sizes(value: str) -> list[dict]:
    result = []
    for number, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*(кг|гр?|г)\.?", value.lower()):
        number = number.replace(".", ",")
        unit = "г" if unit.startswith("г") else "кг"
        result.append({"w": f"{number} {unit}", "p": None})
    return result or [{"w": "фасовка уточняется", "p": None}]


def parse_analysis(value: str, energy: str) -> dict[str, str]:
    aliases = {
        "протеин": "Белок",
        "протеин (белок)": "Белок",
        "белок": "Белок",
        "жир": "Жир",
        "клетчатка": "Клетчатка",
        "зола": "Зола",
        "кальций": "Кальций",
        "фосфор": "Фосфор",
        "влажность": "Влага",
        "влага": "Влага",
        "натрий": "Натрий",
        "калий": "Калий",
        "омега3": "Омега-3",
        "омега 3": "Омега-3",
        "омега6": "Омега-6",
        "омега 6": "Омега-6",
        "омега 3/6/9": "Омега-3/6/9",
        "омега3/6/9": "Омега-3/6/9",
        "омега3\\6\\9": "Омега-3/6/9",
    }
    result: dict[str, str] = {}
    normalized = value.replace("\u00a0", " ")
    pattern = re.compile(
        r"([А-Яа-яЁёA-Za-z0-9 /\\()]+?)\s*[-–:]?\s*"
        r"(\d+(?:[.,]\d+)?\s*(?:%|г(?:/100\s*г)?|мг(?:/кг)?|ккал(?:/кг)?))",
        re.I,
    )
    for key, val in pattern.findall(normalized):
        raw_key = clean(key).lower().strip(";,.")
        raw_key = re.split(r"[;,]", raw_key)[-1].strip()
        key_name = aliases.get(raw_key, clean(key).title())
        if key_name and len(key_name) < 28:
            result[key_name] = clean(val).replace(" ", "")
    energy_match = re.search(r"(\d+(?:[.,]\d+)?)\s*ккал", energy, re.I)
    if energy_match:
        basis = "100 г" if "100 г" in energy.lower() else "кг"
        result[f"Ккал/{basis}"] = energy_match.group(1).replace(",", ".")
    return dict(list(result.items())[:10])


def infer_line(name: str) -> str:
    lower = name.lower().replace("`", "'").replace("’", "'")
    compact = re.sub(r"[\s.]+", "", lower)
    if "вета'дог" in compact:
        return "Vet A'Dog"
    if "вета'кэт" in compact:
        return "Vet A'Cat"
    if "мейн-кун" in lower or "мейн кун" in lower:
        return "Maine Coon"
    if "запеч" in lower and "кэт" in lower:
        return "A Baked Cat"
    if "а'кэт" in compact:
        return "A'Cat"
    rules = (
        ("vet. диета", "Вет. диета"),
        ("вет. диета", "Вет. диета"),
        ("vet a'dog", "Vet A'Dog"),
        ("вет а'дог", "Vet A'Dog"),
        ("vet a'cat", "Vet A'Cat"),
        ("вет а'кэт", "Vet A'Cat"),
        ("a baked dog", "A Baked Dog"),
        ("a'baked dog", "A Baked Dog"),
        ("a baked cat", "A Baked Cat"),
        ("a'baked cat", "A Baked Cat"),
        ("maine coon", "Maine Coon"),
        ("мейн кун", "Maine Coon"),
        ("a'cat", "A'Cat"),
        ("de lux dog", "De'Lux Dog"),
        ("de`lux dog", "De'Lux Dog"),
        ("power flock", "Power Flock"),
        ("волчья", "Power Flock"),
        ("flagman", "Flagman"),
        ("флагман", "Flagman"),
        ("vitality", "Vitality"),
        ("витал", "Vitality"),
        ("optima", "Optima"),
        ("оптим", "Optima"),
        ("superba", "Superba"),
        ("супер актив", "Superba"),
        ("aurora", "Aurora"),
        ("аврора", "Aurora"),
        ("regular", "Regular"),
        ("регуляр", "Regular"),
        ("baby dog", "Для щенков"),
        ("беби дог", "Для щенков"),
        ("бамбино", "Для щенков"),
        ("юниор", "Для щенков"),
    )
    for needle, line in rules:
        if needle in lower:
            return line
    return "Влажные корма" if any(x in lower for x in ("консерв", "мусс", "пауч")) else "Acari Ciar"


def infer_tags(description: str, name: str, pet: str) -> list[str]:
    text = f"{description} {name}".lower()
    candidates = (
        ("стерилиз", "стерилизованные"),
        ("кастрир", "кастрированные"),
        ("котят", "котята"),
        ("щен", "щенки"),
        ("всех пород", "все породы"),
        ("мелких пород", "мелкие породы"),
        ("крупных пород", "крупные породы"),
        ("мейн", "мейн-кун"),
        ("гипоаллерген", "гипоаллергенный"),
        ("беззернов", "беззерновой"),
        ("чувствитель", "чувствительное пищеварение"),
        ("гастро", "пищеварение"),
        ("уринар", "мочевыделительная система"),
        ("дерма", "кожа и шерсть"),
        ("гепат", "поддержка печени"),
        ("вет.", "ветеринарная диета"),
        ("запеч", "запечённый"),
        ("влажн", "влажный корм"),
    )
    tags = [tag for needle, tag in candidates if needle in text]
    if not tags:
        tags.append("для кошек" if pet == "cat" else "для собак")
    return list(dict.fromkeys(tags))[:5]


def local_photo(url: str, slug: str) -> str:
    """Pack shot for the catalogue: resized WebP, a few dozen KB instead of a megabyte."""
    if not url:
        return ""
    target = IMAGE_DIR / f"{slug}.webp"
    if not target.exists() or target.stat().st_size < 1000:
        image = Image.open(BytesIO(fetch(url, binary=True)))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.thumbnail((PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS)
        image.save(target, "WEBP", quality=80, method=6)
    return target.relative_to(ROOT).as_posix()


def local_image(url: str, slug: str, suffix: str = "") -> str:
    if not url:
        return ""
    extension = Path(urlparse(url).path).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    name = f"{slug}{suffix}{extension}"
    target = IMAGE_DIR / name
    if not target.exists() or target.stat().st_size < 1000:
        target.write_bytes(fetch(url, binary=True))
    return target.relative_to(ROOT).as_posix()


def category_urls() -> list[str]:
    urls: list[str] = []
    for category in CATEGORIES:
        category_url = urljoin(BASE, category)
        doc = html.fromstring(fetch(category_url), base_url=category_url)
        for href in doc.xpath("//a/@href"):
            # Links in category pages look root-relative but omit the leading slash.
            url = urljoin(BASE, href)
            parsed = urlparse(url)
            if parsed.netloc != urlparse(BASE).netloc or not parsed.path.endswith(".html"):
                continue
            if not any(part in parsed.path for part in ("/korm-dlya-sobak/", "/korm-dlya-koshek/", "/korm-i-konservy-delux-dog/")):
                continue
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_product(url: str) -> dict:
    doc = html.fromstring(fetch(url), base_url=url)
    h1 = clean(" ".join(doc.xpath("//div[contains(@class,'section-title')]//h1/text()")))
    if not h1:
        raise ValueError("На странице нет заголовка товара")

    name = re.sub(r"^Корм\s+", "", h1, flags=re.I)
    name = re.sub(r"\s+для\s+(кошек|собак)\s*$", "", name, flags=re.I)
    name = clean(name)
    lower = h1.lower()
    pet = "cat" if "кош" in lower else "dog"

    right = doc.xpath("//div[contains(@class,'col-md-9') and contains(@class,'col-lg-8')]")
    description = ""
    if right:
        paragraphs = [clean(p.text_content()) for p in right[0].xpath("./div/p|./p")]
        description = next(
            (p for p in paragraphs if p and not any(marker in p.lower() for marker in ("ингредиент", "питательност", "купить в"))),
            "",
        )
    if not description:
        description = clean(doc.xpath("string(//meta[@name='description']/@content)"))

    ingredients = block_after_label(doc, "Ингредиенты")
    nutrition = block_after_label(doc, "Питательность")
    energy = text_after_label(doc, "Измеренная обменная энергия")
    packaging = text_after_label(doc, "Фасовка")
    storage = text_after_label(doc, "Хранение")
    feed_type = text_after_label(doc, "Тип корма")
    meat_fish = text_after_label(doc, "Мясо/рыба")
    protein_fat = text_after_label(doc, "Протеин/жир")

    slug = Path(urlparse(url).path).stem
    product_id = f"acari-{slugify(slug)}"
    og_image = clean(doc.xpath("string(//meta[@property='og:image']/@content)"))
    photo = local_photo(urljoin(url, og_image), slugify(slug)) if og_image else ""

    norm_href = clean(doc.xpath("string(//h4[contains(.,'Норма кормления')]/following-sibling::div[1]//img/@src)"))
    norm_photo = ""
    if norm_href:
        try:
            norm_photo = local_image(urljoin(BASE, norm_href), slugify(slug), "-norm")
        except RuntimeError:
            pass

    cls = "холистик" if "холистик" in description.lower() or "holistic" in name.lower() else (
        "супер-премиум" if "супер-премиум" in description.lower() else (
            "премиум" if "премиум" in description.lower() else "полнорационный"
        )
    )
    grain = "беззерновой" if "беззернов" in description.lower() else (
        "низкозерновой" if "низкозернов" in description.lower() else ""
    )
    basis_parts = []
    if meat_fish:
        basis_parts.append(f"мясо/рыба {meat_fish}")
    if protein_fat:
        basis_parts.append(f"белок/жир {protein_fat}")
    sub = ", ".join(basis_parts) or clean(feed_type) or cls

    line = infer_line(name)
    if line == "Acari Ciar" and any(x in f"{feed_type} {description}".lower() for x in ("влажн", "консерв")):
        line = "Влажные корма"

    return {
        "id": product_id,
        "brand": "acari",
        "pet": pet,
        "line": line,
        "name": name,
        "sub": sub,
        "cls": cls,
        "grain": grain,
        "tags": infer_tags(description, name, pet),
        "hue": "#5CC8F2",
        "desc": description,
        "comp": ingredients or "Состав уточняется у производителя.",
        "an": parse_analysis(nutrition, energy),
        "norm": "Норма кормления зависит от веса и состояния питомца. Используйте таблицу производителя на упаковке.",
        "sizes": parse_sizes(packaging),
        "photo": photo,
        "normPhoto": norm_photo,
        "source": url,
        "storage": storage,
        "feedType": feed_type,
        "meatFish": meat_fish,
        "proteinFat": protein_fat,
    }


def main() -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    urls = category_urls()
    products: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(parse_product, url): url for url in urls}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            url = futures[future]
            try:
                product = future.result()
                products.append(product)
                print(f"[{completed:02d}/{len(urls):02d}] {product['name']}", flush=True)
            except Exception as exc:  # Continue and leave a reviewable report.
                errors.append({"url": url, "error": str(exc)})
                print(f"[{completed:02d}/{len(urls):02d}] ОШИБКА: {url}: {exc}", flush=True)

    products.sort(key=lambda p: (p["pet"], p["line"], p["name"]))
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "source": BASE,
        "generatedAt": generated_at,
        "productCount": len(products),
        "products": products,
    }
    (DATA_DIR / "acari-catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    js = (
        "/* Автоматически собрано с официального сайта ACARI CIAR. */\n"
        f"window.ACARI_CATALOG_META = {json.dumps({k: payload[k] for k in ('source', 'generatedAt', 'productCount')}, ensure_ascii=False, indent=2)};\n"
        f"window.ACARI_PRODUCTS = {json.dumps(products, ensure_ascii=False, indent=2)};\n"
    )
    (DATA_DIR / "acari-products.js").write_text(js, encoding="utf-8")
    report = {
        "generatedAt": generated_at,
        "discovered": len(urls),
        "imported": len(products),
        "errors": errors,
        "withPhoto": sum(bool(p["photo"]) for p in products),
        "withIngredients": sum(not p["comp"].startswith("Состав уточняется") for p in products),
        "withNutrition": sum(bool(p["an"]) for p in products),
        "withFeedingTable": sum(bool(p["normPhoto"]) for p in products),
        "cats": sum(p["pet"] == "cat" for p in products),
        "dogs": sum(p["pet"] == "dog" for p in products),
    }
    (DATA_DIR / "acari-sync-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
