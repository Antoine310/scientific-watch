
"""Rattrapage bibliographique multibase (2026-10 -> 2016-01).

Sources : arXiv et Crossref par mois ; DBLP (recherche globale) et IACR
(ePrint OAI-PMH, métadonnées) en phases complémentaires.

ATTENTION : les limites d'API et les requêtes thématiques empêchent de promettre
une exhaustivité. Les requêtes et leur ordre sont stables pour le curseur.
Aucun PDF téléchargé, aucun jour de publication inventé.
"""
import calendar
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import cache_v16 as cachelib
import collect as core
import publish_history

STATE_PATH = pathlib.Path('data/history_multisource_progress.json')
RECORDS_PATH = pathlib.Path('data/history_records.json')
SECONDARY_PATH = pathlib.Path('data/history_secondary_records.json')

START_MONTH = '2026-10'  # Commencer par compléter les mois déjà explorés par OpenAlex.
END_MONTH = '2016-01'
PAGE_SIZE = 40
MAX_PAGES = max(1, min(40, int(os.environ.get('MULTI_MAX_PAGES', '8'))))
MAX_AI = max(1, min(50, int(os.environ.get('MULTI_MAX_AI', '8'))))
MAX_QUERY_PAGES = max(1, min(100, int(os.environ.get('MULTI_MAX_QUERY_PAGES', '12'))))
APPLY = os.environ.get('MULTI_APPLY', 'false').lower() == 'true'
USE_DBLP = os.environ.get('MULTI_DBLP_ENABLED', 'true').lower() == 'true'
USE_IACR = os.environ.get('MULTI_IACR_ENABLED', 'true').lower() == 'true'

# Phrases générales ET ciblées : ni filtrage exclusivement par survey, ni
# obligation de trouver le mot-clef dans le titre.
ARXIV_QUERIES = [
    ('T01', 'all:"hardware security"'),
    ('T01', 'all:"root of trust"'),
    ('T01', 'all:"secure boot"'),
    ('T03', 'all:"RISC-V" AND all:security'),
    ('T03', 'all:"trusted execution environment"'),
    ('T03', 'all:enclave AND all:security'),
    ('T04', 'all:"runtime attestation"'),
    ('T06', 'all:"side channel" AND all:cache'),
    ('T06', 'all:microarchitectural AND all:security'),
    ('T06', 'all:"speculative execution" AND all:security'),
    ('T08', 'all:"hardware isolation"'),
    ('T09', 'all:"memory integrity" AND all:security'),
    ('T10', 'all:OpenTitan OR all:CVA6'),
]
CROSSREF_QUERIES = [
    ('T01', 'hardware security'),
    ('T01', 'root of trust secure boot'),
    ('T03', 'RISC-V security enclave'),
    ('T03', 'trusted execution environment security'),
    ('T04', 'runtime attestation hardware'),
    ('T06', 'microarchitectural side channel security'),
    ('T06', 'cache side channel attack'),
    ('T06', 'spectre transient execution security'),
    ('T08', 'temporal isolation processor'),
    ('T09', 'secure memory encryption integrity'),
    ('T10', 'OpenTitan CVA6 RISC-V hardware'),
    ('T07', 'hardware security verification'),
]
DBLP_QUERIES = [
    ('T01', 'hardware root of trust'),
    ('T03', 'RISC-V enclave'),
    ('T03', 'trusted execution environment'),
    ('T04', 'runtime attestation'),
    ('T06', 'microarchitectural side channel'),
    ('T06', 'cache side channel'),
    ('T06', 'spectre transient execution'),
    ('T10', 'OpenTitan'),
    ('T03', 'Sanctum isolation'),
]
MONTHLY_TASKS = [('arxiv', theme, query) for theme, query in ARXIV_QUERIES] + [
    ('crossref', theme, query) for theme, query in CROSSREF_QUERIES]

ARXIV_NS = {'a': 'http://www.w3.org/2005/Atom',
            'x': 'http://arxiv.org/schemas/atom',
            'o': 'http://a9.com/-/spec/opensearch/1.1/'}
OAI_NS = {'o': 'http://www.openarchives.org/OAI/2.0/',
          'dc': 'http://purl.org/dc/elements/1.1/'}
