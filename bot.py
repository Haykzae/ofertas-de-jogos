"""Steam e GOG: links diretos, preços BRL e filtros."""

import html
import json
import os
import re
import time
import unicodedata
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


STATE = Path("data/enviados.json")

BLOCKED = {
    "puzzle", "hidden object", "word game",
    "match 3", "match-3", "jigsaw",
    "sokoban", "nonogram",
}

STORE_HOSTS = {
    "store.steampowered.com",
    "gog.com",
    "www.gog.com",
    "store.epicgames.com",
    "www.epicgames.com",
}

META = {}


class ServiceHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


def fetch(url, payload=None, mode="json"):
    body = (
        json.dumps(payload).encode("utf-8")
        if payload is not None else None
    )

    for attempt in range(3):
        try:
            req = Request(
                url,
                data=body,
                headers={
                    "User-Agent": "GameDealsNotifier/3.1",
                    "Content-Type": "application/json",
                },
            )

            with urlopen(req, timeout=25) as response:
                if mode == "url":
                    return response.geturl()

                text = response.read().decode("utf-8")

                if mode == "text":
                    return text

                return json.loads(text) if text else None

        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                # Não exibe o endereço do webhook.
                raise ServiceHTTPError(exc.code) from None

            delay = 2 ** (attempt + 1)

            try:
                delay = float(
                    exc.headers.get("Retry-After", delay)
                )
            except ValueError:
                pass

        except (URLError, TimeoutError):
            delay = 2 ** (attempt + 1)

        if attempt < 2:
            time.sleep(min(max(delay, 1), 30))

    raise RuntimeError(
        "Serviço indisponível após três tentativas."
    )


def cached(key, function):
    record = META["cache"].get(key)

    if record and time.time() - record["time"] < 86400:
        return record["value"]

    value = function()

    META["cache"][key] = {
        "time": int(time.time()),
        "value": value,
    }

    return value


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()

        if tag == "a" and "app_tag" in classes:
            self.active = True
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            value = " ".join("".join(self.parts).split())

            if value and value not in self.tags:
                self.tags.append(value)

            self.active = False


def quality(app_id):
    def load():
        query = urlencode({
            "json": 1,
            "language": "all",
            "filter": "all",
            "review_type": "all",
            "purchase_type": "all",
            "num_per_page": 1,
        })

        data = fetch(
            f"https://store.steampowered.com/"
            f"appreviews/{app_id}?{query}"
        )

        if data.get("success") != 1:
            raise RuntimeError(
                "Avaliações da Steam indisponíveis."
            )

        summary = data.get("query_summary", {})
        total = int(summary.get("total_reviews", 0))
        positive = int(summary.get("total_positive", 0))

        if total < 500 or positive * 100 < total * 80:
            return None

        time.sleep(0.4)

        parser = Tags()
        parser.feed(fetch(
            f"https://store.steampowered.com/"
            f"app/{app_id}/?l=english&cc=br",
            mode="text",
        ))

        main_tags = parser.tags[:2]

        if not main_tags:
            return None

        if any(
            tag.casefold() in BLOCKED
            for tag in main_tags
        ):
            return None

        return {
            "rating": positive * 100 / total,
            "count": total,
            "tags": main_tags,
        }

    if not str(app_id).isdigit() or int(app_id) <= 0:
        return None

    return cached("quality:" + str(app_id), load)


def normalize(title):
    title = unicodedata.normalize(
        "NFKD", html.unescape(title)
    )

    title = "".join(
        char for char in title
        if not unicodedata.combining(char)
    )

    return re.sub("[^a-z0-9]", "", title.casefold())


def find_app(title):
    def load():
        query = urlencode({
            "term": title,
            "l": "english",
            "cc": "br",
        })

        data = fetch(
            "https://store.steampowered.com/"
            "api/storesearch?" + query
        )

        ids = {
            str(item["id"])
            for item in data.get("items", [])
            if normalize(item.get("name", ""))
            == normalize(title)
        }

        return ids.pop() if len(ids) == 1 else None

    return cached("match:" + normalize(title), load)


