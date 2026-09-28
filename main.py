#!/usr/bin/env python3
"""
718 Made - Weidian shop monitor.

Haalt periodiek de productlijst van de Weidian-shop op, vergelijkt die met
de vorige momentopname, en post alleen de verschillen naar Discord:

  - nieuw product
  - uitverkocht
  - weer op voorraad
  - prijswijziging
  - product verwijderd

Chinese productnamen worden via de Claude API naar het Engels vertaald.
Vertalingen worden bewaard in state.json, zodat elke naam maar een keer
vertaald hoeft te worden.
"""

import os
import re
import json
import time
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")

SHOP_ID = os.environ.get("WEIDIAN_SHOP_ID", "1679474173")

# Kleuren van de Discord-embeds per soort wijziging
COLORS = {
    "new": 0x2ECC71,        # groen
    "restock": 0x3498DB,    # blauw
    "sold_out": 0xE74C3C,   # rood
    "price": 0xF39C12,      # oranje
    "removed": 0x95A5A6,    # grijs
}

LABELS = {
    "new": "New product",
    "restock": "Back in stock",
    "sold_out": "Out of stock",
    "price": "Price changed",
    "removed": "Product removed",
}

# Discord staat maximaal 10 embeds per bericht toe
MAX_EMBEDS_PER_MESSAGE = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 "
        "Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"https://shop{SHOP_ID}.v.weidian.com/",
}

session = requests.Session()
session.headers.update(HEADERS)

_cookie = os.environ.get("WEIDIAN_COOKIE", "").strip()
if _cookie:
    session.headers["Cookie"] = _cookie


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("products", {})
                data.setdefault("translations", {})
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"products": {}, "translations": {}, "initialised": False}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Producten ophalen
# ---------------------------------------------------------------------------

# Het endpoint dat de shoppagina zelf gebruikt. tabId 0 is het tabblad
# "alles". De Referer-header is verplicht: zonder die header antwoordt
# Weidian met "Referer Not Allowed".
DEFAULT_API = (
    "https://thor.weidian.com/decorate/shopDetail.tab.getItemList/1.0"
    "?param={param}"
)
PAGE_SIZE = 20


def build_url(offset: int) -> str:
    configured = os.environ.get("WEIDIAN_API_URL", "").strip()
    if configured:
        return configured.format(shop_id=SHOP_ID, offset=offset, limit=PAGE_SIZE)

    param = json.dumps({
        "shopId": str(SHOP_ID),
        "tabId": 0,
        "sortOrder": "desc",
        "offset": offset,
        "limit": PAGE_SIZE,
        "from": "h5",
        "showItemTag": True,
    }, separators=(",", ":"))
    return DEFAULT_API.format(param=requests.utils.quote(param, safe=""))


