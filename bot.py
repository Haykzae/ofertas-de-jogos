"""Alertas de jogos para Discord. Python 3.12+, sem dependências externas."""
import json
import os
from pathlib import Path
import time
from decimal import Decimal
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

STATE = Path('data/enviados.json')


def request(url, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(5):
        try:
            req = Request(url, data=body, headers={
                'User-Agent': 'GameDealsNotifier/1.0',
                'Content-Type': 'application/json'})
            with urlopen(req, timeout=40) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(f'HTTP {exc.code}; confira a configuração.') from None
            delay = min(float(exc.headers.get('Retry-After', 2 ** attempt)), 60)
        except (URLError, TimeoutError):
            delay = 2 ** attempt
        time.sleep(delay)
    raise RuntimeError('Serviço indisponível após cinco tentativas.')


def discounts():
    stores = {str(s['storeID']): s['storeName'] for s in
              request('https://www.cheapshark.com/api/1.0/stores')}
    result = []
    page = 0
    while True:
        query = urlencode(dict(pageNumber=page, pageSize=60,
                               sortBy='Savings', onSale=1))
        rows = request('https://www.cheapshark.com/api/1.0/deals?' + query)
        if not isinstance(rows, list):
            raise RuntimeError('Resposta inesperada do CheapShark.')
        for row in rows:
            saving = Decimal(row['savings'])
            if saving < 30:
                continue
            price = Decimal(row['salePrice'])
            title = str(row['title'])[:200]
            store = stores.get(str(row['storeID']), 'Loja')
            link = 'https://www.cheapshark.com/redirect?' + urlencode({'dealID': row['dealID']})
            label = 'GRÁTIS' if price == 0 else f'US$ {price:.2f} · {saving:.0f}% de desconto'
            # Identifica oferta/preço, para permitir um novo aviso se o preço mudar.
            key = f"cs:{row['gameID']}:{row['storeID']}:{price}"
            result.append((key, {'title': title, 'url': link,
                'description': f'{label}\nLoja: {store}\nPreço de referência em dólares; confira região e valor na loja.',
                'color': 0x2ECC71 if price == 0 else 0x3498DB,
                'footer': {'text': 'Fonte: CheapShark'}}))
        if len(rows) < 60 or any(Decimal(r['savings']) < 30 for r in rows):
            break
        page += 1
        time.sleep(0.3)
    return result


def free_games():
    rows = request('https://www.gamerpower.com/api/giveaways?platform=pc&type=game&sort-by=date')
    if isinstance(rows, dict) and str(rows.get('status')) == '0':
        return []
    if not isinstance(rows, list):
        raise RuntimeError('Resposta inesperada do GamerPower.')
    return [(f"gp:{r['id']}", {
        'title': str(r['title'])[:200], 'url': r['open_giveaway_url'],
        'description': f"GRÁTIS · {r.get('platforms', 'PC')}\nPrazo: {r.get('end_date', 'Confira no link')}\nConfira região, estoque e instruções para resgate.\n[Fonte: GamerPower](https://www.gamerpower.com/)",
        'color': 0x2ECC71}) for r in rows]


def main():
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    parsed = urlparse(webhook)
    if parsed.scheme != 'https' or parsed.hostname != 'discord.com' or not parsed.path.startswith('/api/webhooks/'):
        raise RuntimeError('Configure o segredo DISCORD_WEBHOOK_URL com o webhook do Discord.')
    sent = json.loads(STATE.read_text('utf-8')) if STATE.exists() else {}
    errors = []
    offers = []
    for source in (free_games, discounts):
        try:
            offers.extend(source())
        except Exception as exc:
            errors.append(f'{source.__name__}: {type(exc).__name__}')
    # Limita o volume por rodada; os demais ficam para a próxima hora.
    pending = list({key: embed for key, embed in offers if key not in sent}.items())[:100]
    for start in range(0, len(pending), 10):
        batch = pending[start:start + 10]
        separator = '&' if '?' in webhook else '?'
        request(webhook + separator + 'wait=true', {
            'username': 'Ofertas de jogos', 'allowed_mentions': {'parse': []},
            'embeds': [embed for _, embed in batch]})
        for key, _ in batch:
            sent[key] = int(time.time())
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(sent, ensure_ascii=False, indent=2), 'utf-8')
        temp.replace(STATE)
        time.sleep(1)
    print(f'{len(pending)} ofertas enviadas; {len(errors)} fontes com erro.')
    if errors:
        raise RuntimeError('; '.join(errors))


if __name__ == '__main__':
    main()
