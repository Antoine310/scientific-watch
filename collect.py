"""Small, manually triggered OpenAlex -> Notion ingestion trial."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

NOTION_URL = 'https://api.notion.com/v1/'
SOURCE_ID = os.environ['NOTION_DATA_SOURCE_ID']
TOKEN = os.environ['NOTION_TOKEN']
KEY = os.environ.get('OPENALEX_API_KEY', '')
NOTION_HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2025-09-03',
    'Content-Type': 'application/json',
}

SEARCHES = [
    ('T01 — Root of Trust & Secure Boot', 'hardware root of trust RISC-V secure boot'),
    ('T02 — DICE & Hardware Identity', 'device identifier composition engine hardware attestation'),
    ('T03 — RISC-V TEE & Enclaves', 'RISC-V trusted execution environment Keystone'),
]


def request_json(url, payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method='POST' if body is not None else 'GET')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:600]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt * 3)
                continue
            raise RuntimeError(f'HTTP {e.code} pour {url.split("?")[0]}: {detail}') from e


def notion(path, payload):
    return request_json(NOTION_URL + path, payload, NOTION_HEADERS)


def notion_existing_titles():
    titles = set()
    cursor = None
    for _ in range(10):
        payload = {'page_size': 100}
        if cursor:
            payload['start_cursor'] = cursor
        page = notion(f'data_sources/{SOURCE_ID}/query', payload)
        for item in page.get('results', []):
            values = item.get('properties', {}).get('Titre', {}).get('title', [])
            title = ''.join(x.get('plain_text', '') for x in values).strip()
            if title:
                titles.add(title.casefold())
        cursor = page.get('next_cursor')
        if not cursor:
            break
    return titles


def openalex_search(term):
    params = {
        'search': term,
        'filter': f'from_publication_date:{(date.today() - timedelta(days=90)).isoformat()}',
        'sort': '-publication_date',
        'per_page': '10',
    }
    if KEY:
        params['api_key'] = KEY
    url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
    return request_json(url).get('results', [])


def main():
    existing = notion_existing_titles()
    added = 0
    seen = set()
    for theme, term in SEARCHES:
        print(f'Recherche : {theme}', flush=True)
        for item in openalex_search(term):
            title = (item.get('title') or '').strip()
            if not title or title.casefold() in existing or title.casefold() in seen:
                continue
            seen.add(title.casefold())
            # First trial: light semantic guard, manual quality check in Notion afterwards.
            text = title.lower()
            if not any(s in text for s in ('risc-v', 'riscv', 'trust', 'enclave', 'attestation', 'secure boot', 'dice')):
                continue
            if 'fault injection' in text or 'voltage glitch' in text:
                continue
            authors = ', '.join(x.get('author', {}).get('display_name', '') for x in item.get('authorships', [])[:8])
            doi = item.get('doi') or ''
            link = doi or item.get('id') or ''
            publication_date = item.get('publication_date')
            props = {
                'Titre': {'title': [{'text': {'content': title[:1900]}}]},
                'Auteurs': {'rich_text': [{'text': {'content': authors[:1900]}}]},
                'Thèmes': {'multi_select': [{'name': theme}]},
                'Résumé court': {'rich_text': [{'text': {'content': 'Résumé IA non généré — première collecte de test.'}}]},
                'Source': {'multi_select': [{'name': 'OpenAlex'}]},
            }
            if link:
                props['Lien article'] = {'url': link}
            if doi:
                props['DOI'] = {'rich_text': [{'text': {'content': doi[:1900]}}]}
            if publication_date:
                props['Date de publication'] = {'date': {'start': publication_date}}
            result = notion('pages', {
                'parent': {'database_id': os.environ['NOTION_DATABASE_ID']},
                'properties': props,
            })
            added += 1
            existing.add(title.casefold())
            print(f'  Ajouté : {title[:100]} — {result.get("url", "")}', flush=True)
            if added >= 10:
                print('Limite de 10 articles atteinte.', flush=True)
                return
        time.sleep(1)
    print(f'Terminé : {added} article(s) ajouté(s).', flush=True)


if __name__ == '__main__':
    main()