def norm_key(key: str) -> str:
    """itemId, item_id en ITEM-ID worden allemaal 'itemid'."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def looks_like_product(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = {norm_key(k) for k in obj.keys()}
    has_id = bool(keys & {"itemid", "id", "itemidstr", "productid", "skuid"})
    has_name = bool(keys & {"itemname", "title", "name", "itemtitle",
                            "productname", "subject"})
    has_price = bool(keys & {"price", "itemprice", "saleprice", "minprice",
                             "sellprice", "currentprice", "originalprice"})
    return has_id and has_name and has_price


def first_value(obj, names):
    lowered = {norm_key(k): v for k, v in obj.items()}
    for n in names:
        if lowered.get(n) not in (None, "", []):
            return lowered[n]
    return None


def normalise_product(raw: dict):
    pid = first_value(raw, ["itemid", "itemidstr", "productid", "id", "skuid"])
    name = first_value(raw, ["itemname", "itemtitle", "productname", "title",
                             "name", "subject"])
    price = first_value(raw, ["price", "itemprice", "saleprice", "sellprice",
                              "currentprice", "minprice"])
    stock = first_value(raw, ["stock", "itemstock", "totalstock", "quantity",
                              "inventory", "stocknum", "remainstock"])
    sold = first_value(raw, ["sold", "soldcount", "salecount"])
    image = first_value(raw, ["itemimg", "imgurl", "image", "imghead", "pic",
                              "picurl", "imgs", "images", "imageurl",
                              "primaryimageurl", "thumbnail"])
    url = first_value(raw, ["itemurl", "url", "link"])

    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url") or image.get("imgUrl")
    if isinstance(image, str) and image.startswith("//"):
        image = "https:" + image

    def as_number(value, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    price = as_number(price)
    if price is not None:
        price = round(price, 2)
    stock = as_number(stock, int)
    sold = as_number(sold, int)

    if pid is None or name is None:
        return None

    return {
        "id": str(pid),
        "name": str(name).strip(),
        "price": price,
        "stock": stock,
        "sold": sold,
        "image": image if isinstance(image, str) else None,
        "url": url if isinstance(url, str) and url.startswith("http")
               else f"https://weidian.com/item.html?itemID={pid}",
    }


def harvest_products(node, found):
    """Loopt het JSON-antwoord door en verzamelt alles wat op een product lijkt."""
    if isinstance(node, dict):
        if looks_like_product(node):
            item = normalise_product(node)
            if item:
                found[item["id"]] = item
            return
        for value in node.values():
            harvest_products(value, found)
    elif isinstance(node, list):
        for value in node:
            harvest_products(value, found)


def fetch_products():
    """Haalt alle producten op via offset-paginering. Geeft {id: product}."""
    products = {}
    offset = 0

    for _ in range(50):  # harde bovengrens: 1000 producten
        url = build_url(offset)
        try:
            r = session.get(url, timeout=25)
        except requests.RequestException as e:
            print(f"  netwerkfout bij offset {offset}: {e}")
            break

        try:
            payload = r.json()
        except ValueError:
            snippet = r.text[:200].replace("\n", " ")
            print(f"  geen JSON (HTTP {r.status_code}): {snippet!r}")
            break

        status = payload.get("status") or {}
        if status.get("code") not in (0, None):
            print(f"  Weidian gaf code {status.get('code')}: "
                  f"{status.get('message')!r}")
            if status.get("code") == 15:
                print("  -> de Referer-header wordt niet geaccepteerd; "
                      "controleer WEIDIAN_SHOP_ID.")
            break

        before = len(products)
        harvest_products(payload, products)
        gained = len(products) - before

        result = payload.get("result") or {}
        item_list = result.get("itemList")
        batch_size = len(item_list) if isinstance(item_list, list) else gained

        print(f"  offset {offset}: {batch_size} item(s), "
              f"{len(products)} uniek totaal")

        if batch_size == 0 or result.get("hasData") is False:
            break
        if batch_size < PAGE_SIZE and gained == 0:
            break

        offset += PAGE_SIZE
        time.sleep(1)

    return products


# ---------------------------------------------------------------------------
# Vertalen
# ---------------------------------------------------------------------------

def load_catalogue():
    """Leest products.json: bekende producten met herkenningsaanwijzingen."""
    if not os.path.exists(PRODUCTS_FILE):
        return []
    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    return [p for p in raw if isinstance(p, dict) and p.get("name")]


def build_name_prompt(names, catalogue):
    workshop = os.environ.get("WORKSHOP_NAME", "718 Made")

    if catalogue:
        lines = []
        for p in catalogue:
            if p.get("hints"):
                lines.append(f"- {p['name']}  |  recognise by: {p['hints']}")
            else:
                lines.append(f"- {p['name']}")
        known = "\n".join(lines)
    else:
        known = "(none yet)"

    numbered = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))

    return f"""Below are raw product names from the Chinese Weidian storefront of {workshop}, a workshop that reproduces garments and footwear from well-known fashion brands.

These raw names are messy: they mix Chinese and English, use brand abbreviations, and contain seller noise. Turn each one into a clean, readable English product name for a Discord shop-update channel.

