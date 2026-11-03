"""Métadonnées bibliographiques V16.8, sans appel Gemini.

Séparer lieu de publication (métadonnée de source) et pertinence (score IA).
Les conférences repères forment une liste ÉDITORIALE, pas un classement ICORE.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

# Attention aux noms ambigus : on n'identifie un alias QUE dans le champ
# du lieu de publication, jamais dans le titre/abstract de l'article.
LANDMARKS = (
    ('IEEE S&P', 2, (r'\bieee\s+(?:symposium\s+on\s+)?security\s*(?:&|and)\s*privacy\b',
                       r'^\s*(?:ieee\s*)?s\s*&\s*p\s*(?:\d{4})?\s*$')),
    ('USENIX Security', 2, (r'\busenix\s+security\b', r'\busenix\s+security\s+symposium\b')),
    ('ACM CCS', 2, (r'\bacm\s+(?:sigsac\s+)?conference\s+on\s+computer\s+and\s+communications\s+security\b',
                      r'^\s*(?:acm\s+)?ccs\s*(?:\d{4})?\s*$')),
    ('NDSS', 2, (r'\bnetwork\s+and\s+distributed\s+system\s+security\b', r'^\s*ndss\s*(?:\d{4})?\s*$')),
    ('ISCA', 2, (r'\binternational\s+symposium\s+on\s+computer\s+architecture\b',
                   r'^\s*(?:acm\s+)?isca\s*(?:\d{4})?\s*$')),
    ('MICRO', 2, (r'\binternational\s+symposium\s+on\s+microarchitecture\b',
                    r'^\s*(?:ieee\/acm\s+)?micro\s*(?:\d{4})?\s*$')),
    ('HPCA', 2, (r'\binternational\s+symposium\s+on\s+high.performance\s+computer\s+architecture\b',
                   r'^\s*hpca\s*(?:\d{4})?\s*$')),
    ('ASPLOS', 2, (r'\barchitectural\s+support\s+for\s+programming\s+languages\s+and\s+operating\s+systems\b',
                     r'^\s*(?:acm\s+)?asplos\s*(?:\d{4})?\s*$')),
    ('IEEE HOST', 1, (r'\bhardware.oriented\s+security\s+and\s+trust\b',
                        r'^\s*(?:ieee\s+)?host\s*(?:\d{4})?\s*$')),
    ('CHES', 1, (r'\bcryptographic\s+hardware\s+and\s+embedded\s+systems\b',
                   r'^\s*(?:iacr\s+)?ches\s*(?:\d{4})?\s*$')),
    ('DAC', 1, (r'\bdesign\s+automation\s+conference\b', r'^\s*(?:acm\/ieee\s+)?dac\s*(?:\d{4})?\s*$')),
    ('DATE', 1, (r'\bdesign,?\s+automation\s*(?:&|and)\s*test\s+in\s+europe\b',
                   r'^\s*date\s*(?:\d{4})?\s*$')),
)
FIELDS = ('publication_venue', 'publication_type', 'venue_provenance',
          'venue_priority', 'venue_landmark', 'venue_note')


def _clean(value):
    return ' '.join(str(value or '').strip().split())[:250]


def _first(value):
    if isinstance(value, list):
        return next((_clean(x) for x in value if _clean(x)), '')
    if isinstance(value, str):
        return _clean(value)
    return ''


def _recognized(venue):
    for name, priority, patterns in LANDMARKS:
        if any(re.search(p, venue, re.I) for p in patterns):
            return name, priority
    return '', 0


def info(name='', kind='', provenance='', note=''):
    name = _clean(name)
    kind = (kind or 'inconnu').strip().lower()
    if kind not in {'conference', 'revue', 'prepublication', 'inconnu'}:
        kind = 'inconnu'
    priority_name, priority = _recognized(name) if kind == 'conference' else ('', 0)
    return {'publication_venue': name, 'publication_type': kind,
            'venue_provenance': _clean(provenance),
            'venue_priority': priority, 'venue_landmark': priority_name,
            'venue_note': _clean(note)}


def from_crossref(item):
    doc_type = str(item.get('type') or '').lower()
    event = item.get('event') or {}
    event_name = _clean(event.get('name')) if isinstance(event, dict) else ''
    container = _first(item.get('container-title'))
    if doc_type in {'proceedings-article', 'proceedings'}:
        return info(event_name or container, 'conference', 'Crossref')
    if doc_type in {'journal-article', 'journal'}:
        return info(container, 'revue', 'Crossref')
    if doc_type in {'posted-content', 'preprint'}:
        return info('', 'prepublication', 'Crossref')
    # Sans type explicite, ne pas transformer "Proceedings" en conférence assurée.
    return info(container, 'inconnu', 'Crossref')


def from_dblp(item):
    name = _first(item.get('venue'))
    typ = str(item.get('type') or '').casefold()
    if 'conference' in typ or 'workshop' in typ:
        kind = 'conference'
    elif 'journal' in typ:
        kind = 'revue'
    else:
        kind = 'inconnu'
    return info(name, kind, 'DBLP')


def from_openalex(item):
    # OpenAlex peut confondre conférence/revue; afficher sa provenance.
    loc = item.get('primary_location') or {}
    source = loc.get('source') or {}
    source_type = str(source.get('type') or '').casefold()
    name = _clean(source.get('display_name'))
    if source_type == 'conference':
        return info(name, 'conference', 'OpenAlex')
    if source_type == 'journal':
        return info(name, 'revue', 'OpenAlex')
    if source_type == 'repository' or str(item.get('type')).lower() == 'preprint':
        return info('', 'prepublication', 'OpenAlex')
    return info(name if name and source_type not in {'repository', 'metadata'} else '',
                'inconnu', 'OpenAlex')


def from_arxiv(journal_ref=''):
    # journal_ref est déclaré par les auteurs; ne pas promouvoir en conférence
    # confirmée sans corroboration bibliographique externe.
    return info('', 'prepublication', 'arXiv',
                ('Référence déclarée sur arXiv (non vérifiée) : ' + _clean(journal_ref))
                if journal_ref else '')


def from_iacr():
    return info('', 'prepublication', 'IACR ePrint')


def annotate(paper, metadata):
    for field in FIELDS:
        paper[field] = metadata.get(field, '' if field != 'venue_priority' else 0)
    return paper


def metadata(paper):
    return {field: paper.get(field, '' if field != 'venue_priority' else 0) for field in FIELDS}


def from_paper(paper):
    # La provenance détermine quelle métadonnée est sûre.
    if paper.get('publication_venue') or paper.get('publication_type'):
        return metadata(paper)
    if (paper.get('primary_location') or {}).get('source'):
        return from_openalex(paper)
    sources = [str(s).lower() for s in paper.get('_discovery_sources', [])]
    if 'arxiv' in sources:
        return from_arxiv()
    if 'iacr eprint' in sources:
        return from_iacr()
    return info()


def priority(paper):
    # Aucun changement du score IA; uniquement un indice de tri de la file.
    try:
        return int(paper.get('venue_priority') or 0)
    except (ValueError, TypeError):
        return 0


def enrich_confirmed(paper):
    """Pour un article déjà sélectionné, demander le lieu par DOI si inconnu.

    Déclenché APRÈS la décision IA : pas d'appels bibliographiques supplémentaires
    sur tous les faux positifs, et aucun changement de la pertinence Gemini.
    """
    if paper.get('publication_venue'):
        return paper
    doi = doi_from_record(paper)
    if not doi:
        return paper
    confirmed = crossref_lookup(doi)
    if confirmed and confirmed.get('publication_venue'):
        annotate(paper, confirmed)
    return paper


def prioritize(candidates, budget):
    """Donner au maximum 1/2 du budget aux conférences repères.

    Le reste conserve l'ordre d'origine; aucun article n'est supprimé.
    Pour la veille quotidienne (qui tronque à MAX_AI), cela évite de laisser
    toute la capacité aux seules conférences privilégiées.
    """
    budget = max(0, int(budget))
    if not budget:
        return list(candidates)
    high = [x for x in candidates if priority(x.get('paper', {})) > 0]
    low = [x for x in candidates if priority(x.get('paper', {})) == 0]
    quota = (budget + 1) // 2
    preferred = sorted(high, key=lambda x: priority(x['paper']), reverse=True)[:quota]
    head = preferred + low[:max(0, budget-len(preferred))]
    # Remplir les espaces si le groupe normal est trop petit.
    ids = {id(x) for x in head}
    head.extend(x for x in candidates if id(x) not in ids and len(head) < budget)
    ids = {id(x) for x in head}
    return head + [x for x in candidates if id(x) not in ids]


def merge_prefer_informative(first, second):
    """Éviter de perdre le lieu lorsque l'abstract d'une autre source est meilleur."""
    f = metadata(first)
    s = metadata(second)
    score_first = (bool(f.get('publication_venue')) * 4
                   + (f.get('publication_type') in ('conference', 'revue')) * 2)
    score_second = (bool(s.get('publication_venue')) * 4
                    + (s.get('publication_type') in ('conference', 'revue')) * 2)
    return s if score_second > score_first else f


def display(record):
    name = _clean(record.get('publication_venue'))
    typ = record.get('publication_type') or ''
    if name:
        if typ == 'inconnu':
            return name + ' (type à vérifier)'
        return name
    if typ == 'prepublication':
        return 'Prépublication (conférence non vérifiée)'
    return ''


def doi_from_record(record):
    link = str(record.get('doi') or record.get('source') or '')
    raw = link.strip()
    if re.fullmatch(r'10\.\d{4,9}/[^\s]+', raw, re.I):
        return raw.lower()
    match = re.search(r'(?:https?://(?:dx\.)?doi\.org/|\bdoi:|/doi/|\b)(10\.\d{4,9}/[^\s?#]+)', link, re.I)
    return match.group(1).rstrip('.,;)]').lower() if match else ''


def crossref_lookup(doi, timeout=20):
    """Recherche exacte par DOI; ne cherche pas par simple similarité de titre."""
    if not re.fullmatch(r'10\.\d{4,9}/[^\s]+', doi or '', re.I):
        return None
    url = 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='/')
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'ScientificWatch/1.8 (academic metadata enrichment)',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            item = json.load(res).get('message') or {}
        returned = str(item.get('DOI') or '').lower()
        if returned != doi.lower():
            return None
        return from_crossref(item)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


def openalex_lookup(work_url, api_key='', timeout=20):
    match = re.fullmatch(r'https?://openalex\.org/(W\d+)', work_url or '', re.I)
    if not match:
        return None
    url = f'https://api.openalex.org/works/{match.group(1)}'
    if api_key:
        url += '?api_key=' + urllib.parse.quote(api_key)
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json', 'User-Agent': 'ScientificWatch/1.8 (academic metadata enrichment)'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return from_openalex(json.load(res))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def _normalized_title(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def openalex_title_lookup(title, pub_year='', api_key='', timeout=22):
    """Correspondance exacte du titre, non une recherche floue.

    On ne reprend un lieu que si UN SEUL travail non ambigu concorde
    (après normalisation) et que l'année est cohérente (écart <= 2 ans).
    Une réponse vide ou ambiguë reste sans lieu vérifié.
    """
    if not title or len(title.strip()) < 15:
        return None
    params = {'search': title, 'per_page': 6,
              'select': 'id,title,publication_year,primary_location,type'}
    if api_key:
        params['api_key'] = api_key
    url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'ScientificWatch/1.8 (academic metadata enrichment)',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            works = json.load(res).get('results') or []
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    matched = []
    for work in works:
        if _normalized_title(work.get('title')) != _normalized_title(title):
            continue
        try:
            if pub_year and abs(int(work.get('publication_year')) - int(pub_year)) > 2:
                continue
        except (ValueError, TypeError):
            continue
        md = from_openalex(work)
        if md.get('publication_venue'):
            matched.append(md)
    unique = {(x['publication_venue'].casefold(), x['publication_type']) for x in matched}
    if len(unique) != 1:
        return None
    result = matched[0].copy()
    result['venue_provenance'] = 'OpenAlex (titre exact)'
    return result


def identify_preprint_url(link):
    host = (urlparse(link or '').hostname or '').lower()
    if host in {'arxiv.org', 'export.arxiv.org'}:
        return from_arxiv()
    if host == 'eprint.iacr.org':
        return from_iacr()
    return None