LAST_REQUEST = {}
# Les HTTP 429 d'arXiv peuvent être liés à la charge du serveur, même en
# respectant les 3 secondes recommandées. La pause est persistée entre jobs.
ARXIV_MIN_INTERVAL = max(3.1, float(os.environ.get('MULTI_ARXIV_INTERVAL_SECONDS', '10')))
ARXIV_COOLDOWN = max(1800, int(os.environ.get('MULTI_ARXIV_COOLDOWN_SECONDS', '10800')))


class SourceRateLimited(RuntimeError):
    """Réessayer dans un prochain lancement, sans avancer le curseur."""

    def __init__(self, source, retry_seconds):
        self.source = source
        self.retry_seconds = max(60, int(retry_seconds))
        super().__init__(f'{source} HTTP 429 : reprise possible dans {self.retry_seconds} s')


def retry_after_seconds(error):
    """Interprète Retry-After (secondes ou date HTTP), sans raccourcir l'attente."""
    value = error.headers.get('Retry-After') if error.headers else None
    if not value:
        return 0
    try:
        return max(0, int(float(value)))
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0, int((target - datetime.now(timezone.utc)).total_seconds()) + 1)
        except (TypeError, ValueError, OverflowError):
            return 0


def load_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save_json(path, value):
    path.parent.mkdir(exist_ok=True, parents=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def new_state():
    return {'version': 1, 'phase': 'monthly', 'month': START_MONTH,
            'query_index': 0, 'offset': 0, 'cursor': '*', 'pending': [],
            'pages_in_query': 0, 'pages_done': 0, 'evaluated': 0,
            'accepted': 0, 'secondary': 0, 'skipped': 0,
            'incomplete_queries': [], 'needs_publish': False, 'completed': False}


def state_read():
    state = load_json(STATE_PATH, new_state())
    if state.get('version') != 1:
        raise RuntimeError('Version inconnue de history_multisource_progress.json.')
    return state


def previous_month(ym):
    year, month = map(int, ym.split('-'))
    return f'{year - (month == 1):04d}-{12 if month == 1 else month - 1:02d}'


def month_bounds(ym):
    year, month = map(int, ym.split('-'))
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}'


def request(url, source, *, xml=False):
    # arXiv recommande >= 3 s entre appels. 10 s ici par prudence.
    delay = {'arxiv': ARXIV_MIN_INTERVAL, 'dblp': 2.6,
             'crossref': 1.0, 'iacr': 2.0}.get(source, 1.0)
    headers = {'User-Agent': 'scientific-watch/1.7 (academic literature; metadata only)',
               'Accept': 'application/xml' if xml else 'application/json'}
    last_exc = None
    # Sur arXiv, 2 tentatives maximum : éviter de surcharger davantage le serveur.
    max_attempts = 2 if source == 'arxiv' else 3
    for attempt in range(max_attempts):
        elapsed = time.monotonic() - LAST_REQUEST.get(source, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        LAST_REQUEST[source] = time.monotonic()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=55
            ) as response:
                raw = response.read()
            return ET.fromstring(raw) if xml else json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
            last_exc = exc
            if isinstance(exc, urllib.error.HTTPError):
                if source == 'arxiv' and exc.code == 429:
                    header_wait = retry_after_seconds(exc)
                    if header_wait > 300:
                        # Ne pas bloquer le job entier pour un long Retry-After.
                        raise SourceRateLimited('arxiv', max(ARXIV_COOLDOWN, header_wait)) from exc
                    if attempt + 1 < max_attempts:
                        pause = max(60, header_wait)
                        print(f'arXiv HTTP 429 : nouvelle tentative dans {pause} s.', flush=True)
                        time.sleep(pause)
                        continue
                    # Le serveur peut limiter durablement les requêtes, parfois
                    # sans Retry-After : on conserve l'état et on reviendra plus tard.
                    raise SourceRateLimited('arxiv', max(ARXIV_COOLDOWN, header_wait)) from exc
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            if attempt + 1 < max_attempts:
                time.sleep((attempt + 1) * 12)
    raise RuntimeError(f'{source} inaccessible : {type(last_exc).__name__} — {str(last_exc)[:180]}') from last_exc


def canonical_title(title):
    return cachelib.key({'title': title})


