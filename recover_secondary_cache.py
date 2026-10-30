"""Récupération ponctuelle des articles secondaires depuis le cache V16.

N'appelle jamais Gemini ni Notion. Recherche les métadonnées manquantes dans
OpenAlex et refuse les rapprochements de titre douteux. À lancer d'abord en aperçu.
"""
import argparse
import difflib
import json
import os
import pathlib
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

CACHE = pathlib.Path('data/article_cache.json')
SECONDARY = pathlib.Path('data/history_secondary_records.json')
PRIMARY = pathlib.Path('data/history_records.json')
START = date(2025, 1, 1)
END = date.fromisoformat(os.environ.get('HISTORY_END', '2026-10-28'))
OPENALEX = 'https://api.openalex.org/works'


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def normalize(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in value).split())


def same_title(a, b, strict=False):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= (0.96 if strict else 0.86)


def valid_doi(doi):
    return bool(re.fullmatch(r'10\.\d{4,9}/[^\s]+', doi, re.I))


def openalex_get(url):
    api_key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if api_key:
        sep = '&' if '?' in url else '?'
        url += sep + urllib.parse.urlencode({'api_key': api_key})
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'scientific-watch-cache-recovery/1.0 (manual one-time migration)',
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            print(f'  OpenAlex HTTP {error.code} : entrée ignorée')
            return None
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            print(f'  Erreur réseau OpenAlex : {type(error).__name__}')
            return None
    return None


def find_work(cache_id, title):
    if cache_id.startswith('doi:'):
        doi = cache_id[4:].strip()
        if not valid_doi(doi):
            return None
        url = OPENALEX + '/' + urllib.parse.quote('https://doi.org/' + doi, safe=':/')
        work = openalex_get(url)
        if isinstance(work, dict) and same_title(title, work.get('title')):
            return work
        return None
    # Sans DOI : rapprochement strict par titre, non par simple mots-clés.
    if cache_id.startswith('title:') and title:
        query = urllib.parse.urlencode({'search': title, 'per_page': 5,
                                        'select': 'id,title,doi,publication_date,primary_location'})
        data = openalex_get(OPENALEX + '?' + query)
        matches = [w for w in (data or {}).get('results', [])
                   if same_title(title, w.get('title'), strict=True)]
        if len(matches) == 1:
            return matches[0]
    return None


def public_link(work):
    doi = str(work.get('doi') or '').strip()
    if doi.lower().startswith('https://doi.org/'):
        return doi
    location = work.get('primary_location') or {}
    link = location.get('landing_page_url') or work.get('id') or ''
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme not in ('http', 'https'):
        return ''
    host = (parsed.hostname or '').lower()
    if not host or 'notion.' in host or host in ('localhost', '127.0.0.1'):
        return ''
    return link


def main():
    parser = argparse.ArgumentParser(description='Récupération ponctuelle des secondaires depuis le cache')
    parser.add_argument('--apply', action='store_true', help='Écrire le fichier JSON (sinon aperçu)')
    args = parser.parse_args()
    cache = load(CACHE, {})
    if not isinstance(cache.get('articles'), dict):
        raise RuntimeError('Cache V16 absent ou format inattendu')
    secondary = load(SECONDARY, {})
    primary = load(PRIMARY, {})
    if not isinstance(secondary, dict) or not isinstance(primary, dict):
        raise RuntimeError('Fichiers historiques inattendus')

    candidates = []
    for cache_id, row in cache['articles'].items():
        if not isinstance(row, dict) or row.get('status') != 'rejected':
            continue
        category = str(row.get('category') or '').casefold().strip()
        try:
            score = int(row.get('score'))
        except (TypeError, ValueError):
            continue
        if 60 <= score <= 74 and category in ('coeur', 'transfert_explicite'):
            if cache_id not in primary and cache_id not in secondary:
                candidates.append((cache_id, row, score))

    print(f'Cache : {len(cache["articles"])} références')
    print(f'À retrouver (60–74, catégories techniques, non publiées) : {len(candidates)}')
    found = 0
    skipped = 0
    added = {}
    for idx, (cache_id, row, score) in enumerate(candidates, 1):
        title = str(row.get('title') or '').strip()
        print(f'[{idx}/{len(candidates)}] {score}/100 — {title[:100]}')
        work = find_work(cache_id, title)
        if not work:
            print('  → Ignoré : référence non confirmée dans OpenAlex')
            skipped += 1
            continue
        when = str(work.get('publication_date') or '')
        try:
            published = date.fromisoformat(when)
        except ValueError:
            print('  → Ignoré : date non fiable')
            skipped += 1
            continue
        if not START <= published <= END:
            print(f'  → Ignoré : date hors historique ({when})')
            skipped += 1
            continue
        link = public_link(work)
        if not link:
            print('  → Ignoré : lien public indisponible')
            skipped += 1
            continue
        # Vérification anti-doublons supplémentaire par DOI ou titre.
        norm_title = normalize(title)
        existing = list(primary.values()) + list(secondary.values()) + list(added.values())
        if any(normalize(rec.get('title')) == norm_title for rec in existing):
            print('  → Déjà présent dans une archive')
            skipped += 1
            continue
        added[cache_id] = {'title': title, 'score': score,
                           'source': link, 'publication_date': when}
        found += 1
        print(f'  → Retrouvé : {when} ; prêt pour {when[:7]}')
        time.sleep(0.25)
    if args.apply and added:
        secondary.update(added)
        save(SECONDARY, secondary)
        print(f'\n✅ {found} article(s) ajouté(s) à {SECONDARY}')
    elif args.apply:
        print('\nAucun nouvel article à ajouter.')
    else:
        print('\nMODE APERÇU : aucun fichier modifié.')
    print(f'Bilan : {found} récupérable(s), {skipped} ignoré(s).')
    print('Note : le cache ne dit pas si un rejet venait du quotidien ou de l’historique.')


if __name__ == '__main__':
    main()