def steam_candidates():
    start = int(META.get("steam_start", 0))

    query = urlencode({
        "start": start,
        "count": 50,
        "specials": 1,
        "category1": 998,
        "cc": "br",
        "l": "english",
        "json": 1,
        "infinite": 1,
    })

    data = fetch(
        "https://store.steampowered.com/"
        "search/results/?" + query
    )

    if not data.get("success") or "results_html" not in data:
        raise RuntimeError("Busca da Steam inválida.")

    ids = list(dict.fromkeys(re.findall(
        r'data-ds-appid="(\d+)"',
        data["results_html"],
    )))

    total = int(data.get("total_count", 0))

    META["steam_start"] = (
        start + 50
        if ids and start + 50 < total else 0
    )

    print(
        f"Steam: lote a partir de {start}; "
        f"{len(ids)} jogos.",
        flush=True,
    )

    for app_id in ids:
        query = urlencode({
            "appids": app_id,
            "cc": "br",
            "l": "english",
        })

        data = fetch(
            "https://store.steampowered.com/"
            "api/appdetails?" + query
        )

        entry = data.get(app_id, {})

        if not entry.get("success"):
            continue

        game = entry["data"]
        price = game.get("price_overview", {})

        if (
            game.get("type") != "game"
            or price.get("currency") != "BRL"
        ):
            continue

        base = int(price["initial"])
        final = int(price["final"])

        if base <= 0:
            continue

        if (base - final) * 100 < base * 30:
            continue

        yield {
            "id": app_id,
            "app": app_id,
            "store": "Steam",
            "title": game["name"],
            "price": Decimal(final) / 100,
            "discount": (base - final) * 100 / base,
            "url": (
                "https://store.steampowered.com/"
                f"app/{app_id}/"
            ),
            "image": game.get("header_image"),
        }

        time.sleep(0.4)


def gog_candidates():
    page = int(META.get("gog_page", 1))

    query = urlencode({
        "limit": 48,
        "page": page,
        "order": "desc:trending",
        "productType": "in:game",
        "discounted": "eq:true",
        "countryCode": "BR",
        "locale": "en-US",
        "currencyCode": "BRL",
    })

    data = fetch(
        "https://catalog.gog.com/v1/catalog?" + query
    )

    if not isinstance(data.get("products"), list):
        raise RuntimeError("Catálogo da GOG inválido.")

    META["gog_page"] = (
        page + 1
        if page < int(data.get("pages", 1)) else 1
    )

    print(f"GOG: lote da página {page}.", flush=True)

    for game in data["products"]:
        price = game.get("price", {})
        base_money = price.get("baseMoney", {})
        final_money = price.get("finalMoney", {})

        if (
            game.get("productType") != "game"
            or final_money.get("currency") != "BRL"
        ):
            continue

        base = Decimal(base_money["amount"])
        final = Decimal(final_money["amount"])

        if base <= 0:
            continue

        if (base - final) * 100 < base * 30:
            continue

        link = game.get("storeLink", "")

        if urlparse(link).hostname not in {
            "gog.com", "www.gog.com"
        }:
            continue

        yield {
            "id": game["id"],
            "app": None,
            "store": "GOG",
            "title": game["title"],
            "price": final,
            "discount": float(
                (base - final) * 100 / base
            ),
            "url": link,
            "image": game.get("coverHorizontal"),
        }


def direct_store_link(row):
    # Prioriza links de loja já presentes no anúncio.
    text = html.unescape(" ".join(
        str(row.get(field, ""))
        for field in (
            "description",
            "instructions",
            "open_giveaway_url",
        )
    ))

    links = re.findall(
        r'https?://[^\s<>"\']+',
        text,
    )

    valid = []

    for link in links:
        link = link.rstrip(".,);]")
        parsed = urlparse(link)
        path = parsed.path

        is_product = (
            parsed.hostname == "store.steampowered.com"
            and re.match(r"^/app/\d+", path)
        ) or (
            parsed.hostname in {"gog.com", "www.gog.com"}
            and re.match(r"^/(?:[a-z]{2}/)?game/", path)
        ) or (
            parsed.hostname in {
                "store.epicgames.com",
                "www.epicgames.com",
            }
            and re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?p/", path)
        )

        if is_product and link not in valid:
            valid.append(link)

    # Não escolhe entre produtos diferentes.
    if len(valid) == 1:
        return valid[0]

    link = row.get("open_giveaway_url", "")

    if urlparse(link).hostname not in {
        "gamerpower.com", "www.gamerpower.com"
    }:
        return None

    try:
        direct = fetch(link, mode="url")

    except ServiceHTTPError as exc:
        if exc.status == 403:
            print(
                f"AVISO: link do anúncio {row.get('id')} "
                "bloqueado com HTTP 403; resgate ignorado.",
                flush=True,
            )
            return None
        raise

    if urlparse(direct).hostname in STORE_HOSTS:
        return direct

    return None