def relevant(paper):
    """Préfiltre permissif sur TITRE ET ABSTRACT, pas un classement final."""
    t = core.norm(paper.get('title', ''))
    a = core.norm(core.abstract(paper.get('abstract_inverted_index')))
    both = t + ' ' + a
    if len(t) < 7:
        return False
    direct = (
        'root of trust', 'secure boot', 'measured boot', 'trusted boot',
        'risc-v', 'riscv', 'opentitan', 'cva6', 'keystone', 'sanctum',
        'hardware attestation', 'runtime attestation', 'control-flow attestation',
        'trusted execution', 'enclave', 'security monitor',
        'microarchitectur', 'side channel', 'side-channel', 'spectre', 'meltdown',
        'prime+probe', 'flush+reload', 'cache timing', 'memory integrity',
        'hardware security', 'processor security', 'hardware isolation',
        'secure processor', 'physical unclonable function',
    )
    if any(term in both for term in direct):
        return True
    # Association de concepts (avec abstracts) plutôt qu'un titre imposé.
    return (any(x in both for x in ('hardware', 'processor', 'cpu', 'microarchitecture', 'firmware', 'soc'))
            and any(x in both for x in ('security', 'attack', 'isolation', 'trust', 'attestation', 'leakage', 'side channel')))


def arxiv_page(ym, query, offset):
    start, end = month_bounds(ym)
    startstamp = start.replace('-', '') + '0000'
    endstamp = end.replace('-', '') + '2359'
    search = f'({query}) AND submittedDate:[{startstamp} TO {endstamp}]'
    params = urllib.parse.urlencode({'search_query': search, 'start': offset,
                                     'max_results': PAGE_SIZE, 'sortBy': 'submittedDate',
                                     'sortOrder': 'ascending'})
    root = request('https://export.arxiv.org/api/query?' + params, 'arxiv', xml=True)
    total_text = root.findtext('o:totalResults', default='', namespaces=ARXIV_NS)
    total = int(total_text) if total_text.isdigit() else None
    papers = []
    for entry in root.findall('a:entry', ARXIV_NS):
        title = ' '.join(entry.findtext('a:title', default='', namespaces=ARXIV_NS).split())
        summary = ' '.join(entry.findtext('a:summary', default='', namespaces=ARXIV_NS).split())
        url = entry.findtext('a:id', default='', namespaces=ARXIV_NS).strip()
        when = entry.findtext('a:published', default='', namespaces=ARXIV_NS)[:10]
        if not title or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', when):
            continue
        if not when.startswith(ym):
            continue
        doi = entry.findtext('x:doi', default='', namespaces=ARXIV_NS).strip()
        papers.append({'title': title, 'id': url, 'doi': 'https://doi.org/' + doi if doi else '',
                       'publication_date': when, 'abstract_inverted_index': core.indexed_abstract(summary),
                       'primary_location': {'landing_page_url': url},
                       'authorships': [{'author': {'display_name': x.findtext('a:name', default='', namespaces=ARXIV_NS)}}
                                       for x in entry.findall('a:author', ARXIV_NS)]})
    # Le dernier lot peut avoir des entrées hors mois / mal formées : se fonder sur
    # le nombre brut d'entrées pour avancer la pagination, pas sur len(papers).
    raw_count = len(root.findall('a:entry', ARXIV_NS))
    finished = ((total is not None and offset + raw_count >= total)
                or raw_count < PAGE_SIZE)
    return papers, raw_count, finished, ''


def precise_date(item):
    """Ne jamais transformer '2025' ou '2025-01' en '2025-01-01'."""
    for field in ('published', 'published-online', 'published-print', 'issued'):
        raw = (item.get(field) or {}).get('date-parts') or []
        if raw and len(raw[0]) >= 3:
            try:
                result = date(*map(int, raw[0][:3])).isoformat()
            except (TypeError, ValueError):
                continue
            return result
    return ''


def crossref_convert(item):
    title = core.plain_text((item.get('title') or [''])[0])
    doi = core.normalized_doi(item.get('DOI', ''))
    published = precise_date(item)
    return {'title': title, 'doi': 'https://doi.org/' + doi if doi else '',
            'id': item.get('URL') or ('https://doi.org/' + doi if doi else title),
            'publication_date': published,
            'abstract_inverted_index': core.indexed_abstract(core.plain_text(item.get('abstract', ''))),
            'authorships': [{'author': {'display_name': (x.get('given', '') + ' ' + x.get('family', '')).strip()}}
                            for x in (item.get('author') or [])],
            'primary_location': {'landing_page_url': item.get('URL') or ''}}


