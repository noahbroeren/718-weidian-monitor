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

Productnamen worden getoond zoals ze in de shop staan.
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")

SHOP_ID = os.environ.get("WEIDIAN_SHOP_ID", "1611988515")

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
    """
    Leest de momentopname. Hoort die bij een andere shop, dan wordt opnieuw
    begonnen: anders zou de hele oude catalogus als "verwijderd" en de nieuwe
    als "nieuw" in het kanaal belanden.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict) and data.get("shop_id") == SHOP_ID:
            data.setdefault("products", {})
            return data
        if data:
            print(f"Momentopname hoort niet bij shop {SHOP_ID}; "
                  "er wordt opnieuw begonnen.")
    return {"shop_id": SHOP_ID, "products": {}, "initialised": False}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)



# ---------------------------------------------------------------------------
# Afbeeldingen, wisselkoers en ruisfilter
# ---------------------------------------------------------------------------

SQUARE_IMAGES = os.environ.get("SQUARE_IMAGES", "true").lower() != "false"
SQUARE_SIZE = 800

# Items die geen echt product zijn en dus geen melding verdienen
SKIP_PATTERNS = [p.strip() for p in os.environ.get(
    "SKIP_PATTERNS", "万能链接,邮费,补拍,定金,尾款,差价,运费"
).split(",") if p.strip()]

_square_cache = {}


def square_image_url(url):
    """
    Weidian-afbeeldingen eindigen vaak op _<breedte>_<hoogte>.jpg. Door daar
    een vierkant formaat van te maken krijg je nette 1:1 foto's in Discord.
    De URL wordt gecontroleerd voordat hij gebruikt wordt, zodat er nooit een
    kapotte afbeelding in het bericht komt.
    """
    if not url or not SQUARE_IMAGES:
        return url
    if url in _square_cache:
        return _square_cache[url]

    candidate = re.sub(
        r"_\d+_\d+\.(jpg|jpeg|png|webp)$",
        f"_{SQUARE_SIZE}_{SQUARE_SIZE}.\\1",
        url,
        flags=re.IGNORECASE,
    )

    if candidate == url:
        _square_cache[url] = url
        return url

    try:
        r = requests.head(candidate, timeout=10, allow_redirects=True)
        ok = r.status_code == 200
    except requests.RequestException:
        ok = False

    _square_cache[url] = candidate if ok else url
    if not ok:
        print("  (vierkante variant niet beschikbaar, originele foto gebruikt)")
    return _square_cache[url]


def usd_rate():
    """Haalt de CNY->USD koers op. Valt terug op een vaste koers."""
    fixed = os.environ.get("USD_RATE")
    if fixed:
        try:
            return float(fixed)
        except ValueError:
            pass
    try:
        r = requests.get("https://open.er-api.com/v6/latest/CNY", timeout=15)
        rate = r.json().get("rates", {}).get("USD")
        if rate:
            return float(rate)
    except (requests.RequestException, ValueError, AttributeError):
        pass
    print("  (kon de wisselkoers niet ophalen; USD-prijs wordt weggelaten)")
    return None


def is_noise(product) -> bool:
    name = product.get("name", "")
    return any(p in name for p in SKIP_PATTERNS)


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

def cny(value):
    if value is None:
        return "?"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


def money(value, rate=None):
    """Prijs als CNY met de dollarkoers eronder."""
    if value is None:
        return "unknown"
    text = f"**{cny(value)} CNY**"
    if rate:
        text += f"\n${value * rate:,.2f}"
    return text


def money_change(old, new, rate=None):
    """Prijswijziging: oude prijs doorgestreept, nieuwe vet, elk op een regel."""
    line = f"~~{cny(old)} CNY~~ -> **{cny(new)} CNY**"
    if rate and old is not None and new is not None:
        line += f"\n~~${old * rate:,.2f}~~ -> ${new * rate:,.2f}"
    return line


def build_embed(kind, product, extra, rate=None):
    embed = {
        "title": product["name"][:250],
        "url": product["url"],
        "color": COLORS[kind],
        "author": {"name": LABELS[kind]},
        "fields": [],
        "footer": {"text": f"718 Made  -  item {product['id']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if kind == "price":
        embed["fields"].append({
            "name": "Price",
            "value": money_change(extra["old_price"], product["price"], rate),
            "inline": False,
        })
    else:
        embed["fields"].append({
            "name": "Price",
            "value": money(product["price"], rate),
            "inline": True,
        })

    # Voorraad alleen tonen als het getal iets betekent
    stock = product.get("stock")
    if stock is not None and kind != "removed" and stock < 100000:
        embed["fields"].append({
            "name": "Stock",
            "value": str(stock),
            "inline": True,
        })

    if product.get("sold"):
        embed["fields"].append({
            "name": "Sold",
            "value": f"{product['sold']:,}",
            "inline": True,
        })

    if product.get("image"):
        embed["thumbnail"] = {"url": square_image_url(product["image"])}

    return embed


def post_changes(webhook_url, changes, rate=None):
    embeds = [build_embed(k, p, e, rate) for k, p, e in changes]

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

    print(f"Shop {SHOP_ID} ophalen...")
    new_products = fetch_products()

    if not new_products:
        print(
            "Geen producten gevonden. Waarschijnlijk klopt het API-endpoint niet.\n"
            "Zet WEIDIAN_API_URL in de workflow op het verzoek dat de shoppagina "
            "zelf doet (zie README: 'Endpoint achterhalen')."
        )
        return

    skipped = {k: v for k, v in new_products.items() if is_noise(v)}
    if skipped:
        new_products = {k: v for k, v in new_products.items() if k not in skipped}
        print(f"{len(skipped)} item(s) overgeslagen als ruis "
              "(万能链接, aanbetaling, verzendkosten).")

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

        post_changes(webhook_url, changes, usd_rate())

        # Momentopname wel bijwerken als die er nog niet was, maar geen
        # echte wijzigingen melden.
        if not state.get("initialised"):
            state.update({"products": new_products, "initialised": True})
            save_state(state)
        print("Testberichten geplaatst.")
        return

    if not state.get("initialised"):
        state.update({"products": new_products, "initialised": True})
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

    post_changes(webhook_url, changes, usd_rate())

    state["products"] = new_products
    save_state(state)
    print(f"Klaar. {len(changes)} wijziging(en) geplaatst.")


if __name__ == "__main__":
    main()