def giveaway_candidates():
    try:
        rows = fetch(
            "https://www.gamerpower.com/api/giveaways"
            "?platform=pc&type=game&sort-by=date"
        )

    except ServiceHTTPError as exc:
        if exc.status == 403:
            print(
                "AVISO: GamerPower recusou acesso com HTTP 403. "
                "A busca de resgates grátis foi pulada nesta "
                "execução. Steam e GOG continuarão.",
                flush=True,
            )
            return
        raise

    if isinstance(rows, dict):
        if str(rows.get("status")) in {"0", "201"}:
            return

    if not isinstance(rows, list):
        raise RuntimeError("Lista de jogos grátis inválida.")

    for row in rows:
        direct = direct_store_link(row)

        if not direct:
            continue

        match = re.search(
            r"store\.steampowered\.com/app/(\d+)",
            direct,
        )

        title = re.sub(
            r"\s*\([^)]*(?:giveaway|steam|epic|gog|free)"
            r"[^)]*\)\s*$",
            "",
            row["title"],
            flags=re.I,
        )

        title = re.sub(
            r"\s+(?:(?:steam|epic games|gog|pc)\s+)?"
            r"(?:game\s+|key\s+)?giveaway\s*$",
            "",
            title,
            flags=re.I,
        ).strip()

        yield {
            "id": row["id"],
            "app": match.group(1) if match else None,
            "store": "Resgate grátis",
            "title": title,
            "price": Decimal(0),
            "discount": 100,
            "url": direct,
            "image": row.get("image"),
            "free": True,
            "deadline": row.get(
                "end_date", "Confira na loja"
            ),
        }


def save(sent):
    sent["_direct_v3"] = META
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            sent,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(STATE)


def main():
    global META

    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL", ""
    ).strip()

    parsed = urlparse(webhook)

    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or not parsed.path.startswith("/api/webhooks/")
    ):
        raise RuntimeError(
            "Confira o segredo DISCORD_WEBHOOK_URL."
        )

    sent = (
        json.loads(STATE.read_text("utf-8"))
        if STATE.exists() else {}
    )

    if not isinstance(sent, dict):
        raise RuntimeError("Histórico inválido.")

    META = sent.get("_direct_v3", {"cache": {}})
    META.setdefault("cache", {})

    META["cache"] = {
        key: value
        for key, value in META["cache"].items()
        if time.time() - value["time"] < 86400
    }

    errors = []
    total = 0

    sources = [
        ("Grátis", giveaway_candidates),
        ("Steam", steam_candidates),
        ("GOG", gog_candidates),
    ]

    for name, source in sources:
        deadline = time.monotonic() + 240
        count = 0

        try:
            for game in source():
                if time.monotonic() > deadline or count >= 35:
                    break

                if game.get("free"):
                    key = f"gp:{game['id']}"
                else:
                    key = (
                        f"{game['store'].lower()}-brl:"
                        f"{game['id']}:"
                        f"{game['price']:.2f}"
                    )

                if key in sent:
                    continue

                try:
                    app_id = (
                        game["app"]
                        or find_app(game["title"])
                    )

                    info = quality(app_id)

                except Exception as exc:
                    print(
                        f"Verificação de {game['title']}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    continue

                if not info:
                    continue

                if game["price"] == 0:
                    price = (
                        "🎁 GRÁTIS — confira "
                        "as condições de resgate"
                    )
                else:
                    price = (
                        "R$ "
                        + f"{game['price']:.2f}".replace(".", ",")
                    )

                description = (
                    f"**{price}** · "
                    f"{game['discount']:.1f}% de desconto\n"
                    f"Loja: {game['store']}\n"
                    f"Steam: {info['rating']:.1f}% positivas "
                    f"· {info['count']} avaliações\n"
                    f"Tags principais: "
                    f"{', '.join(info['tags'])}\n"
                    "Confira preço, região e condições na loja."
                )

                if game.get("free"):
                    description += (
                        f"\nPrazo: {game['deadline']}\n"
                        "[Fonte: GamerPower]"
                        "(https://www.gamerpower.com/)"
                    )

                embed = {
                    "title": game["title"][:200],
                    "url": game["url"],
                    "description": description,
                    "color": (
                        0x2ECC71
                        if game["price"] == 0 else 0x3498DB
                    ),
                }

                image = game.get("image")

                if (
                    image
                    and urlparse(image).scheme
                    in {"https", "http"}
                ):
                    embed["image"] = {"url": image}

                separator = "&" if "?" in webhook else "?"

                fetch(
                    webhook + separator + "wait=true",
                    payload={
                        "username": "Ofertas de jogos",
                        "allowed_mentions": {"parse": []},
                        "embeds": [embed],
                    },
                )

                sent[key] = int(time.time())
                save(sent)

                count += 1
                total += 1

                time.sleep(1)

        except Exception as exc:
            detail = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            print(f"ERRO: {detail}", flush=True)

        finally:
            save(sent)

        print(
            f"{name}: {count} avisos enviados.",
            flush=True,
        )

    print(f"Total: {total} avisos.", flush=True)

    if errors:
        raise RuntimeError(" | ".join(errors))


if __name__ == "__main__":
    main()
