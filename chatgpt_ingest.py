#!/usr/bin/env python3
"""Importe les découvertes ChatGPT déposées dans inbox/chatgpt/*.json.

Le dépôt principal reste la source de vérité. Les fichiers inbox sont des
propositions, jamais des instructions exécutables ni des pages déjà publiées.
"""
import json
import os
import pathlib
import re
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import cache_v16
import publish_history
import publish_weekly as gh

INBOX = pathlib.Path('inbox/chatgpt')
PRIMARY = pathlib.Path('data/history_records.json')
SECONDARY = pathlib.Path('data/history_secondary_records.json')
STATE = pathlib.Path('data/chatgpt_ingest_state.json')
APPLY = os.getenv('CHATGPT_APPLY', 'false').lower() == 'true'
MAX_FILES = 30


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def normalized(title):
    raw = unicodedata.normalize('NFKD', str(title)).casefold()
    return ''.join(c for c in raw if c.isalnum())


def valid_url(value):
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or host in ('localhost', '127.0.0.1'):
        return False
    if host.endswith(('.notion.so', '.notion.com')) or host.startswith('127.'):
        return False
    return True


def validate(raw):
    if not isinstance(raw, dict) or raw.get('version') != 1:
        raise ValueError('Format attendu : {"version":1,"articles":[...]}')
    papers = raw.get('articles')
    if not isinstance(papers, list) or not 1 <= len(papers) <= 5:
        raise ValueError('Un lot doit comporter entre 1 et 5 articles')
    result = []
    for x in papers:
        if not isinstance(x, dict):
            raise ValueError('Article non structuré')
        title = str(x.get('title') or '').strip()
        link = str(x.get('url') or '').strip()
        summary = str(x.get('summary') or '').strip()
        score = x.get('score')
        if len(title) < 12 or len(title) > 350 or not valid_url(link):
            raise ValueError('Titre ou URL invalide')
        if type(score) is not int or not 60 <= score <= 100:
            raise ValueError('Score entier de 60 à 100 requis')
        if not summary or len(summary) > 5000:
            raise ValueError('Résumé factuel requis (maximum 5000 caractères)')
        raw_date = str(x.get('publication_date') or '').strip()
        precision = 'day'
        if re.fullmatch(r'\d{4}', raw_date):
            precision, stored_date = 'year', raw_date + '-01-01'
        elif re.fullmatch(r'\d{4}-\d{2}', raw_date):
            precision, stored_date = 'month', raw_date + '-01'
        else:
            stored_date = raw_date
        try:
            date.fromisoformat(stored_date)
        except ValueError as exc:
            raise ValueError('Date YYYY, YYYY-MM ou YYYY-MM-DD requise') from exc
        if not 2010 <= int(stored_date[:4]) <= date.today().year + 1:
            raise ValueError('Année incohérente')
        doi = str(x.get('doi') or '').strip()
        if doi:
            doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi, flags=re.I)
            if not re.fullmatch(r'10\.\d{4,9}/[^\s]{2,200}', doi):
                raise ValueError('DOI invalide')
        venue = str(x.get('venue') or '').strip()[:250]
        themes = x.get('themes') or []
        if not isinstance(themes, list):
            raise ValueError('themes doit être une liste')
        result.append({'title': title, 'source': link, 'summary': summary,
                       'score': score, 'doi': 'https://doi.org/' + doi if doi else '',
                       'publication_date': stored_date, 'publication_date_source': raw_date,
                       'publication_date_precision': precision,
                       'publication_venue': venue, 'venue_provenance': 'Vérification ChatGPT' if venue else '',
                       'themes': [str(t)[:90] for t in themes[:8]],
                       'publication_type': str(x.get('publication_type') or 'inconnu')[:40]})
    if len({normalized(x['title']) for x in result}) != len(result):
        raise ValueError('Doublons au sein du lot')
    return result


