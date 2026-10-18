"""Ofertas de jogos com filtros de qualidade e imagens grandes."""

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

MIN_POSITIVE = 80
MIN_REVIEWS = 500
MIN_DISCOUNT = 30
MAX_SEND = 100

# Impede que consultas extensas ultrapassem o tempo do workflow.
SEARCH_SECONDS = 12 * 60

BLOCKED_TAGS = {
    "puzzle",
    "hidden object",
    "word game",
    "match 3",
    "match-3",
    "jigsaw",
    "sokoban",
    "nonogram",
}

QUALITY_CACHE = {}
STARTED = 0


def time_available():
    return time.monotonic() - STARTED < SEARCH_SECONDS


def fetch(url, payload=None, as_text=False, with_headers=False):
    body = (
        json.dumps(payload).encode("utf-8")
        if payload is not None
        else None
    )

    last_error = "Serviço indisponível"

    for attempt in range(3):
        try:
            req = Request(
                url,
                data=body,
                headers={
                    "User-Agent": "GameDealsNotifier/2.0",
                    "Accept": "*/*",
                    "Content-Type": "application/json",
                },
            )

            with urlopen(req, timeout=25) as response:
                raw = response.read().decode("utf-8")

                if as_text:
                    data = raw
                else:
                    data = json.loads(raw) if raw else None

                if with_headers:
                    return data, dict(response.headers.items())

                return data

        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"

            if exc.code != 429 and exc.code < 500:
                # Não expõe a URL do webhook.
                raise RuntimeError(last_error) from None

            try:
                delay = float(
                    exc.headers.get("Retry-After", 2 ** (attempt + 1))
                )
            except (TypeError, ValueError):
                delay = 2 ** (attempt + 1)

        except (URLError, TimeoutError):
            last_error = "Falha de conexão ou tempo limite"
            delay = 2 ** (attempt + 1)

        except (ValueError, UnicodeError):
            raise RuntimeError(
                "Resposta recebida em formato inesperado."
            ) from None

        if attempt < 2:
            time.sleep(min(max(delay, 1), 30))

    raise RuntimeError(f"{last_error}, após três tentativas.")


class TagParser(HTMLParser):
    """Extrai as tags exibidas na página do jogo."""

    def __init__(self):
        super().__init__()
        self.collecting = False
        self.parts = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()

        if tag == "a" and "app_tag" in classes:
            self.collecting = True
            self.parts = []

    def handle_data(self, data):
        if self.collecting:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.collecting:
            value = " ".join("".join(self.parts).split())

            if value and value not in self.tags:
                self.tags.append(value)

            self.collecting = False


def quality(app_id):
    app_id = str(app_id or "")

    if not app_id.isdigit() or int(app_id) <= 0:
        return None

    if app_id in QUALITY_CACHE:
        return QUALITY_CACHE[app_id]

    if not time_available():
        return None

    try:
        query = urlencode({
            "json": 1,
            "language": "all",
            "filter": "all",
            "review_type": "all",
            "purchase_type": "all",
            "num_per_page": 1,
        })

        data = fetch(
            f"https://store.steampowered.com/appreviews/{app_id}?"
            + query
        )

        if not isinstance(data, dict) or data.get("success") != 1:
            raise RuntimeError("Avaliações indisponíveis.")

        summary = data.get("query_summary", {})
        total = int(summary.get("total_reviews", 0))
        positive = int(summary.get("total_positive", 0))

        if total < MIN_REVIEWS:
            QUALITY_CACHE[app_id] = None
            return None

        # Comparação exata, sem arredondar para aprovar.
        if positive * 100 < MIN_POSITIVE * total:
            QUALITY_CACHE[app_id] = None
            return None

        time.sleep(0.4)

        page = fetch(
            f"https://store.steampowered.com/app/{app_id}/"
            "?l=english&cc=us",
            as_text=True,
        )

        parser = TagParser()
        parser.feed(page)

        if not parser.tags:
            # Sem tags, não conseguimos conferir o gênero.
            QUALITY_CACHE[app_id] = None
            return None

        main_tags = parser.tags[:2]

        if any(tag.casefold() in BLOCKED_TAGS for tag in main_tags):
            QUALITY_CACHE[app_id] = None
            return None

        result = {
            "positive": positive * 100 / total,
            "reviews": total,
            "tags": main_tags,
        }

        QUALITY_CACHE[app_id] = result
        time.sleep(0.4)
        return result

    except Exception as exc:
        print(
            f"Steam {app_id}: não foi possível verificar "
            f"({type(exc).__name__}: {exc}).",
            flush=True,
        )

        # Não aprova jogos sem dados verificáveis.
        QUALITY_CACHE[app_id] = None
        return None