For each name:
1. WORK OUT THE BRAND. The full brand name is often not written out. Expand abbreviations and Chinese brand names, for example PRD or 普拉达 is Prada, GC is Gucci, KS or 克罗心 is Chrome Hearts, MM6 and 马吉拉 are Maison Margiela, CG is Canada Goose, BLCG or 巴黎世家 is Balenciaga. Use the catalogue below, the product type, and any model codes to decide.
2. WRITE THE CLEAN NAME as "Brand + Product Type", plus a distinguishing detail such as colour, weight or model when it is in the original. For example "Prada Canvas Carpenter Pants - Navy" or "Chrome Hearts KSCut Jeans - Black 16.5oz".
3. STRIP THE NOISE: remove 包邮 (free shipping), store names, promotional filler, emoji, stray punctuation and slash-separated spec dumps that add nothing.
4. KEEP model codes, sizes, weights and colours that identify the item.

Rules:
- {workshop} is the workshop, NOT a brand. Never use it as the brand name.
- If you cannot work out the brand, use the product type alone, for example "Heavyweight Sweatshirt". Never guess a brand you have no evidence for.
- If the entry is not a real product (a universal or placeholder link such as 万能链接, a deposit link, a test item), just write what it is, for example "Universal link".
- No emoji in the output.
- Keep each name short. A product name, not a sentence.

Catalogue of products this workshop is known to make:
{known}

Reply with exactly one cleaned name per line, numbered the same way as the input, and nothing else.

