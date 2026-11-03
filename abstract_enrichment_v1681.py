"""Recherche prudente de résumés académiques manquants, avant Gemini.

Aucun modèle IA, aucune navigation libre et aucune modification du cache Gemini.
Correspondances vérifiées par DOI/arXiv, ou titre exact avec contrôle d'année.
Les erreurs HTTP et limites de débit sont non bloquantes.
"""
from __future__ import annotations

import difflib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ENABLED = os.environ.get('ABSTRACT_ENRICH_ENABLED', 'true').lower() == 'true'
MAX_CHECKS = max(0, min(25, int(os.environ.get('ABSTRACT_ENRICH_MAX', '8'))))
USE_SEMANTIC_SCHOLAR = os.environ.get('ABSTRACT_SEMANTIC_SCHOLAR', 'true').lower() == 'true'
MIN_CHARS = 100
TIMEOUT_SECONDS = 12
USER_AGENT = 'scientific-watch/16.8.1 (noncommercial academic bibliography; no ai web search)'
_PAUSE = float(os.environ.get('ABSTRACT_LOOKUP_DELAY', '0.8'))
_LAST_CALL = 0.0
_CACHE: dict[str, tuple[str, str]] = {}
_STOPS: set[str] = set()
_USED_CHECKS = 0  # budget global à toute l’exécution Python, lots Gemini confondus


def clean_text(text):
    text = re.sub(r'<[^>]+>', ' ', str(text or ''))
    text = html.unescape(text)
    return ' '.join(text.split())


def clean_title(text):
    s = html.unescape(str(text or '')).casefold()
    s = s.replace('‐', '-').replace('–', '-').replace('—', '-')
    return ' '.join(re.findall(r'[\w]+', s))


def title_match(given, found, strict=False):
    a, b = clean_title(given), clean_title(found)
    if not a or not b:
        return False
    if strict:
        return a == b
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


def doi_key(paper):
    raw = str(paper.get('doi') or '').strip().lower()
    raw = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', raw)
    raw = raw.removeprefix('doi:').strip()
    if re.fullmatch(r'10\.\d{4,9}/\S+', raw):
        return raw
    # Certains articles DBLP n'ont le DOI que dans leur URL.
    for field in (paper.get('id'), (paper.get('primary_location') or {}).get('landing_page_url')):
        match = re.search(r'doi\.org/(10\.\d{4,9}/[^?#\s]+)', str(field or ''), flags=re.I)
        if match:
            return urllib.parse.unquote(match.group(1)).lower()
    return ''


def arxiv_key(paper):
    for field in (paper.get('id'), (paper.get('primary_location') or {}).get('landing_page_url')):
        m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?', str(field or ''), flags=re.I)
        if m:
            return m.group(1)
    return ''


def _get(url, service, *, xml=False, headers=None):
    global _LAST_CALL
    if service in _STOPS:
        return None
    pause = max(0., _PAUSE - (time.monotonic() - _LAST_CALL))
    if pause:
        time.sleep(pause)
    req_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/atom+xml' if xml else 'application/json'}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            if xml:
                result = ET.fromstring(response.read())
            else:
                result = json.load(response)
        return result
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429, 500, 502, 503, 504):
            _STOPS.add(service)  # ne pas marteler une API temporairement indisponible
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError, OSError):
        _STOPS.add(service)
        return None
    finally:
        _LAST_CALL = time.monotonic()


def from_arxiv(paper, aid):
    url = 'https://export.arxiv.org/api/query?' + urllib.parse.urlencode({'id_list': aid})
    root = _get(url, 'arxiv', xml=True)
    if root is None:
        return ''
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    for entry in root.findall('atom:entry', ns):
        url_val = entry.findtext('atom:id', default='', namespaces=ns)
        if not re.search(r'/abs/' + re.escape(aid) + r'(?:v\d+)?$', url_val):
            continue
        title = entry.findtext('atom:title', default='', namespaces=ns)
        if title_match(paper.get('title'), title):
            return clean_text(entry.findtext('atom:summary', default='', namespaces=ns))
    return ''


def from_openalex_doi(paper, doi):
    query = {'filter': 'doi:https://doi.org/' + doi,
             'per-page': '1', 'select': 'doi,title,abstract_inverted_index,publication_year'}
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if key:
        query['api_key'] = key
    data = _get('https://api.openalex.org/works?' + urllib.parse.urlencode(query), 'openalex')
    for work in ((data or {}).get('results') or []):
        if doi_key(work) != doi or not title_match(paper.get('title'), work.get('title')):
            continue
        words = sorted((int(pos), word) for word, positions in (work.get('abstract_inverted_index') or {}).items()
                       for pos in positions if isinstance(pos, int))
        return clean_text(' '.join(w for _, w in words))
    return ''