def normalize_title(value):
    value = unicodedata.normalize("NFKD", html.unescape(value))
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )
    value = value.casefold()
    return re.sub(r"[^a-z0-9]+", "", value)


def giveaway_app_id(row):
    # Primeiro procura um link direto da Steam nos dados.
    text = " ".join(
        str(row.get(field, ""))
        for field in (
            "description",
            "instructions",
            "open_giveaway_url",
            "giveaway_url",
        )
    )

    matches = set(re.findall(
        r"store\.steampowered\.com/app/(\d+)",
        text,
        flags=re.I,
    ))

    if len(matches) == 1:
        return matches.pop()

    # Se não houver um link direto, busca pelo título.
    title = html.unescape(str(row.get("title", "")))

    # Remove apenas sufixos comuns de anúncios de resgate.
    title = re.sub(
        r"\s*\([^)]*(?:giveaway|steam|epic|gog|free)[^)]*\)\s*$",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"\s+(?:(?:steam|epic games|gog|pc)\s+)?"
        r"(?:game\s+|key\s+)?giveaway\s*$",
        "",
        title,
        flags=re.I,
    ).strip()

    if not title or not time_available():
        return None

    try:
        data = fetch(
            "https://store.steampowered.com/api/storesearch?"
            + urlencode({
                "term": title,
                "l": "english",
                "cc": "us",
            })
        )

        wanted = normalize_title(title)

        matches = {
            str(item["id"])
            for item in data.get("items", [])
            if normalize_title(str(item.get("name", ""))) == wanted
        }

        # Só associa quando há uma correspondência exata e única.
        if len(matches) == 1:
            return matches.pop()

    except Exception as exc:
        print(
            f"Busca Steam indisponível: {type(exc).__name__}.",
            flush=True,
        )

    return None


def add_image(embed, url):
    if not isinstance(url, str):
        return

    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme in ("https", "http") and parsed.hostname:
        embed["image"] = {"url": url}


def review_text(info):
    count = f"{info['reviews']:,}".replace(",", ".")
    return (
        f"Steam: {info['positive']:.1f}% positivas "
        f"· {count} avaliações\n"
        f"Tags principais: {', '.join(info['tags'])}"
    )


def free_games(sent):
    rows = fetch(
        "https://www.gamerpower.com/api/giveaways"
        "?platform=pc&type=game&sort-by=date"
    )

    if isinstance(rows, dict):
        if str(rows.get("status")) in {"0", "201"}:
            return []
        raise RuntimeError("Resposta inesperada do GamerPower.")

    if not isinstance(rows, list):
        raise RuntimeError("Lista de jogos grátis inválida.")

    result = []

    for row in rows:
        if not time_available():
            break

        key = f"gp:{row['id']}"

        if key in sent:
            continue

        app_id = giveaway_app_id(row)
        info = quality(app_id)

        if not info:
            continue

        embed = {
            "title": str(row["title"])[:200],
            "url": row["open_giveaway_url"],
            "description": (
                f"🎁 **GRÁTIS**\n"
                f"Plataformas: {row.get('platforms', 'PC')}\n"
                f"{review_text(info)}\n"
                f"Prazo: {row.get('end_date', 'Confira no link')}\n"
                "Confira região, estoque e instruções de resgate.\n"
                "[Fonte: GamerPower](https://www.gamerpower.com/)"
            ),
            "color": 0x2ECC71,
        }

        add_image(
            embed,
            row.get("image") or row.get("thumbnail"),
        )

        result.append((key, embed))

    return result