def crossref_page(ym, query, cursor):
    start, end = month_bounds(ym)
    params = urllib.parse.urlencode({'query.bibliographic': query,
                                     'filter': f'from-pub-date:{start},until-pub-date:{end}',
                                     'rows': PAGE_SIZE, 'cursor': cursor,
                                     'select': 'DOI,title,abstract,published,published-online,published-print,issued,author,URL,type'})
    message = request('https://api.crossref.org/works?' + params, 'crossref').get('message', {})
    items = message.get('items') or []
    papers = [crossref_convert(x) for x in items if x.get('title')]
    # Crossref peut retourner next-cursor même après le dernier lot.
    next_cursor = message.get('next-cursor', '')
    finished = len(items) < PAGE_SIZE or not next_cursor or next_cursor == cursor
    return papers, len(items), finished, next_cursor


def dblp_page(query, offset):
    params = urllib.parse.urlencode({'q': query, 'h': PAGE_SIZE, 'f': offset, 'format': 'json'})
    data = request('https://dblp.org/search/publ/api?' + params, 'dblp')
    all_hits = data.get('result', {}).get('hits', {})
    hits = all_hits.get('hit', [])
    if isinstance(hits, dict):
        hits = [hits]
    total = int(all_hits.get('@total', 0))
    papers = []
    for hit in hits:
        info = hit.get('info') or {}
        year = str(info.get('year', ''))
        if not year.isdigit() or not 2016 <= int(year) <= 2026:
            continue
        # DBLP fournit souvent l'année seule. On tente une date complète via
        # Crossref (par DOI), sans créer une fausse date le 1er janvier.
        title = core.plain_text(info.get('title', ''))
        authors = info.get('authors', {}).get('author', [])
        if isinstance(authors, dict):
            authors = [authors]
        doi = core.normalized_doi(info.get('doi', ''))
        papers.append({'title': title, 'doi': 'https://doi.org/' + doi if doi else '',
                       'id': info.get('url') or title, 'publication_date': '',
                       'abstract_inverted_index': {},
                       'authorships': [{'author': {'display_name': a.get('text', '') if isinstance(a, dict) else str(a)}} for a in authors],
                       'primary_location': {'landing_page_url': info.get('ee') or info.get('url') or ''}})
    return papers, len(hits), offset + len(hits) >= total or len(hits) < PAGE_SIZE, ''


def enrich_dblp_date(paper):
    doi = core.normalized_doi(paper.get('doi'))
    if not doi:
        return paper
    try:
        url = 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='/')
        item = request(url, 'crossref').get('message', {})
        day = precise_date(item)
        if day and '2016-01-01' <= day <= '2026-10-31':
            paper['publication_date'] = day
            if not core.abstract(paper.get('abstract_inverted_index')) and item.get('abstract'):
                paper['abstract_inverted_index'] = core.indexed_abstract(core.plain_text(item['abstract']))
    except RuntimeError as exc:
        print('DBLP date : résolution DOI temporairement impossible :', str(exc)[:120])
    return paper