{numbered}"""


def translate_names(names, cache):
    """
    Zet rauwe Weidian-namen om in schone Engelse productnamen met merk.
    Al eerder verwerkte namen komen uit de cache, zodat elke naam maar een
    keer door de API gaat.
    """
    todo = [n for n in dict.fromkeys(names) if n and n not in cache]
    if not todo:
        return cache

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        for n in todo:
            cache[n] = n
        return cache

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    prompt = build_name_prompt(todo, load_catalogue())

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("stop_reason") == "max_tokens":
            print("  ! antwoord liep tegen de tokenlimiet aan")

        blocks = payload.get("content", [])
        raw = "".join(b["text"] for b in blocks if b.get("type") == "text")

        for line in raw.splitlines():
            m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(todo):
                cleaned = m.group(2).strip().strip('"')
                if cleaned:
                    cache[todo[idx]] = cleaned

        missed = [n for n in todo if n not in cache]
        for n in missed:
            cache[n] = n
        print(f"  {len(todo) - len(missed)} van {len(todo)} naam/namen opgeschoond"
              + (f", {len(missed)} onveranderd gelaten" if missed else ""))
    except Exception as e:
        print(f"  ! naamverwerking mislukt ({type(e).__name__}: {e}); "
              "originele namen worden gebruikt")
        for n in todo:
            cache.setdefault(n, n)

    return cache


# ---------------------------------------------------------------------------
# Verschillen bepalen
# ---------------------------------------------------------------------------

def in_stock(product) -> bool:
    stock = product.get("stock")
    if stock is None:
        return True  # onbekend: behandel als beschikbaar
    return stock > 0


def diff_products(old, new):
    """Geeft een lijst met (soort, product, extra) terug."""
    changes = []

    for pid, item in new.items():
        previous = old.get(pid)

        if previous is None:
            changes.append(("new", item, {}))
            continue

        was, now = in_stock(previous), in_stock(item)
        if was and not now:
            changes.append(("sold_out", item, {}))
        elif not was and now:
            changes.append(("restock", item, {}))

        old_price, new_price = previous.get("price"), item.get("price")
        if old_price is not None and new_price is not None and old_price != new_price:
            changes.append(("price", item, {"old_price": old_price}))

    for pid, item in old.items():
        if pid not in new:
            changes.append(("removed", item, {}))

    return changes


# ---------------------------------------------------------------------------
# Naar Discord posten
# ---------------------------------------------------------------------------

def stock_text(value):
    """Weidian gebruikt extreem hoge getallen voor 'onbeperkt'."""
    if value is None:
        return "unknown"
    if value >= 100000:
        return "Unlimited"
    return str(value)


def money(value):
    if value is None:
        return "unknown"
    if float(value).is_integer():
        return f"{int(value):,} CNY"
    return f"{value:,.2f} CNY"


def build_embed(kind, product, extra, translations):
    name = translations.get(product["name"], product["name"])
    embed = {
        "title": name[:250],
        "url": product["url"],
        "color": COLORS[kind],
        "author": {"name": LABELS[kind]},
        "fields": [],
        "footer": {"text": f"718 Made  -  item {product['id']}"},
    }

    if kind == "price":
        embed["fields"].append({
            "name": "Price",
            "value": f"~~{money(extra['old_price'])}~~  ->  **{money(product['price'])}**",
            "inline": True,
        })
    else:
        embed["fields"].append({
            "name": "Price",
            "value": money(product["price"]),
            "inline": True,
        })

    if product.get("stock") is not None and kind != "removed":
        embed["fields"].append({
            "name": "Stock",
            "value": stock_text(product["stock"]),
            "inline": True,
        })

    if product.get("sold"):
        embed["fields"].append({
            "name": "Sold",
            "value": f"{product['sold']:,}",
            "inline": True,
        })

    if product.get("image"):
        embed["thumbnail"] = {"url": product["image"]}

    # De originele Chinese naam eronder, handig om in de shop terug te vinden
    if name != product["name"]:
        embed["description"] = product["name"][:300]

    return embed


def post_changes(webhook_url, changes, translations):
    embeds = [build_embed(k, p, e, translations) for k, p, e in changes]

    for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        batch = embeds[i:i + MAX_EMBEDS_PER_MESSAGE]
        resp = requests.post(webhook_url, json={"embeds": batch}, timeout=30)
        resp.raise_for_status()
        if i + MAX_EMBEDS_PER_MESSAGE < len(embeds):
            time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

    state = load_state()
    old_products = state.get("products", {})
    translations = state.get("translations", {})

    print(f"Shop {SHOP_ID} ophalen...")
    new_products = fetch_products()

    if not new_products:
        print(
            "Geen producten gevonden. Waarschijnlijk klopt het API-endpoint niet.\n"
            "Zet WEIDIAN_API_URL in de workflow op het verzoek dat de shoppagina "
            "zelf doet (zie README: 'Endpoint achterhalen')."
        )
        return

    print(f"{len(new_products)} product(en) in de shop.")

    # TEST_POST=n plaatst n willekeurige huidige producten als voorbeeld,
    # zodat je kunt zien hoe de berichten eruitzien zonder te wachten tot er
    # echt iets verandert. De momentopname blijft hierbij ongemoeid.
    try:
        test_post = int(os.environ.get("TEST_POST", "0") or "0")
    except ValueError:
        test_post = 0

    if test_post > 0:
        sample = list(new_products.values())[:test_post]
        print(f"TEST_POST={test_post}: {len(sample)} voorbeeldbericht(en) "
              "worden geplaatst.")
        kinds = ["new", "restock", "sold_out", "price", "removed"]
        changes = []
        for i, product in enumerate(sample):
            kind = kinds[i % len(kinds)]
            extra = {"old_price": (product.get("price") or 0) + 50} \
                if kind == "price" else {}
            changes.append((kind, product, extra))

        translations = translate_names([p["name"] for _, p, _ in changes],
                                       translations)
        post_changes(webhook_url, changes, translations)

        # Momentopname wel bijwerken als die er nog niet was, maar geen
        # echte wijzigingen melden.
        if not state.get("initialised"):
            state.update({"products": new_products, "initialised": True})
        state["translations"] = translations
        save_state(state)
        print("Testberichten geplaatst.")
        return

    if not state.get("initialised"):
        state.update({
            "products": new_products,
            "translations": translations,
            "initialised": True,
        })
        save_state(state)
        print("Eerste run: momentopname vastgelegd, niets geplaatst. "
              "Vanaf nu worden alleen wijzigingen gemeld.")
        return

    changes = diff_products(old_products, new_products)

    if not changes:
        print("Geen wijzigingen.")
        state["products"] = new_products
        save_state(state)
        return

    summary = {}
    for kind, _, _ in changes:
        summary[kind] = summary.get(kind, 0) + 1
    print("Wijzigingen: " + ", ".join(f"{LABELS[k]}: {v}" for k, v in summary.items()))

    translations = translate_names([p["name"] for _, p, _ in changes], translations)
    post_changes(webhook_url, changes, translations)

    state.update({"products": new_products, "translations": translations})
    save_state(state)
    print(f"Klaar. {len(changes)} wijziging(en) geplaatst.")


if __name__ == "__main__":
    main()
