"""Conservative OpenAlex -> Notion trial for hardware RoT & side channels.

Manually triggered only. Use article titles and abstracts to avoid generic keyword hits.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

SOURCE_ID = os.environ['NOTION_DATA_SOURCE_ID']
TOKEN = os.environ['NOTION_TOKEN']
DATABASE_ID = os.environ['NOTION_DATABASE_ID']
KEY = os.environ.get('OPENALEX_API_KEY', '').strip()
NOTION_HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2025-09-03',
    'Content-Type': 'application/json',
}

# Search OpenAlex broadly enough to find candidates, then validate locally.
SEARCHES = [
    ('T01 — Root of Trust & Secure Boot', '"hardware root of trust"'),
    ('T02 — DICE & Hardware Identity', '"device identifier composition engine"'),
    ('T03 — RISC-V TEE & Enclaves', '"RISC-V" "enclave"'),
    ('T04 — Runtime Attestation', '"runtime attestation" processor'),
    ('T05 — Enclave Performance & Crypto', '"enclave startup" performance'),
    ('T06 — Microarchitectural Side Channels', '"microarchitectural side channel"'),
    ('T07 — Hardware Security Verification', '"RTL security verification"'),
    ('T08 — Cache & Temporal Isolation', '"cache partitioning" security'),
    ('T09 — Secure Memory', '"memory integrity" enclave'),
    ('T10 — RISC-V SoC & FPGA', '"OpenTitan" "RISC-V"'),
    ('T11 — PUF & TRNG', '"physical unclonable function" hardware security'),
    ('T12 — Advanced Memory Architectures', '"cache state restoration"'),
]

# Strong signals in the article's title or abstract.
KEY_PHRASES = {
    'root of trust', 'trusted execution environment', 'trusted execution environments',
    'secure enclave', 'secure enclaves', 'keystone enclave', 'secure boot', 'measured boot',
    'runtime attestation', 'continuous attestation', 'remote attestation',
    'control-flow attestation', 'microarchitectural side channel',
    'microarchitectural side-channel', 'cache side channel', 'cache side-channel',
    'temporal isolation', 'cache partitioning', 'cache isolation',
    'secure memory', 'memory integrity', 'memory encryption',
    'device identifier composition engine', 'physical unclonable function',
    'security monitor', 'hardware security verification', 'rtl security',
}
SPECIAL_NAMES = {'opentitan', 'keystone', 'cva6', 'ariane core', 'sanctorum', 'penglai', 'dice attestation'}
CONTEXT = {'risc-v', 'riscv', 'processor', 'microarchitecture', 'hardware', 'firmware',
           'enclave', 'cryptograph', 'security', 'attestation', 'soc', 'embedded',
           'rtl', 'fpga', 'isolation', 'trusted execution', 'cache', 'memory'}
EXCLUDE = {'fault injection', 'voltage glitch', 'clock glitch', 'laser fault',
           'electromagnetic fault injection'}
MAX_ARTICLES = 12
MAX_PER_THEME = 2


def request_json(url, payload=None, headers=None):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method='POST' if data is not None else 'GET')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:500]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (2 ** attempt))
                continue
            raise RuntimeError(f'HTTP {e.code} : {detail}') from e


def notion(path, payload):
    return request_json('https://api.notion.com/v1/' + path, payload, NOTION_HEADERS)


def get_existing():
    titles, dois = set(), set()
    cursor = None
    while True:
        payload = {'page_size': 100}
        if cursor:
            payload['start_cursor'] = cursor
        page = notion(f'data_sources/{SOURCE_ID}/query', payload)
        for item in page.get('results', []):
            props = item.get('properties', {})
            title = ''.join(t.get('plain_text', '') for t in props.get('Titre', {}).get('title', [])).strip().casefold()
            doi = ''.join(t.get('plain_text', '') for t in props.get('DOI', {}).get('rich_text', [])).strip().casefold()
            if title:
                titles.add(title)
            if doi:
                dois.add(doi)
        cursor = page.get('next_cursor')
        if not cursor:
            break
    return titles, dois


def abstract_from_index(index):
    if not index:
        return ''
    positions = [(pos, word) for word, locs in index.items() for pos in locs]
    return ' '.join(word for _, word in sorted(positions))


def normalise(text):
    return re.sub(r'\s+', ' ', text.casefold().replace('–', '-').replace('—', '-')).strip()


def relevant(item):
    title = normalise(item.get('title') or '')
    abstract = normalise(abstract_from_index(item.get('abstract_inverted_index')))
    combined = title + ' ' + abstract
    has_specific = any(p in combined for p in KEY_PHRASES | SPECIAL_NAMES)
    has_context = any(p in combined for p in CONTEXT)
    # Explicitly reject papers ONLY about fault injection; retain mixed papers about side channels.
    side_channel = ('side channel' in combined or 'side-channel' in combined
                    or 'timing leakage' in combined or 'cache leakage' in combined)
    if any(p in combined for p in EXCLUDE) and not side_channel:
        return False
    # At minimum an exact security/hardware domain phrase is required.
    return has_specific and has_context


def openalex_search(query):
    params = {
        'search': query,
        'filter': f'from_publication_date:{(date.today() - timedelta(days=90)).isoformat()}',
        'sort': 'publication_date:desc',
        'per_page': 25,
    }
    if KEY:
        params['api_key'] = KEY
    return request_json('https://api.openalex.org/works?' + urllib.parse.urlencode(params)).get('results', [])


def main():
    existing_titles, existing_dois = get_existing()
    seen = set()
    added = 0
    for theme, query in SEARCHES:
        count = 0
        print(f'\nRecherche {theme}', flush=True)
        for item in openalex_search(query):
            title = (item.get('title') or '').strip()
            doi = (item.get('doi') or '').strip()
            if not title or title.casefold() in existing_titles or title.casefold() in seen:
                continue
            if doi and doi.casefold() in existing_dois:
                continue
            if not relevant(item):
                print(f'  Ignoré (hors sujet probable) : {title[:90]}', flush=True)
                continue
            seen.add(title.casefold())
            authors = ', '.join(a.get('author', {}).get('display_name', '') for a in item.get('authorships', [])[:8])
            landing = doi or (item.get('primary_location') or {}).get('landing_page_url') or item.get('id')
            props = {
                'Titre': {'title': [{'text': {'content': title[:1900]}}]},
                'Auteurs': {'rich_text': [{'text': {'content': authors[:1900]}}]},
                'Thèmes': {'multi_select': [{'name': theme}]},
                'Résumé court': {'rich_text': [{'text': {'content': 'Résumé IA non généré — à analyser.'}}]},
                'Source': {'multi_select': [{'name': 'OpenAlex'}]},
            }
            if landing:
                props['Lien article'] = {'url': landing}
            if doi:
                props['DOI'] = {'rich_text': [{'text': {'content': doi[:1900]}}]}
            if item.get('publication_date'):
                props['Date de publication'] = {'date': {'start': item['publication_date']}}
            notion('pages', {'parent': {'database_id': DATABASE_ID}, 'properties': props})
            existing_titles.add(title.casefold())
            if doi:
                existing_dois.add(doi.casefold())
            added += 1
            count += 1
            print(f'  ✅ Ajouté : {title[:110]}', flush=True)
            if added >= MAX_ARTICLES:
                print('Limite globale atteinte.', flush=True)
                return
            if count >= MAX_PER_THEME:
                break
        time.sleep(1)
    print(f'\nTerminé : {added} article(s) ajouté(s).', flush=True)


if __name__ == '__main__':
    main()