def iacr_page(cursor):
    params = {'verb': 'ListRecords'}
    if cursor and cursor != '*':
        params['resumptionToken'] = cursor
    else:
        params.update({'metadataPrefix': 'oai_dc', 'from': '2016-01-01', 'until': date.today().isoformat()})
    root = request('https://eprint.iacr.org/oai?' + urllib.parse.urlencode(params), 'iacr', xml=True)
    errs = root.findall('.//o:error', OAI_NS)
    if errs:
        raise RuntimeError('IACR OAI-PMH : ' + '; '.join((e.get('code', '') + ' ' + (e.text or '')).strip() for e in errs))
    papers = []
    records = root.findall('.//o:ListRecords/o:record', OAI_NS)
    for rec in records:
        header = rec.find('o:header', OAI_NS)
        if header is None or header.get('status') == 'deleted':
            continue
        metadata = rec.find('o:metadata', OAI_NS)
        if metadata is None:
            continue
        def texts(label):
            return [' '.join((x.text or '').split()) for x in metadata.findall('.//dc:' + label, OAI_NS) if (x.text or '').strip()]
        names = texts('creator')
        titles = texts('title')
        desc = texts('description')
        dates = [d[:10] for d in texts('date') if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?', d)]
        date_str = next((d for d in dates if '2016-01-01' <= d <= '2026-10-31'), '')
        urls = texts('identifier')
        refid = header.findtext('o:identifier', default='', namespaces=OAI_NS)
        match = re.search(r'(\d{4}/\d+)', refid)
        url = next((u for u in urls if re.match(r'https?://', u) and not u.endswith('.pdf')),
                   'https://eprint.iacr.org/' + match.group(1) if match else '')
        doi = next((u for u in urls if 'doi.org/' in u), '')
        if not titles or not date_str or not url:
            continue
        papers.append({'title': titles[0], 'doi': doi,
                       'publication_date': date_str, 'id': url,
                       'abstract_inverted_index': core.indexed_abstract(desc[0] if desc else ''),
                       'authorships': [{'author': {'display_name': a}} for a in names],
                       'primary_location': {'landing_page_url': url}})
    token = root.findtext('.//o:ListRecords/o:resumptionToken', default='', namespaces=OAI_NS).strip()
    return papers, len(records), not bool(token), token


def fetch(state):
    phase = state['phase']
    if phase == 'monthly':
        source, _, query = MONTHLY_TASKS[state['query_index']]
        if source == 'arxiv':
            remaining = int(state.get('arxiv_pause_until', 0) - time.time())
            if remaining > 0:
                raise SourceRateLimited('arxiv', remaining)
            return source, *arxiv_page(state['month'], query, state['offset'])
        return source, *crossref_page(state['month'], query, state['cursor'])
    if phase == 'dblp':
        _, query = DBLP_QUERIES[state['query_index']]
        return 'dblp', *dblp_page(query, state['offset'])
    if phase == 'iacr':
        return 'iacr', *iacr_page(state['cursor'])
    raise RuntimeError('Phase de rattrapage inconnue : ' + phase)


def advance_task(state):
    state.update(offset=0, cursor='*', pages_in_query=0)
    if state['phase'] == 'monthly':
        state['query_index'] += 1
        if state['query_index'] >= len(MONTHLY_TASKS):
            state['query_index'] = 0
            state['month'] = previous_month(state['month'])
            if state['month'] < END_MONTH:
                state['phase'] = 'dblp' if USE_DBLP else ('iacr' if USE_IACR else 'completed')
    elif state['phase'] == 'dblp':
        state['query_index'] += 1
        if state['query_index'] >= len(DBLP_QUERIES):
            state['query_index'] = 0
            state['phase'] = 'iacr' if USE_IACR else 'completed'
    elif state['phase'] == 'iacr':
        state['phase'] = 'completed'
    state['completed'] = state['phase'] == 'completed'


def finish_page(state, source, raw_count, finished, cursor):
    state['pages_done'] += 1
    state['pages_in_query'] += 1
    state['offset'] += raw_count
    if cursor:
        state['cursor'] = cursor
    if finished:
        advance_task(state)
    elif source != 'iacr' and state['pages_in_query'] >= MAX_QUERY_PAGES:
        note = f"{state['phase']}/{state.get('month')}/query-{state['query_index']} : résultat tronqué au plafond {MAX_QUERY_PAGES} pages"
        state['incomplete_queries'].append(note)
        print('ATTENTION :', note)
        advance_task(state)


def known_papers(cache, titles, dois, records, secondary):
    # En plus du cache et de Notion : vérifier les archives publiques déjà stockées.
    all_records = list(records.values()) + list(secondary.values())
    known_names = {canonical_title(x.get('title')) for x in all_records}
    known_dois = {core.normalized_doi(x.get('doi')) for x in all_records if x.get('doi')}
    return known_names, known_dois


def is_known(paper, cache, titles, dois, known_names, known_dois):
    name = core.norm(paper.get('title', ''))
    canonical = canonical_title(paper.get('title', ''))
    doi = core.normalized_doi(paper.get('doi'))
    return (name in titles or canonical in known_names or
            (doi and (doi in dois or doi in known_dois)) or cachelib.done(cache, paper))


def persist(state, cache, records, secondary):
    save_json(RECORDS_PATH, records)
    save_json(SECONDARY_PATH, secondary)
    cachelib.write(cache)
    save_json(STATE_PATH, state)


def process_one(rec, assessment, state, cache, records, secondary, titles, dois):
    paper = rec['paper']
    identity = cachelib.key(paper)
    cat = str(assessment.get('categorie') or '').casefold().strip()
    score = int(assessment['score'])
    url = ((paper.get('primary_location') or {}).get('landing_page_url')
           or paper.get('doi') or paper.get('id') or '')
    accepted = (cat in {'coeur', 'transfert_explicite'} and
                assessment.get('decision', '').casefold() in {'conserver', 'consigner', 'examiner'} and
                score >= (core.MIN_SCORE_CORE if cat == 'coeur' else core.MIN_SCORE_TRANSFER))
    if accepted:
        detail = core.analyze_detail(rec)
        schema = core.api(f'https://api.notion.com/v1/data_sources/{core.SOURCE_ID}').get('properties', {})
        # En cas de reprise après un import Notion avant sauvegarde, ne pas réimporter.
        latest_titles, latest_dois = core.existing_items()
        doi = core.normalized_doi(paper.get('doi'))
        if core.norm(paper['title']) not in latest_titles and not (doi and doi in latest_dois):
            core.notion_add(rec, assessment, detail, schema)
            print('NOTION :', paper['title'][:110])
        titles.add(core.norm(paper['title']))
        if doi:
            dois.add(doi)
        if identity:
            records[identity] = {'title': paper['title'], 'summary': detail.get('resume_court', ''),
                                 'score': score, 'themes': rec['themes'],
                                 'source': url, 'publication_date': paper['publication_date']}
            secondary.pop(identity, None)
        cachelib.set_status(cache, paper, assessment, 'imported')
        state['accepted'] += 1
        state['needs_publish'] = True
        print(f'PRINCIPAL ({score}/100) :', paper['title'][:110])
    else:
        if 60 <= score <= 74 and cat in {'coeur', 'transfert_explicite'} and identity:
            secondary[identity] = {'title': paper['title'], 'score': score, 'source': url,
                                   'publication_date': paper['publication_date']}
            state['secondary'] += 1
            state['needs_publish'] = True
            print(f'SECONDAIRE ({score}/100) :', paper['title'][:110])
        else:
            print(f'NON RETENU ({score}/100 ; {cat}) :', paper['title'][:110])
        cachelib.set_status(cache, paper, assessment, 'rejected')
    state['evaluated'] += 1
    # Article traité puis retiré de la file : opération atomique au niveau fichier.
    state['pending'].pop(0)
    persist(state, cache, records, secondary)


def task_description(state):
    if state['phase'] == 'monthly':
        source, _, query = MONTHLY_TASKS[state['query_index']]
        return f"{state['month']} / {source} / {query}"
    if state['phase'] == 'dblp':
        return 'DBLP / ' + DBLP_QUERIES[state['query_index']][1]
    return state['phase']


def main():
    state = state_read()
    print('MULTISOURCE |', task_description(state), '| pages cumulées :', state['pages_done'])
    if state['completed'] and not state['pending'] and not state['needs_publish']:
        print('Rattrapage multibase terminé. Requêtes plafonnées :', len(state['incomplete_queries']))
        return
    if not APPLY:
        # Un aperçu ne modifie aucun fichier et ne consulte pas Gemini/Notion.
        try:
            src, papers, raw, done, _ = fetch(state)
        except SourceRateLimited as exc:
            print(f'APERÇU : arXiv temporairement limité (HTTP 429). Réessayer dans {exc.retry_seconds} s.')
            print('Aucun fichier modifié, aucun appel Gemini/Notion.')
            return
        candidates = [p for p in papers if p.get('publication_date') and relevant(p)]
        print(f'APERÇU : {src} : {raw} résultats, {len(candidates)} candidats ayant une date précise et un signal technique.')
        for p in candidates[:8]:
            print(' -', p['publication_date'], p['title'][:135])
        print('Aucun fichier modifié, aucun appel Gemini/Notion.')
        return
    if not core.WRITE_NOTION:
        raise RuntimeError('MULTI_APPLY=true exige WRITE_NOTION=true.')
    if state['phase'] == 'dblp' and not USE_DBLP:
        state['phase'] = 'iacr' if USE_IACR else 'completed'
        state['query_index'] = 0
        state['completed'] = state['phase'] == 'completed'
    if state['phase'] == 'iacr' and not USE_IACR:
        state['phase'] = 'completed'
        state['completed'] = True
    cache = cachelib.read()
    records = load_json(RECORDS_PATH, {})
    secondary = load_json(SECONDARY_PATH, {})
    titles, dois = core.existing_items()
    pages = evaluated = 0
    while evaluated < MAX_AI and (state['pending'] or (pages < MAX_PAGES and not state['completed'])):
        if state['pending']:
            known_names, known_dois = known_papers(cache, titles, dois, records, secondary)
            # Retester les doublons après chaque appel, au cas où un autre workflow
            # avait enrichi Notion/cache depuis la sauvegarde du lot.
            while state['pending'] and is_known(state['pending'][0]['paper'], cache, titles, dois, known_names, known_dois):
                print('DÉJÀ CONNU :', state['pending'][0]['paper']['title'][:110])
                state['pending'].pop(0)
                state['skipped'] += 1
                persist(state, cache, records, secondary)
            if not state['pending']:
                continue
            limit = min(MAX_AI - evaluated, len(state['pending']), core.AI_BATCH_SIZE)
            batch = state['pending'][:limit]
            encoded = [{'paper': r['paper'], 'themes': r['themes'], 'sources': set(r['sources'])} for r in batch]
            assessments = core.ai_assess(encoded)
            if len(assessments) != len(encoded):
                raise RuntimeError('Gemini : lot incomplet, pending conservé pour une reprise.')
            for i, rec in enumerate(encoded):
                process_one(rec, assessments[i], state, cache, records, secondary, titles, dois)
                evaluated += 1
            continue
        try:
            src, papers, raw_count, finished, cursor = fetch(state)
        except SourceRateLimited as exc:
            state['arxiv_pause_until'] = max(
                state.get('arxiv_pause_until', 0), time.time() + exc.retry_seconds)
            persist(state, cache, records, secondary)
            until = datetime.fromtimestamp(state['arxiv_pause_until'], timezone.utc)
            print('PAUSE arXiv (HTTP 429) : la position actuelle est conservée.', flush=True)
            print('Reprise au plus tôt :', until.isoformat(timespec='minutes'),
                  '| position :', task_description(state), flush=True)
            print('Les autres sources seront traitées après la reprise d’arXiv.', flush=True)
            break
        if src == 'arxiv':
            state.pop('arxiv_pause_until', None)
        pages += 1
        if src == 'dblp':
            papers = [enrich_dblp_date(p) for p in papers if p.get('title') and relevant(p)]
        print(f'PAGE {pages}/{MAX_PAGES} : {task_description(state)} -> {raw_count} résultats')
        queued = []
        known_names, known_dois = known_papers(cache, titles, dois, records, secondary)
        in_page = set()
        for paper in papers:
            d = paper.get('publication_date', '')
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', d or ''):
                continue  # Année seule : ne pas fabriquer un mois/une date.
            if not '2016-01-01' <= d <= '2026-10-31':
                continue
            if not paper.get('title') or not relevant(paper):
                continue
            if is_known(paper, cache, titles, dois, known_names, known_dois):
                state['skipped'] += 1
                continue
            title_key = canonical_title(paper['title'])
            if title_key in in_page:
                continue
            in_page.add(title_key)
            theme = ((MONTHLY_TASKS[state['query_index']][1] if state['phase'] == 'monthly' else
                      DBLP_QUERIES[state['query_index']][0] if state['phase'] == 'dblp' else 'T06'))
            themes = core.matching_themes(paper) or [next((n for n, _ in core.THEMES if n.startswith(theme)), core.THEMES[0][0])]
            queued.append({'paper': paper, 'themes': themes, 'sources': [{'arxiv': 'arXiv', 'crossref': 'Crossref', 'dblp': 'DBLP', 'iacr': 'IACR ePrint'}[src]]})
        # On sauvegarde la page et son cursor AVANT Gemini pour ne jamais perdre
        # des candidats à cause d'une panne API / limite de quota.
        state['pending'] = queued
        finish_page(state, src, raw_count, finished, cursor)
        persist(state, cache, records, secondary)
        print('  ->', len(queued), 'candidat(s) en file. Prochaine étape :', task_description(state))
        # Même si nous venons d'avancer le mois ou la source, les candidats sont
        # autoportants et peuvent être traités sans relire le thème précédent.
    if state['pending']:
        print('Candidats restants pour le prochain lancement :', len(state['pending']))
    if state['needs_publish']:
        publish_history.publish(records, secondary)
        state['needs_publish'] = False
        persist(state, cache, records, secondary)
    print(f"FIN : pages ce lancement {pages}, évaluations {evaluated}, position {task_description(state)}, "
          f"pages cumulées {state['pages_done']}, candidats restants {len(state['pending'])}, "
          f"requêtes plafonnées {len(state['incomplete_queries'])}.")


if __name__ == '__main__':
    main()
