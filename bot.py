"""Jogos grátis e descontos de 30% ou mais para Discord."""

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


STATE = Path("data/enviados.json")


def add_image(embed, image_url):
    if not isinstance(image_url, str):
        return

    image_url = image_url.strip()
    parsed = urlparse(image_url)

    if parsed.scheme in ("https", "http") and parsed.hostname:
        # Imagem grande abaixo do texto.
        embed["image"] = {"url": image_url}


def request(url, payload=None):
    body = (
        json.dumps(payload).encode("utf-8")
        if payload is not None
        else None
    )

    last_error = "Erro desconhecido"

    for attempt in range(5):
        try:
            req = Request(
                url,
                data=body,
                headers={
                    "User-Agent": "GameDealsNotifier/1.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

            with urlopen(req, timeout=40) as response:
                raw = response.read()

                if not raw:
                    return None

                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise RuntimeError(
                        "O serviço respondeu com conteúdo "
                        "que não é JSON."
                    ) from None

        except HTTPError as exc:
            # Não mostra a URL para proteger o webhook.
            last_error = f"HTTP {exc.code}"

            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(
                    f"{last_error}: o serviço recusou a consulta."
                ) from None

            try:
                delay = float(
                    exc.headers.get("Retry-After", 2 ** attempt)
                )
            except (TypeError, ValueError):
                delay = 2 ** attempt

        except (URLError, TimeoutError):
            last_error = "Falha de conexão ou tempo limite"
            delay = 2 ** attempt

        if attempt < 4:
            time.sleep(min(max(delay, 1), 60))

    raise RuntimeError(
        f"{last_error}, após cinco tentativas."
    )


def discounts():
    def fetch(endpoint, params=None):
        url = (
            "https://www.cheapshark.com/api/1.0/"
            + endpoint
        )

        if params:
            url += "?" + urlencode(params)

        page_label = (
            f", página {params['pageNumber']}"
            if params and "pageNumber" in params
            else ""
        )

        for attempt in range(5):
            req = Request(
                url,
                headers={
                    "User-Agent": "GameDealsNotifier/1.0",
                    "Accept": "application/json",
                },
            )

            try:
                with urlopen(req, timeout=40) as response:
                    data = json.loads(response.read())
                    total = response.headers.get(
                        "X-Total-Page-Count"
                    )

                    return (
                        data,
                        int(total) if total is not None else None,
                    )

            except HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    if attempt < 4:
                        try:
                            delay = float(
                                exc.headers.get(
                                    "Retry-After",
                                    2 ** (attempt + 1),
                                )
                            )
                        except (TypeError, ValueError):
                            delay = 2 ** (attempt + 1)

                        time.sleep(min(max(delay, 1), 60))
                        continue

                detail = exc.read(600).decode(
                    "utf-8",
                    errors="replace",
                )

                raise RuntimeError(
                    f"CheapShark /{endpoint}{page_label}: "
                    f"HTTP {exc.code}. Resposta: {detail}"
                ) from None

            except (URLError, TimeoutError):
                if attempt < 4:
                    time.sleep(2 ** (attempt + 1))
                    continue

                raise RuntimeError(
                    f"CheapShark /{endpoint}{page_label}: "
                    "falha de conexão após cinco tentativas."
                ) from None

    store_rows, _ = fetch("stores")

    if not isinstance(store_rows, list):
        raise RuntimeError(
            "CheapShark retornou uma lista de lojas inesperada."
        )

    stores = {
        str(store["storeID"]): store["storeName"]
        for store in store_rows
    }

    result = []
    page = 0

    while True:
        rows, total_pages = fetch(
            "deals",
            {
                "pageNumber": page,
                "pageSize": 60,
                "sortBy": "Savings",
                "onSale": 1,
            },
        )

        if not isinstance(rows, list):
            raise RuntimeError(
                f"Resposta de ofertas inesperada "
                f"na página {page}."
            )

        print(
            f"CheapShark: página {page}, "
            f"{len(rows)} ofertas; "
            f"total de páginas informado: {total_pages}.",
            flush=True,
        )

        for row in rows:
            saving = Decimal(str(row["savings"]))

            if saving < 30:
                continue

            price = Decimal(str(row["salePrice"]))
            store = stores.get(
                str(row["storeID"]),
                "Loja",
            )

            link = (
                "https://www.cheapshark.com/redirect?"
                + urlencode({"dealID": row["dealID"]})
            )

            if price == 0:
                label = "GRÁTIS"
            else:
                label = (
                    f"US$ {price:.2f} · "
                    f"{saving:.1f}% de desconto"
                )

            # Preserva o histórico para não repetir ofertas.
            key = (
                f"cs:{row['gameID']}:"
                f"{row['storeID']}:{price}"
            )

            embed = {
                "title": str(row["title"])[:200],
                "url": link,
                "description": (
                    f"{label}\n"
                    f"Loja: {store}\n"
                    "Preço de referência em dólares. "
                    "Confira o valor e a disponibilidade "
                    "no Brasil."
                ),
                "color": (
                    0x2ECC71 if price == 0 else 0x3498DB
                ),
                "footer": {"text": "Fonte: CheapShark"},
            }

            add_image(embed, row.get("thumb"))
            result.append((key, embed))

        if not rows or len(rows) < 60:
            break

        if (
            total_pages is not None
            and page + 1 >= total_pages
        ):
            break

        if any(
            Decimal(str(row["savings"])) < 30
            for row in rows
        ):
            break

        page += 1
        time.sleep(1)

    return result


def free_games():
    rows = request(
        "https://www.gamerpower.com/api/giveaways"
        "?platform=pc&type=game&sort-by=date"
    )

    if isinstance(rows, dict):
        if str(rows.get("status")) in {"0", "201"}:
            return []

        raise RuntimeError(
            "GamerPower retornou uma resposta inesperada."
        )

    if not isinstance(rows, list):
        raise RuntimeError(
            "GamerPower não retornou uma lista de jogos."
        )

    result = []

    for row in rows:
        key = f"gp:{row['id']}"

        embed = {
            "title": str(row["title"])[:200],
            "url": row["open_giveaway_url"],
            "description": (
                f"GRÁTIS · {row.get('platforms', 'PC')}\n"
                f"Prazo: "
                f"{row.get('end_date', 'Confira no link')}\n"
                "Confira região, estoque "
                "e instruções de resgate.\n"
                "[Fonte: GamerPower]"
                "(https://www.gamerpower.com/)"
            ),
            "color": 0x2ECC71,
        }

        # Prioriza a imagem completa quando disponível.
        add_image(
            embed,
            row.get("image") or row.get("thumbnail"),
        )

        result.append((key, embed))

    return result


def save_history(sent):
    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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
    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL",
        "",
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

    if STATE.exists():
        sent = json.loads(
            STATE.read_text(encoding="utf-8")
        )

        if not isinstance(sent, dict):
            raise RuntimeError(
                "O histórico de envios está inválido."
            )
    else:
        sent = {}

    offers = []
    errors = []

    sources = [
        ("GamerPower — jogos grátis", free_games),
        ("CheapShark — descontos", discounts),
    ]

    for name, source in sources:
        try:
            found = source()
            offers.extend(found)

            print(
                f"{name}: {len(found)} ofertas encontradas.",
                flush=True,
            )

        except Exception as exc:
            detail = (
                f"{name}: "
                f"{type(exc).__name__}: {exc}"
            )

            errors.append(detail)

            print(
                f"ERRO — {detail}",
                flush=True,
            )

    # Até 100 ofertas por execução.
    pending = list({
        key: embed
        for key, embed in offers
        if key not in sent
    }.items())[:100]

    for start in range(0, len(pending), 10):
        batch = pending[start:start + 10]
        separator = "&" if "?" in webhook else "?"

        request(
            webhook + separator + "wait=true",
            {
                "username": "Ofertas de jogos",
                "allowed_mentions": {"parse": []},
                "embeds": [
                    embed
                    for _, embed in batch
                ],
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
