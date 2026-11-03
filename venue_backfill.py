"""Enrichissement ponctuel V16.8 des archives historiques — sans Gemini/Notion.

Exécution indépendante et progressive, avec cache de références examinées.
Aucune supposition de conférence d'après le seul titre de l'article.
"""
import json
import os
import pathlib

import publish_history
import venue_v168 as venue

RECORDS = pathlib.Path('data/history_records.json')
SECONDARY = pathlib.Path('data/history_secondary_records.json')
STATE = pathlib.Path('data/venue_backfill_progress.json')
LIMIT = max(1, min(100, int(os.environ.get('VENUE_BACKFILL_LIMIT', '20'))))
APPLY = os.environ.get('VENUE_BACKFILL_APPLY', 'false').casefold() == 'true'
FORCE = os.environ.get('VENUE_BACKFILL_FORCE', 'false').casefold() == 'true'
OPENALEX_API_KEY = os.environ.get('OPENALEX_API_KEY', '')
TITLE_LOOKUP = os.environ.get('VENUE_BACKFILL_TITLE_SEARCH', 'true').casefold() == 'true'


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)


def resolve(record):
    doi = venue.doi_from_record(record)
    result = venue.crossref_lookup(doi) if doi else None
    if result and result.get('publication_venue'):
        return result
    link = str(record.get('source') or '')
    openalex = venue.openalex_lookup(link, OPENALEX_API_KEY)
    if openalex and openalex.get('publication_venue'):
        return openalex
    if TITLE_LOOKUP:
        pub_year = str(record.get('publication_date') or '')[:4]
        exact = venue.openalex_title_lookup(record.get('title'), pub_year, OPENALEX_API_KEY)
        if exact:
            return exact
    return result or openalex or venue.identify_preprint_url(link)


def main():
    records = load(RECORDS, {})
    secondary = load(SECONDARY, {})
    state = load(STATE, {'checked': []})
    checked = set(state.get('checked') or [])
    candidates = [(f'principal:{key}', r) for key, r in sorted(records.items())]
    candidates += [(f'secondaire:{key}', r) for key, r in sorted(secondary.items())]
    # Les fiches ayant une conférence fiable sont préservées, sans écrasement.
    candidates = [(key, r) for key, r in candidates
                  if not r.get('publication_venue') and (FORCE or key not in checked)]
    print('ENRICHISSEMENT LIEUX DE PUBLICATION V16.8')
    print('Références restantes :', len(candidates), '| limite :', LIMIT,
          '| appliquer :', APPLY, '| forcer :', FORCE)
    processed = changed = 0
    for identity, record in candidates[:LIMIT]:
        try:
            metadata = resolve(record)
        except Exception as exc:
            # Ne jamais publier une référence devinée ou interrompre l'historique.
            print('ERREUR METADONNEE :', record.get('title', '')[:70], type(exc).__name__)
            continue
        processed += 1
        if metadata and (metadata.get('publication_venue') or metadata.get('publication_type') == 'prepublication'):
            print('TROUVÉ :', record.get('title', '')[:85], '=>', venue.display(metadata),
                  '|', metadata.get('venue_provenance'))
            if APPLY:
                record.update(metadata)
                changed += 1
        else:
            print('NON CONFIRMÉ :', record.get('title', '')[:100])
        if APPLY:
            checked.add(identity)
            state['checked'] = sorted(checked)
            save(STATE, state)
            save(RECORDS, records)
            save(SECONDARY, secondary)
    if not APPLY:
        print('APERÇU : aucune donnée modifiée, aucune publication GitHub.')
        return
    if changed:
        print('Mise à jour des fiches historiques et du README public...')
        publish_history.publish(records, secondary)
    else:
        print('Aucun changement bibliographique à publier.')
    print(f'FIN : {processed} références vérifiées, {changed} enrichies. '
          f'Restantes à examiner : {max(0, len(candidates)-processed)}.')


if __name__ == '__main__':
    main()
