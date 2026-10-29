"""V16 : rattrapage historique progressif (OpenAlex) depuis janvier 2025.

Une page OpenAlex par exécution, curseur et file d'attente sauvegardés.
Les autres sources conservent leur rôle dans la collecte quotidienne ; ce
rattrapage n'est PAS exhaustif pour arXiv, Crossref, DBLP ou IACR.
"""
import calendar
import json
import os
import pathlib
import time
import urllib.parse
from datetime import date, timedelta

import cache_v16 as cachelib
import collect as core
import publish_history

STATE_PATH = pathlib.Path('data/history_progress.json')
RECORDS_PATH = pathlib.Path('data/history_records.json')
HISTORY_START = date(2025, 1, 1)
# Date fixe de fin : le quotidien couvrira les publications suivantes.
HISTORY_END = date.fromisoformat(os.environ.get('HISTORY_END', '2026-10-28'))
MAX_EVAL = max(1, min(25, int(os.environ.get('HISTORY_MAX_AI', '12'))))
DRY_RUN = os.environ.get('HISTORY_DRY_RUN', 'true').lower() != 'false'


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def month_bounds(month):
    start = date.fromisoformat(month + '-01')
    return start, min(next_month(start) - timedelta(days=1), HISTORY_END)


def all_queries():
    return [(theme, query) for theme, queries in core.THEMES for query in queries]


def historical_relevant(theme, paper):
    """Préfiltre historique élargi : le jugement final revient à Gemini.

    On garde le filtre original et on accepte aussi les titres portant un
    signal technique reconnu. Le préfiltre reste volontairement imparfait :
    il limite le bruit et le coût, sans prétendre être exhaustif.
    """
    title = core.norm(paper.get('title', ''))
    abstract = core.norm(core.abstract(paper.get('abstract_inverted_index')))

    if core.relevant(theme[:3], title, abstract):
        return True

    signals = (
        'root of trust', 'secure boot', 'trusted boot', 'measured boot',
        'hardware attestation', 'runtime attestation', 'remote attestation',
        'risc-v', 'riscv', 'opentitan', 'cva6', 'keystone',
        'security monitor', 'trusted execution environment',
        'microarchitectural side channel', 'microarchitectural side-channel',
        'cache side channel', 'cache side-channel', 'cache timing',
        'spectre', 'secure enclave', 'memory integrity', 'hardware isolation',
    )
    return any(signal in title for signal in signals)


def clean_state(state):
    if not state:
        return {'month': '2025-01', 'query_index': 0, 'cursor': '*', 'pending': [],
                'last_page': False, 'completed': False, 'pages_done': 0}
    return state


def advance_query(state):
    state['query_index'] += 1
    state['cursor'] = '*'
    state['last_page'] = False
    if state['query_index'] >= len(all_queries()):
        state['query_index'] = 0
        start = next_month(date.fromisoformat(state['month'] + '-01'))
        state['month'] = start.strftime('%Y-%m')
        if start > HISTORY_END:
            state['completed'] = True


def fetch_page(state):
    start, end = month_bounds(state['month'])
    theme, query = all_queries()[state['query_index']]
    params = {
        'search': query,
        'filter': f'from_publication_date:{start.isoformat()},to_publication_date:{end.isoformat()}',
        'per_page': '25', 'cursor': state['cursor'], 'select': 'id,title,doi,publication_date,authorships,abstract_inverted_index,primary_location',
    }
    if core.API_KEY:
        params['api_key'] = core.API_KEY
    url = 'https://api.openalex.org/works?' + urllib.parse.urlencode(params)
    result = core.api(url)
    papers = result.get('results', [])
    next_cursor = result.get('meta', {}).get('next_cursor')
    # On sauvegarde la liste brute pour ne pas perdre un candidat en cas de quota.
    state['pending'] = [
        p for p in papers
        if p.get('title') and historical_relevant(theme, p)
    ]
    state['cursor'] = next_cursor or '*'
    state['last_page'] = not (next_cursor and papers)
    state['pages_done'] += 1
    return len(papers), len(state['pending'])


def accepted(assessment):
    category = str(assessment.get('categorie', '')).casefold().strip()
    score = int(assessment['score'])
    threshold = (core.MIN_SCORE_CORE if category == 'coeur' else
                 core.MIN_SCORE_TRANSFER if category == 'transfert_explicite' else 101)
    return assessment.get('decision', '').casefold() in {'conserver', 'consigner', 'examiner'} and score >= threshold