def notion_record(row):
    import collect as core
    paper = {'title': row['title'], 'doi': row['doi'],
             'publication_date': row['publication_date'],
             'primary_location': {'landing_page_url': row['source']},
             'authorships': [], 'abstract_inverted_index': core.indexed_abstract(row['summary']),
             'publication_venue': row['publication_venue']}
    themes = [x for x in core.THEMES if x[0][:3] in {y[:3] for y in row['themes']}]
    if not themes:
        themes = [core.THEMES[2]]
    record = {'paper': paper, 'themes': [name for name, _ in themes], 'sources': {'ChatGPT'}}
    decision = {'score': row['score'], 'themes': [name[:3] for name, _ in themes],
                'confiance': 'Évaluation fournie par ChatGPT',
                'justification': row['summary'][:700]}
    detail = {key: row['summary'][:700] if key in ('resume_court', 'interet')
              else 'Non renseigné dans les métadonnées vérifiées.'
              for key in ('resume_court', 'problematique', 'methode', 'resultats', 'limites', 'interet')}
    return core, record, decision, detail


def public_index(rows, existing):
    """Reconstruit seulement les tableaux ; conserve l'introduction et les règles."""
    def esc(s):
        return str(s or '').replace('|', '&#124;').replace('\n', ' ').replace('<', '&lt;')

    begin = '## Publications principales (75–100/100)'
    middle = '## Articles secondaires (60–74/100)'
    end = '## Principes de mise à jour'
    if not existing or not all(t in existing for t in (begin, middle, end)):
        raise ValueError('Structure de chatgpt/README.md inconnue ; publication arrêtée')

    before, remainder = existing.split(begin, 1)
    old_primary, remainder = remainder.split(middle, 1)
    old_secondary, after = remainder.split(end, 1)

    def table(filtered, old):
        lines = ['| Ajout | Score | Publication | Conférence / Revue | Fiche scientifique |',
                 '| --- | ---: | --- | --- | --- |']
        existing_lines = [line for line in old.splitlines()
                          if line.startswith('| ') and '[Fiche](' in line]
        # Préserver les lignes d'anciens imports, même si l'état privé a été
        # restauré depuis une sauvegarde incomplète.
        rendered_titles = {normalized(r['title']) for r in filtered}
        for line in existing_lines:
            match = re.search(r'\|\s*\[([^]]+)\]\(https?://', line)
            if not match or normalized(match.group(1)) not in rendered_titles:
                lines.append(line)
        for row in sorted(filtered, key=lambda r: (-r['score'], r['title'])):
            y, m = row['publication_date'][:7].split('-')
            archive = f'../historical/{y}/{m}.md'
            lines.append(f"| {esc(row['added_at'])} | {row['score']}/100 | "
                         f"[{esc(row['title'])}]({row['source'].replace(')', '%29')}) | "
                         f"{esc(row.get('publication_venue') or '—')} | [Fiche]({archive}) |")
        if len(lines) == 2:
            lines.append('| — | — | *Aucune découverte ChatGPT publiée pour le moment.* | — | — |')
        return '\n'.join(lines)

    return (before + begin + '\n\n' + table([x for x in rows.values() if x['score'] >= 75], old_primary)
            + '\n\n' + middle + '\n\n' + table([x for x in rows.values() if 60 <= x['score'] < 75], old_secondary)
            + '\n\n' + end + after)