def discounts(sent):
    stores_data = fetch(
        "https://www.cheapshark.com/api/1.0/stores"
    )

    if not isinstance(stores_data, list):
        raise RuntimeError("Lista de lojas inválida.")

    stores = {
        str(store["storeID"]): store["storeName"]
        for store in stores_data
    }

    result = []
    page = 0

    while time_available():
        params = urlencode({
            "pageNumber": page,
            "pageSize": 60,
            "sortBy": "Savings",
            "onSale": 1,
        })

        rows, headers = fetch(
            "https://www.cheapshark.com/api/1.0/deals?" + params,
            with_headers=True,
        )

        if not isinstance(rows, list):
            raise RuntimeError(
                f"Lista de ofertas inválida na página {page}."
            )

        headers = {
            key.lower(): value
            for key, value in headers.items()
        }

        total_value = headers.get("x-total-page-count")
        total_pages = (
            int(total_value) if total_value is not None else None
        )

        print(
            f"CheapShark: página {page}; "
            f"{len(rows)} ofertas.",
            flush=True,
        )

        for row in rows:
            if not time_available():
                break

            saving = Decimal(str(row["savings"]))

            if saving < MIN_DISCOUNT:
                continue

            # Pré-filtro rápido antes de consultar a Steam.
            rating = Decimal(
                str(row.get("steamRatingPercent") or "0")
            )
            count = int(row.get("steamRatingCount") or 0)

            if rating < MIN_POSITIVE or count < MIN_REVIEWS:
                continue

            price = Decimal(str(row["salePrice"]))
            key = (
                f"cs:{row['gameID']}:{row['storeID']}:{price}"
            )

            if key in sent:
                continue

            info = quality(row.get("steamAppID"))

            if not info:
                continue

            label = (
                "🎁 **GRÁTIS**"
                if price == 0
                else (
                    f"🏷️ **US$ {price:.2f}** "
                    f"· {saving:.1f}% de desconto"
                )
            )

            link = (
                "https://www.cheapshark.com/redirect?"
                + urlencode({"dealID": row["dealID"]})
            )

            embed = {
                "title": str(row["title"])[:200],
                "url": link,
                "description": (
                    f"{label}\n"
                    f"Loja: "
                    f"{stores.get(str(row['storeID']), 'Loja')}\n"
                    f"{review_text(info)}\n"
                    "Preço em dólares. Confira o valor "
                    "e a disponibilidade no Brasil."
                ),
                "color": 0x2ECC71 if price == 0 else 0x3498DB,
                "footer": {"text": "Fonte: CheapShark"},
            }

            add_image(embed, row.get("thumb"))
            result.append((key, embed))

            if len(result) >= MAX_SEND:
                return result

        if not rows or len(rows) < 60:
            break

        if total_pages is not None and page + 1 >= total_pages:
            break

        if any(
            Decimal(str(row["savings"])) < MIN_DISCOUNT
            for row in rows
        ):
            break

        page += 1
        time.sleep(0.5)

    return result


def save_history(sent):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(sent, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temporary.replace(STATE)


def main():
    global STARTED
    STARTED = time.monotonic()

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
            "Configure o segredo DISCORD_WEBHOOK_URL "
            "com a URL do webhook do Discord."
        )

    sent = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {}
    )

    if not isinstance(sent, dict):
        raise RuntimeError("Histórico de envios inválido.")

    offers = []
    errors = []

    for name, source in (
        ("GamerPower", free_games),
        ("CheapShark", discounts),
    ):
        if not time_available():
            print("Limite de consulta atingido.", flush=True)
            break

        try:
            found = source(sent)
            offers.extend(found)

            print(
                f"{name}: {len(found)} ofertas "
                "aprovadas pelo filtro.",
                flush=True,
            )

        except Exception as exc:
            detail = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(detail)
            print(f"ERRO — {detail}", flush=True)

    pending = list({
        key: embed
        for key, embed in offers
        if key not in sent
    }.items())[:MAX_SEND]

    for start in range(0, len(pending), 10):
        batch = pending[start:start + 10]
        separator = "&" if "?" in webhook else "?"

        fetch(
            webhook + separator + "wait=true",
            payload={
                "username": "Ofertas de jogos",
                "allowed_mentions": {"parse": []},
                "embeds": [embed for _, embed in batch],
            },
        )

        for key, _ in batch:
            sent[key] = int(time.time())

        save_history(sent)
        time.sleep(1)

    print(
        f"{len(pending)} ofertas enviadas; "
        f"{len(errors)} fontes com erro.",
        flush=True,
    )

    if errors:
        raise RuntimeError(" | ".join(errors))


if __name__ == "__main__":
    main()
