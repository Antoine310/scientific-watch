"""Scientific watch v5: disambiguated topic screening and balanced ranking, preview by default.

Requires NOTION_TOKEN, NOTION_DATA_SOURCE_ID, NOTION_DATABASE_ID.
Optional OPENALEX_API_KEY. Set WRITE_TO_NOTION=true to save selected papers.
No AI claims or scores are fabricated.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

TOKEN = os.environ['NOTION_TOKEN']
SOURCE_ID = os.environ['NOTION_DATA_SOURCE_ID']
DATABASE_ID = os.environ['NOTION_DATABASE_ID']
API_KEY = os.environ.get('OPENALEX_API_KEY', '').strip()
WRITE = os.environ.get('WRITE_TO_NOTION', 'false').lower() == 'true'
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Notion-Version': '2025-09-03', 'Content-Type': 'application/json'}

# Each theme has multiple focused searches; the classification rules are stricter than search.
THEMES = [
 ('T01 — Root of Trust & Secure Boot', ['"hardware root of trust"', '"secure boot" RISC-V']),
 ('T02 — DICE & Hardware Identity', ['"device identifier composition engine"', '"DICE attestation"']),
 ('T03 — RISC-V TEE & Enclaves', ['"RISC-V" "enclave"', 'Keystone enclave']),
 ('T04 — Runtime Attestation', ['"runtime attestation"', '"control flow attestation"']),
 ('T05 — Enclave Performance & Crypto', ['"enclave startup" performance', '"cryptographic accelerator" attestation']),
 ('T06 — Microarchitectural Side Channels', ['"microarchitectural side channel"', '"cache timing attack" processor']),
 ('T07 — Hardware Security Verification', ['"hardware information flow tracking"', '"RTL security verification"']),
 ('T08 — Cache & Temporal Isolation', ['"cache partitioning" security', '"temporal isolation" microarchitecture']),
 ('T09 — Secure Memory', ['"memory integrity" enclave', '"secure scratchpad" processor']),
 ('T10 — RISC-V SoC & FPGA', ['OpenTitan RISC-V security', 'CVA6 security']),
 ('T11 — PUF & TRNG', ['"physical unclonable function" hardware identity', '"TRNG" hardware security']),
 ('T12 — Advanced Memory Architectures', ['"cache state restoration"', '"hybrid cache scratchpad"']),
]

def norm(s):
    return re.sub(r'\s+', ' ', (s or '').casefold().replace('–', '-').replace('—', '-')).strip()

def contains(text, *phrases):
    return any(p in text for p in phrases)

def abstract(index):
    if not index:
        return ''
    return ' '.join(word for pos, word in sorted((position, word) for word, positions in index.items() for position in positions))

def relevant(theme_id, title, abstract_text):
    """Conservative rules: paper title must signal the scientific topic.

    A true match isn't a proof of thesis relevance; abstracts are used as context.
    """
    t, a = norm(title), norm(abstract_text)
    both = t + ' ' + a
    # Only fault-injection articles are not a target; a mixed study of leakage is eligible.
    if contains(both, 'fault injection', 'voltage glitch', 'clock glitch', 'laser fault') and not contains(both, 'side channel', 'side-channel', 'timing leak', 'cache leak'):
        return False
    if theme_id == 'T01':
        return contains(t, 'root of trust', 'secure boot', 'measured boot', 'chain of trust') and contains(both, 'hardware', 'risc-v', 'firmware', 'soc', 'opentitan', 'secure element', 'processor')
    if theme_id == 'T02':
        return contains(t, 'device identifier composition engine', 'compound device identifier', 'dice attestation', 'layered attestation', 'hardware identity', 'device identity') and contains(both, 'attestation', 'cryptograph', 'firmware', 'hardware', 'risc-v', 'root of trust', 'secure boot')
    if theme_id == 'T03':
        # Keystone is ambiguous (e.g., ecology). Require an explicit computing-security context.
        if 'keystone' in t and not contains(both, 'risc-v', 'riscv', 'enclave', 'trusted execution', 'security monitor', 'computer security', 'webassembly', 'tee', 'pmp'):
            return False
        return (contains(t, 'enclave', 'trusted execution environment', 'security monitor', 'keystone tee', 'keystone enclave', 'penglai', 'sanctorum', 'risc-v tee') and contains(both, 'risc-v', 'riscv', 'enclave', 'trusted execution', 'sgx', 'keystone tee', 'keystone enclave', 'penglai', 'sanctorum', 'physical memory protection', 'pmp', 'security monitor'))
    if theme_id == 'T04':
        return contains(t, 'runtime attestation', 'run-time attestation', 'continuous attestation', 'control-flow attestation', 'control flow attestation', 'execution attestation') and contains(both, 'hardware', 'processor', 'embedded', 'firmware', 'tee', 'risc-v', 'enclave', 'control flow')
    if theme_id == 'T05':
        return ((contains(t, 'enclave', 'trusted execution') and contains(t, 'start', 'launch', 'creation', 'initializ', 'instantiat', 'accelerat', 'performance', 'overhead', 'measurement')) or (contains(t, 'cryptographic accelerator', 'crypto accelerator', 'sha-256 accelerator') and contains(both, 'secure boot', 'attestation', 'root of trust', 'enclave', 'risc-v')))
    if theme_id == 'T06':
        # Avoid physical/thermal/perception and LLM side channels unless they are about processor leakage.
        if contains(t, 'thermal side-channel', 'thermal side channel', 'power side-channel', 'power side channel', 'physical-layer', 'perception attack') and not contains(both, 'processor', 'microarchitect', 'cpu', 'cache', 'cryptograph', 'risc-v', 'enclave'):
            return False
        return (contains(t, 'microarchitectural side channel', 'microarchitectural side-channel', 'cache side channel', 'cache side-channel', 'cache timing', 'timing leak', 'prime+probe', 'flush+reload', 'spectre', 'transient execution', 'processor side-channel', 'processor side channel', 'side-channel analysis') and contains(both, 'microarchitect', 'cache', 'processor', 'cpu', 'branch predict', 'enclave', 'risc-v', 'riscv', 'hardware', 'tee', 'cryptograph', 'rtl'))
    if theme_id == 'T07':
        return contains(t, 'rtl', 'hardware', 'microarchitectur', 'information-flow', 'information flow', 'formal verification') and contains(t, 'security', 'noninterference', 'non-interference', 'fuzz', 'verification', 'leakage', 'flow tracking')
    if theme_id == 'T08':
        return contains(t, 'cache', 'tlb', 'microreset', 'temporal isolation', 'time protection') and contains(t, 'isolat', 'partition', 'security', 'secure', 'side channel', 'side-channel', 'context switch', 'temporal', 'microreset', 'locking')
    if theme_id == 'T09':
        return contains(t, 'memory encrypt', 'memory integrity', 'secure memory', 'secure scratchpad', 'memory authentication', 'authenticated memory', 'merkle tree', 'encrypted paging') and contains(both, 'processor', 'enclave', 'risc-v', 'tee', 'hardware', 'security', 'soc', 'trusted')
    if theme_id == 'T10':
        return contains(t, 'opentitan', 'cva6', 'caliptra', 'risc-v soc', 'riscv soc', 'cheshire', 'carfield') and contains(both, 'security', 'secure', 'root of trust', 'attestation', 'enclave', 'crypto', 'isolation')
    if theme_id == 'T11':
        return contains(t, 'physical unclonable function', 'sram puf', 'ring oscillator puf', 'trng', 'true random number generator', 'hardware identity') and contains(both, 'security', 'secret', 'key', 'entropy', 'hardware', 'root of trust', 'cryptograph')
    if theme_id == 'T12':
        return contains(t, 'cache restor', 'cache warm-up', 'cache partition', 'cache reconfigur', 'hybrid cache', 'scratchpad', 'working set prediction', 'context-aware prefetch') and contains(t, 'cache', 'memory', 'scratchpad', 'prefetch', 'context', 'working set')
    return False

def rank_record(record):
    paper = record['paper']
    title = norm(paper.get('title', ''))
    body = norm(abstract(paper.get('abstract_inverted_index')))
    score = 0
    strong = ('risc-v', 'riscv', 'opentitan', 'cva6', 'dice attestation', 'root of trust',
              'microarchitect', 'cache side-channel', 'cache side channel',
              'hardware information-flow', 'hardware information flow', 'keystone enclave',
              'keystone tee', 'secure boot', 'enclave isolation')
    score += 8 * sum(phrase in title for phrase in strong)
    score += 2 * sum(phrase in body for phrase in strong)
    score += 5 * len(record['themes'])
    # No abstract: keep, but rank below otherwise comparable candidates.
    if not body:
        score -= 2
    return score


def api(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = HEADERS if url.startswith('https://api.notion.com/') else {'User-Agent': 'scientific-watch/0.5 (research monitoring)'}
    request = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            message = exc.read().decode('utf-8', 'replace')[:800]
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f'HTTP {exc.code} for {url.split("?")[0]}: {message}') from exc

def existing_items():
    titles, dois = set(), set()
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        result = api(f'https://api.notion.com/v1/data_sources/{SOURCE_ID}/query', body)
        for row in result.get('results', []):
            p = row.get('properties', {})
            title = ''.join(part.get('plain_text', '') for part in p.get('Titre', {}).get('title', []))
            doi = ''.join(part.get('plain_text', '') for part in p.get('DOI', {}).get('rich_text', []))
            if title:
                titles.add(norm(title))
            if doi:
                dois.add(norm(doi))
        cursor = result.get('next_cursor')
        if not cursor:
            return titles, dois

def search(query):
    params = {'search': query, 'filter': 'from_publication_date:' + (date.today() - timedelta(days=90)).isoformat(), 'sort': 'publication_date:desc', 'per_page': 25}
    if API_KEY:
        params['api_key'] = API_KEY
    return api('https://api.openalex.org/works?' + urllib.parse.urlencode(params)).get('results', [])

def main():
    titles, dois = existing_items()
    candidates = {}
    for theme, queries in THEMES:
        theme_id = theme.split(' ')[0]
        count = 0
        for query in queries:
            for paper in search(query):
                title = (paper.get('title') or '').strip()
                doi = (paper.get('doi') or '').strip()
                if not title or norm(title) in titles or (doi and norm(doi) in dois):
                    continue
                if not relevant(theme_id, title, abstract(paper.get('abstract_inverted_index'))):
                    continue
                pid = doi.casefold() if doi else norm(title)
                if pid not in candidates:
                    candidates[pid] = {'paper': paper, 'themes': []}
                if theme not in candidates[pid]['themes']:
                    candidates[pid]['themes'].append(theme)
                count += 1
        print(f'{theme_id}: {count} correspondance(s) avant dédoublonnage')
        time.sleep(.3)
    print(f'\nCandidats uniques retenus : {len(candidates)}')
    # Rank globally, with a soft cap per theme, instead of taking early themes first.
    ranked = sorted(candidates.values(), key=rank_record, reverse=True)
    selected = []
    selected_counts = {}
    for item in ranked:
        primary = item['themes'][0].split(' ')[0]
        if selected_counts.get(primary, 0) >= 3:
            continue
        selected.append(item)
        selected_counts[primary] = selected_counts.get(primary, 0) + 1
        if len(selected) == 12:
            break
    print('Mode :', 'ECRITURE NOTION' if WRITE else 'APERÇU UNIQUEMENT — aucun article ajouté')
    for record in selected:
        paper, themes = record['paper'], record['themes']
        title = paper['title']
        print(' -', title[:130], '|', ', '.join(t[:3] for t in themes), '| score indicatif', rank_record(record))
        if not WRITE:
            continue
        doi = paper.get('doi') or ''
        authors = ', '.join(a.get('author', {}).get('display_name', '') for a in paper.get('authorships', [])[:8])
        landing = doi or (paper.get('primary_location') or {}).get('landing_page_url') or paper.get('id')
        props = {
            'Titre': {'title': [{'text': {'content': title[:1900]}}]},
            'Auteurs': {'rich_text': [{'text': {'content': authors[:1900]}}]},
            'Thèmes': {'multi_select': [{'name': t} for t in themes]},
            'Source': {'multi_select': [{'name': 'OpenAlex'}]},
            'Résumé court': {'rich_text': [{'text': {'content': 'Analyse IA non effectuée.'}}]},
        }
        if doi:
            props['DOI'] = {'rich_text': [{'text': {'content': doi[:1900]}}]}
        if landing:
            props['Lien article'] = {'url': landing}
        if paper.get('publication_date'):
            props['Date de publication'] = {'date': {'start': paper['publication_date']}}
        api('https://api.notion.com/v1/pages', {'parent': {'database_id': DATABASE_ID}, 'properties': props})
        print('   ✓ Ajouté')
    print('\nTerminé :', len(selected), 'proposition(s) ;', len(selected) if WRITE else 0, 'ajout(s)')

if __name__ == '__main__':
    main()