def main():
    files = sorted(INBOX.glob('*.json'))[:MAX_FILES]
    if not files:
        print('Aucun lot ChatGPT en attente.')
        return
    state = load(STATE, {'processed': {}, 'published': {}})
    primary, secondary = load(PRIMARY, {}), load(SECONDARY, {})
    cache = cache_v16.read()
    all_rows = {**primary, **secondary}
    known_titles = {normalized(r.get('title', '')) for r in all_rows.values()}
    # Valider tous les lots avant la première écriture.
    batches = [(path, validate(load(path, {}))) for path in files]
    if not APPLY:
        for path, rows in batches:
            print(f'APERÇU {path}: {len(rows)} article(s) : ' + ', '.join(r['title'] for r in rows))
        return
    if not gh.GH_TOKEN:
        raise RuntimeError('PUBLIC_REPO_TOKEN requis en mode réel')
    import collect as core
    notion_titles, notion_dois = core.existing_items()
    schema = core.api(f'https://api.notion.com/v1/data_sources/{core.SOURCE_ID}').get('properties', {})
    for path, rows in batches:
        if str(path) in state['processed']:
            print('Lot déjà traité :', path)
            continue
        for row in rows:
            identity = cache_v16.key(row)
            title_norm = normalized(row['title'])
            alternate = cache_v16.key({'title': row['title']})
            already = (identity in all_rows or alternate in all_rows or title_norm in known_titles)
            if already:
                print('Déjà archivé :', row['title'])
                continue
            # Un article déjà dans Notion ne doit pas être recréé. Il reste
            # toutefois publiable en archive si une tentative s'était interrompue.
            doi_value = core.normalized_doi(row['doi'])
            notion_exists = core.norm(row['title']) in notion_titles or (doi_value and doi_value in notion_dois)
            if row['score'] >= 75 and not notion_exists:
                if not core.WRITE_NOTION:
                    raise RuntimeError('WRITE_NOTION doit être true')
                core_, record, decision, detail = notion_record(row)
                core_.notion_add(record, decision, detail, schema)
                notion_titles.add(core_.norm(row['title']))
                if doi_value:
                    notion_dois.add(doi_value)
                print('Notion créé :', row['title'])
            # Enregistrer immédiatement après l'écriture distante : un nouveau
            # lancement détectera la page Notion si la publication échoue ensuite.
            entry = {k: v for k, v in row.items() if k not in ('doi',) or v}
            target = primary if row['score'] >= 75 else secondary
            target[identity] = entry
            all_rows[identity] = entry
            known_titles.add(title_norm)
            cache_v16.set_status(cache, row, {'score': row['score'], 'categorie': 'coeur',
                                               'decision': 'conserver'}, 'imported')
            save(PRIMARY, primary)
            save(SECONDARY, secondary)
            cache_v16.write(cache)
            state['published'][identity] = {**entry, 'added_at': datetime.now(timezone.utc).date().isoformat()}
            save(STATE, state)
        state['processed'][str(path)] = datetime.now(timezone.utc).isoformat()
        save(STATE, state)
    # Les sorties publiques sont régénérées depuis les données persistantes.
    # Ne marquer aucune opération comme entièrement synchronisée avant le succès.
    if state['published']:
        months = {r['publication_date'][:7] for r in state['published'].values()}
        for ym in sorted(months):
            year, month = ym.split('-')
            normal = [r for r in primary.values() if r.get('publication_date', '')[:7] == ym]
            extra = [r for r in secondary.values() if r.get('publication_date', '')[:7] == ym]
            gh.put_file(f'historical/{year}/{month}.md',
                        publish_history.build_month(ym, normal, extra),
                        f'Bibliographie historique : {ym} (ChatGPT)')
        # Une seule mise à jour du tableau de bord, pas une centaine de PUT.
        months_count = {}
        for rec in list(primary.values()) + list(secondary.values()):
            ym = str(rec.get('publication_date') or '')[:7]
            if re.fullmatch(r'\d{4}-\d{2}', ym):
                months_count.setdefault(ym, 0)
        for rec in primary.values():
            ym = str(rec.get('publication_date') or '')[:7]
            if ym in months_count:
                months_count[ym] += 1
        readme, _ = gh.get_file('README.md')
        gh.put_file('README.md', publish_history.index_region(readme, months_count),
                    'Veille ChatGPT : actualiser les archives')
        current, _ = gh.get_file('chatgpt/README.md')
        output = public_index(state['published'], current)
        gh.put_file('chatgpt/README.md', output, 'Veille ChatGPT : index des publications')
        import dashboard_readme
        dashboard_readme.refresh(gh)
    print(f"FIN : {len(state['published'])} découverte(s) ChatGPT indexée(s) ; {len(state['processed'])} lot(s) pris en compte.")


if __name__ == '__main__':
    main()
