"""Rattrapage complémentaire de décembre 2024 vers janvier 2016.

Jusqu'à 20 pages OpenAlex par exécution, avec reprise et budget Gemini distinct.
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

STATE_PATH = pathlib.Path('data/history_backward_progress.json')
RECORDS_PATH = pathlib.Path('data/history_records.json')
SECONDARY_PATH = pathlib.Path('data/history_secondary_records.json')
HISTORY_START = date(2016, 1, 1)
# Date fixe de début du balayage ancien (décembre 2024).
HISTORY_END = date(2024, 12, 31)
MAX_EVAL = max(1, min(100, int(os.environ.get('HISTORY_MAX_AI', '30'))))
MAX_PAGES = max(1, min(100, int(os.environ.get('HISTORY_MAX_PAGES', '20'))))
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


def previous_month(value):
    return date(value.year - (value.month == 1), 12 if value.month == 1 else value.month - 1, 1)


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
        'sanctum', 'software isolation', 'enclave', 'trusted processor',
    )
    return any(signal in title for signal in signals)


def clean_state(state):
    if not state:
        return {'month': '2024-12', 'query_index': 0, 'cursor': '*', 'pending': [],
                'last_page': False, 'completed': False, 'pages_done': 0}
    return state


def advance_query(state):
    state['query_index'] += 1
    state['cursor'] = '*'
    state['last_page'] = False
    if state['query_index'] >= len(all_queries()):
        state['query_index'] = 0
        start = previous_month(date.fromisoformat(state['month'] + '-01'))
        state['month'] = start.strftime('%Y-%m')
        if start < HISTORY_START:
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


def persist(state, cache, records, secondary_records):
    """Sauvegarde locale après chaque lot, même si le workflow s'arrête ensuite.

    GitHub Actions commite data/ à la fin du job, y compris après erreur.
    """
    write_json(RECORDS_PATH, records)
    write_json(SECONDARY_PATH, secondary_records)
    cachelib.write(cache)
    write_json(STATE_PATH, state)


def process_pending(state, cache, records, secondary_records, titles, dois, budget):
    """Traite un maximum de `budget` nouveaux candidats, sans perdre le reliquat."""
    if not state['pending']:
        return 0, 0

    theme, _ = all_queries()[state['query_index']]
    candidates = []
    consumed = []
    # Éliminer aussi les doublons à l'intérieur de la page elle-même.
    seen_in_page = set()
    for paper in state['pending']:
        title = core.norm(paper.get('title', ''))
        doi = core.normalized_doi(paper.get('doi'))
        identity = cachelib.key(paper)
        if (title in titles or (doi and doi in dois) or
                cachelib.done(cache, paper) or (identity and identity in seen_in_page)):
            consumed.append(paper)
            continue
        if len(candidates) >= budget:
            break
        candidates.append(to_record(paper, theme))
        consumed.append(paper)
        if identity:
            seen_in_page.add(identity)

    reviewed = []
    for offset in range(0, len(candidates), core.AI_BATCH_SIZE):
        batch = candidates[offset:offset + core.AI_BATCH_SIZE]
        decisions = core.ai_assess(batch)
        if len(decisions) != len(batch):
            raise RuntimeError('Lot Gemini incomplet : nouvelle tentative au prochain lancement.')
        reviewed.extend((rec, decisions[idx]) for idx, rec in enumerate(batch))
        if offset + len(batch) < len(candidates):
            time.sleep(core.AI_BATCH_PAUSE)

    accepted_entries = []
    for rec, decision in reviewed:
        if accepted(decision):
            # L'analyse détaillée a lieu uniquement pour les articles retenus.
            detail = core.analyze_detail(rec)
            accepted_entries.append((rec, decision, detail))
        else:
            paper = rec['paper']
            category = str(decision.get('categorie', '')).casefold().strip()
            score = int(decision.get('score', 0))
            # Les 60–74 non importés restent visibles, sans analyse détaillée.
            # Les catégories périphériques / hors sujet ne sont jamais publiées.
            if 60 <= score <= 74 and category in {'coeur', 'transfert_explicite'}:
                identity = cachelib.key(paper)
                date_str = str(paper.get('publication_date') or '')
                location = paper.get('primary_location') or {}
                link = paper.get('doi') or location.get('landing_page_url') or paper.get('id') or ''
                if identity and identity not in secondary_records and len(date_str) == 10:
                    secondary_records[identity] = {
                        'title': paper.get('title', ''),
                        'score': score,
                        'source': link,
                        'publication_date': date_str,
                    }
            cachelib.set_status(cache, paper, decision, 'rejected')

    schema = (core.api(f'https://api.notion.com/v1/data_sources/{core.SOURCE_ID}')
              .get('properties', {})) if accepted_entries else {}
    for rec, decision, detail in accepted_entries:
        paper = rec['paper']
        title = core.norm(paper.get('title'))
        doi = core.normalized_doi(paper.get('doi'))
        # Revérifier Notion : protège d'un arrêt après import mais avant commit.
        current_titles, current_dois = core.existing_items()
        if title not in current_titles and not (doi and doi in current_dois):
            core.notion_add(rec, decision, detail, schema)
            print('Notion :', paper['title'][:100])
        titles.add(title)
        if doi:
            dois.add(doi)
        cachelib.set_status(cache, paper, decision, 'imported')
        secondary_records.pop(cachelib.key(paper), None)
        identity = cachelib.key(paper)
        if identity and identity not in records:
            loc = paper.get('primary_location') or {}
            link = loc.get('landing_page_url') or paper.get('doi') or paper.get('id') or ''
            records[identity] = {
                'title': paper['title'], 'summary': detail.get('resume_court', ''),
                'score': int(decision['score']), 'themes': [theme],
                'source': link, 'publication_date': paper.get('publication_date', ''),
            }

    # On supprime les candidats uniquement APRÈS traitement complet du lot.
    consumed_ids = {id(p) for p in consumed}
    state['pending'] = [p for p in state['pending'] if id(p) not in consumed_ids]
    persist(state, cache, records, secondary_records)
    return len(reviewed), len(accepted_entries)


def main():
    state = clean_state(read_json(STATE_PATH, {}))
    cache = cachelib.read()
    records = read_json(RECORDS_PATH, {})
    secondary_records = read_json(SECONDARY_PATH, {})
    if state['completed']:
        print('Historique achevé :', state['month'])
        return
    if DRY_RUN:
        print('APERÇU historique :', json.dumps(
            {k: v for k, v in state.items() if k != 'pending'}, ensure_ascii=False))
        print('Aucune écriture. Pour importer : HISTORY_DRY_RUN=false.')
        return
    if not core.WRITE_NOTION:
        raise RuntimeError('Le mode historique réel exige WRITE_NOTION=true')

    titles, dois = core.existing_items()
    pages = 0
    total_analyzed = 0
    total_selected = 0
    print(f'Historique accéléré : maximum {MAX_PAGES} pages OpenAlex et '
          f'{MAX_EVAL} articles Gemini par lancement.')

    while pages < MAX_PAGES or state['pending']:
        remaining = MAX_EVAL - total_analyzed

        # Toujours vider les candidats déjà récupérés avant de lire une autre page.
        if state['pending']:
            if remaining <= 0:
                print(f'Budget Gemini atteint : {len(state["pending"])} candidat(s) en attente.')
                break
            analyzed, selected = process_pending(
                state, cache, records, secondary_records, titles, dois, remaining)
            total_analyzed += analyzed
            total_selected += selected
            if state['pending']:
                # Des candidats ont été laissés : attendre la prochaine exécution.
                break
            # Les articles ne sont pas perdus même si on a atteint le budget.
            if total_analyzed >= MAX_EVAL:
                break
            continue

        if pages >= MAX_PAGES or total_analyzed >= MAX_EVAL:
            break
        if state['last_page']:
            advance_query(state)
            persist(state, cache, records, secondary_records)
            if state['completed']:
                print('Recherche historique entièrement parcourue.')
                break

        count, found = fetch_page(state)
        pages += 1
        print(f'[{pages}/{MAX_PAGES}] {state["month"]} '
              f'requête {state["query_index"] + 1}/{len(all_queries())}: '
              f'{count} résultats, {found} candidat(s).')
        # Important : persister le curseur ET les candidats avant de les analyser.
        persist(state, cache, records, secondary_records)

    # Publication à partir des fiches, regroupées par date de publication.
    # Si cette étape échoue, records reste sauvegardé et pourra être republié.
    if records or secondary_records:
        publish_history.publish(records, secondary_records)
    print(f'Fin : {pages} page(s) lue(s), {total_analyzed} analysé(s), '
          f'{total_selected} retenu(s), {len(state["pending"])} en attente. '
          f'Pages cumulées : {state["pages_done"]}.')


if __name__ == '__main__':
    main()