def from_openalex_title(paper):
    title = str(paper.get('title') or '').strip()
    if len(title) < 35:
        return ''
    query = {'search': title, 'per-page': '5',
             'select': 'doi,title,abstract_inverted_index,publication_year'}
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if key:
        query['api_key'] = key
    data = _get('https://api.openalex.org/works?' + urllib.parse.urlencode(query), 'openalex')
    year = (paper.get('publication_date') or '')[:4]
    for work in ((data or {}).get('results') or []):
        if not title_match(title, work.get('title'), strict=True):
            continue
        if year.isdigit() and work.get('publication_year'):
            if abs(int(year) - int(work['publication_year'])) > 1:
                continue
        words = sorted((int(pos), word) for word, positions in (work.get('abstract_inverted_index') or {}).items()
                       for pos in positions if isinstance(pos, int))
        abstract = clean_text(' '.join(w for _, w in words))
        if len(abstract) >= MIN_CHARS:
            return abstract
    return ''


def from_crossref(paper, doi):
    url = 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='/')
    data = _get(url, 'crossref')
    item = (data or {}).get('message') or {}
    titles = item.get('title') or []
    if str(item.get('DOI') or '').casefold() != doi or not titles:
        return ''
    if not title_match(paper.get('title'), titles[0]):
        return ''
    return clean_text(item.get('abstract'))


def from_semantic_scholar(paper, identifier):
    url = ('https://api.semanticscholar.org/graph/v1/paper/' +
           urllib.parse.quote(identifier, safe='') + '?fields=title,abstract,externalIds')
    api_key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY', '').strip()
    headers = {'x-api-key': api_key} if api_key else {}
    data = _get(url, 'semantic_scholar', headers=headers)
    if not isinstance(data, dict) or not title_match(paper.get('title'), data.get('title')):
        return ''
    external = data.get('externalIds') or {}
    if identifier.startswith('DOI:'):
        if str(external.get('DOI') or '').casefold() != identifier[4:].casefold():
            return ''
    elif identifier.startswith('ARXIV:'):
        if str(external.get('ArXiv') or '').casefold() != identifier[6:].casefold():
            return ''
    return clean_text(data.get('abstract'))


def lookup(paper):
    """Renvoie (abstract, provenance) si identité confirmée, sinon ('','')."""
    title = clean_title(paper.get('title'))
    doi = doi_key(paper)
    aid = arxiv_key(paper)
    if not title:
        return '', ''
    cache_key = 'doi:' + doi if doi else ('arxiv:' + aid if aid else 'titre:' + title)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    strategies = []
    if aid:
        strategies.append(('arXiv', lambda: from_arxiv(paper, aid)))
    if doi:
        strategies += [('OpenAlex DOI', lambda: from_openalex_doi(paper, doi)),
                       ('Crossref DOI', lambda: from_crossref(paper, doi))]
    elif not aid:
        strategies.append(('OpenAlex titre exact', lambda: from_openalex_title(paper)))
    if USE_SEMANTIC_SCHOLAR and (doi or aid):
        identifier = 'DOI:' + doi if doi else 'ARXIV:' + aid
        strategies.append(('Semantic Scholar', lambda: from_semantic_scholar(paper, identifier)))
    result = ('', '')
    for source, fn in strategies:
        try:
            value = fn()
        except (TypeError, ValueError, AttributeError):
            continue
        if len(value) >= MIN_CHARS:
            result = (value[:12000], source)
            break
    _CACHE[cache_key] = result
    return result


def enrich(records, to_indexed, from_indexed):
    """Mutates only records with missing abstract; max MAX_CHECKS network lookups.

    Reuses candidate paper dict in pending queue so a failed Gemini request can
    still be resumed by the history workflow without losing fetched context.
    """
    if not ENABLED or MAX_CHECKS <= 0:
        return
    global _USED_CHECKS
    checked = found = missing = 0
    for rec in records:
        paper = rec['paper']
        if from_indexed(paper.get('abstract_inverted_index')).strip():
            continue
        if _USED_CHECKS >= MAX_CHECKS:
            break
        checked += 1
        _USED_CHECKS += 1
        value, source = lookup(paper)
        if value:
            paper['abstract_inverted_index'] = to_indexed(value)
            paper['abstract_provenance'] = source
            print(f'RÉSUMÉ RETROUVÉ [{source}] : {str(paper.get("title", ""))[:86]}', flush=True)
            found += 1
        else:
            paper['abstract_provenance'] = 'titre seul (introuvable ou API indisponible)'
            missing += 1
    if checked:
        print(f'Enrichissement abstracts : {found}/{checked} trouvé(s), '
              f'{missing} introuvable(s), { _USED_CHECKS }/{MAX_CHECKS} recherches ce lancement.', flush=True)