def to_record(paper, theme):
    return {'paper': paper, 'themes': [theme], 'sources': {'OpenAlex'}}


def main():
    state = clean_state(read_json(STATE_PATH, {}))
    cache = cachelib.read()
    records = read_json(RECORDS_PATH, {})
    if state['completed']:
        print('Historique achevé :', state['month'])
        return
    if DRY_RUN:
        # Aperçu sans appel Gemini ni écriture, pour un premier test peu coûteux.
        print('APERÇU historique :', json.dumps({k: v for k,v in state.items() if k != 'pending'}, ensure_ascii=False))
        print('Aucune écriture. Pour importer : HISTORY_DRY_RUN=false.')
        return
    if not core.WRITE_NOTION:
        raise RuntimeError('Le mode historique réel exige WRITE_NOTION=true')
    titles, dois = core.existing_items()
    # Une seule page distante lue par exécution ; le reliquat est ensuite repris.
    if not state['pending']:
        if state['last_page']:
            advance_query(state)
        if state['completed']:
            write_json(STATE_PATH, state)
            return
        raw_count, found = fetch_page(state)
        print(f"Historique {state['month']} requête {state['query_index'] + 1}/{len(all_queries())}: {raw_count} résultats, {found} pertinents selon le filtre local")
    theme, query = all_queries()[state['query_index']]
    print(f"Traitement {state['month']} — {theme} — {query} ; attente={len(state['pending'])}")
    # On consomme au plus MAX_EVAL candidats non encore connus pour ce lancement.
    candidates = []
    processed = []
    for paper in state['pending']:
        title = core.norm(paper.get('title', ''))
        doi = core.normalized_doi(paper.get('doi'))
        if title in titles or (doi and doi in dois) or cachelib.done(cache, paper):
            processed.append(paper)
            continue
        if len(candidates) < MAX_EVAL:
            candidates.append(to_record(paper, theme))
            processed.append(paper)
        else:
            break
    reviewed = []
    for offset in range(0, len(candidates), core.AI_BATCH_SIZE):
        batch = candidates[offset:offset + core.AI_BATCH_SIZE]
        decisions = core.ai_assess(batch)
        if len(decisions) != len(batch):
            raise RuntimeError('Lot Gemini incomplet ; progression conservée au dernier commit.')
        reviewed.extend((rec, decisions[idx]) for idx, rec in enumerate(batch))
        if offset + len(batch) < len(candidates):
            time.sleep(core.AI_BATCH_PAUSE)
    # Préparer toutes les fiches AVANT de commencer à écrire dans Notion.
    new_entries = []
    for rec, decision in reviewed:
        if accepted(decision):
            detail = core.analyze_detail(rec)
            new_entries.append((rec, decision, detail))
        else:
            cachelib.set_status(cache, rec['paper'], decision, 'rejected')
    schema = core.api(f'https://api.notion.com/v1/data_sources/{core.SOURCE_ID}').get('properties', {}) if new_entries else {}
    for rec, decision, detail in new_entries:
        paper = rec['paper']
        title = core.norm(paper.get('title'))
        doi = core.normalized_doi(paper.get('doi'))
        # Relecture idempotente : un incident entre Notion et le commit ne duplique pas.
        current_titles, current_dois = core.existing_items()
        if title not in current_titles and not (doi and doi in current_dois):
            core.notion_add(rec, decision, detail, schema)
            print('Notion :', paper['title'][:100])
        cachelib.set_status(cache, paper, decision, 'imported')
        identity = cachelib.key(paper)
        if identity not in records:
            loc = paper.get('primary_location') or {}
            link = loc.get('landing_page_url') or paper.get('doi') or paper.get('id') or ''
            records[identity] = {
                'title': paper['title'], 'summary': detail.get('resume_court', ''),
                'score': int(decision['score']), 'themes': [theme],
                'source': link, 'publication_date': paper.get('publication_date', ''),
            }
    # Le cache et la progression sont sauvegardés APRÈS l'import validé.
    # Si publication GitHub échoue, le prochain run reprend de ce point.
    completed_ids = {id(p) for p in processed}
    state['pending'] = [p for p in state['pending'] if id(p) not in completed_ids]
    write_json(RECORDS_PATH, records)
    cachelib.write(cache)
    write_json(STATE_PATH, state)
    if records:
        publish_history.publish(records)
    print(f"Fin de lot : {len(reviewed)} analysé(s), {len(new_entries)} retenu(s), {len(state['pending'])} en attente. Pages visitées : {state['pages_done']}")


if __name__ == '__main__':
    main()
