"""V14 : publication hebdomadaire de la veille dans un dépôt GitHub PUBLIC.

Utilise Notion comme registre des nouveaux articles et le token GitHub à droits
limités uniquement pour écrire README.md et reports/YYYY/week-NN.md.
Aucun lien Notion privé ni secret n'est publié.
"""
import base64
import json
import pathlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '')
SOURCE_ID = os.environ.get('NOTION_DATA_SOURCE_ID', '')
GH_TOKEN = os.environ.get('PUBLIC_REPO_TOKEN', '')
REPO = os.environ.get('PUBLIC_REPO', 'Antoine310/scientific-watch-v2')
DRY_RUN = os.environ.get('PUBLIC_DRY_RUN', 'true').lower() != 'false'


def request_json(url, *, method='GET', payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)


def notion_text(props, key, kind='rich_text'):
    return ''.join(x.get('plain_text', '') for x in props.get(key, {}).get(kind, []))


def source_link(props):
    """Ne publier que les URL externes de publication (jamais une page Notion)."""
    link = props.get('Lien article', {}).get('url', '') or ''
    doi = notion_text(props, 'DOI').strip()
    if not link and doi:
        doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi, flags=re.I)
        link = 'https://doi.org/' + urllib.parse.quote(doi, safe='/')
    parsed = urllib.parse.urlparse(link)
    host = (parsed.hostname or '').lower()
    forbidden = ('notion.so', 'notion.com', 'localhost')
    if parsed.scheme not in ('https', 'http') or not host or any(host == h or host.endswith('.' + h) for h in forbidden):
        return ''
    return link


def get_articles():
    if not NOTION_TOKEN or not SOURCE_ID:
        raise RuntimeError('Secrets Notion manquants')
    # Semaine ISO en cours : lundi à dimanche (rapport du dimanche).
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).date()
    since = monday
    until = monday + timedelta(days=7)
    url = f'https://api.notion.com/v1/data_sources/{SOURCE_ID}/query'
    headers = {'Authorization': f'Bearer {NOTION_TOKEN}', 'Notion-Version': '2025-09-03',
               'Content-Type': 'application/json'}
    rows, cursor = [], None
    while True:
        data = {'page_size': 100, 'filter': {'and': [
            {'property': 'Date de détection', 'date': {'on_or_after': since.isoformat()}},
            {'property': 'Date de détection', 'date': {'before': until.isoformat()}}
        ]}}
        if cursor:
            data['start_cursor'] = cursor
        result = request_json(url, method='POST', payload=data, headers=headers)
        for item in result.get('results', []):
            p = item.get('properties', {})
            title = notion_text(p, 'Titre', 'title').strip()
            if not title:
                continue
            rows.append({'title': title, 'summary': notion_text(p, 'Résumé court'),
                         'score': p.get('Pertinence', {}).get('number'),
                         'source': source_link(p),
                         'themes': [x.get('name','') for x in p.get('Thèmes', {}).get('multi_select', [])],
                         'date': (p.get('Date de détection', {}).get('date') or {}).get('start', '')})
        cursor = result.get('next_cursor')
        if not cursor:
            break
    # L'historique possède sa propre archive publique : ne pas le mélanger
    # avec les nouveautés hebdomadaires, même importées aujourd'hui.
    archive = pathlib.Path('data/history_records.json')
    if archive.exists():
        historical = json.loads(archive.read_text(encoding='utf-8'))
        old_titles = {re.sub(r'\W+', '', v.get('title', '').casefold()) for v in historical.values()}
        rows = [r for r in rows if re.sub(r'\W+', '', r['title'].casefold()) not in old_titles]
    # Une seule entrée par titre : garde-fou supplémentaire pour le rapport.
    unique = {re.sub(r'\W+', '', r['title'].casefold()): r for r in rows}
    return sorted(unique.values(), key=lambda r: r['score'] or 0, reverse=True), since


def md_escape(s):
    return str(s or '').replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')


def build_report(rows, since):
    year, week, _ = since.isocalendar()
    end = since + timedelta(days=6)
    lines = [f'# Veille scientifique — Semaine {week:02d} ({year})', '',
             f'Période de détection : **{since:%d/%m/%Y} – {end:%d/%m/%Y}**', '',
             f'**{len(rows)} publication(s)** retenue(s) par la veille.', '',
             '> Les scores indiquent la pertinence estimée pour une thèse sur les RoT, TEE RISC-V et canaux auxiliaires, et non la qualité scientifique absolue.', '']
    for idx, row in enumerate(rows, 1):
        lines += [f"## {idx}. {md_escape(row['title'])}", '',
                  f"**Pertinence indicative :** {row['score']}/100" if row['score'] is not None else '**Pertinence :** non renseignée',
                  f"**Thèmes :** {', '.join(md_escape(x) for x in row['themes'])}" if row['themes'] else '**Thèmes :** non renseignés', '',
                  md_escape(row['summary']) or '*Résumé non disponible dans les métadonnées.*', '']
        if row['source']:
            lines += [f"[Consulter la publication originale]({row['source']})", '']
        else:
            lines += ['*Lien de publication original non disponible ; aucun lien Notion privé publié.*', '']
    return '\n'.join(lines).rstrip() + '\n', year, week


def github_headers():
    return {'Authorization': f'Bearer {GH_TOKEN}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}


def get_file(path):
    url = f'https://api.github.com/repos/{REPO}/contents/{path}'
    try:
        result = request_json(url, headers=github_headers())
        raw = base64.b64decode(result['content']).decode('utf-8')
        return raw, result['sha']
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def put_file(path, content, message):
    current, sha = get_file(path)
    if current == content:
        print('Déjà à jour :', path)
        return
    payload = {'message': message, 'content': base64.b64encode(content.encode()).decode()}
    if sha:
        payload['sha'] = sha
    request_json(f'https://api.github.com/repos/{REPO}/contents/{path}',
                 method='PUT', payload=payload, headers=github_headers())
    print('Publié :', path)


def update_index(existing, year, week, count):
    # Préserver le README existant en ne modifiant qu'une section délimitée.
    begin, end = '<!-- SCIENTIFIC-WATCH-REPORTS:START -->', '<!-- SCIENTIFIC-WATCH-REPORTS:END -->'
    path = f'reports/{year}/week-{week:02d}.md'
    line = f'- **{year} — semaine {week:02d}** : [{count} publication(s)]({path})'
    template = (existing or '# Scientific Watch — Rapports scientifiques\n\n'
                'Sélection hebdomadaire de publications sur les RoT matérielles, TEE RISC-V et canaux auxiliaires.\n\n')
    if begin in template and end in template:
        pre, rest = template.split(begin, 1)
        region, post = rest.split(end, 1)
        entries = [x for x in region.splitlines() if x.startswith('- **') and f'**{year} — semaine {week:02d}**' not in x]
        entries.insert(0, line)
        return pre + begin + '\n' + '\n'.join(entries) + '\n' + end + post
    return template.rstrip() + '\n\n## Rapports hebdomadaires\n\n' + begin + '\n' + line + '\n' + end + '\n'


def main():
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPO):
        raise RuntimeError('PUBLIC_REPO invalide')
    rows, since = get_articles()
    report, year, week = build_report(rows, since)
    path = f'reports/{year}/week-{week:02d}.md'
    print(f'Rapport préparé : {path} ({len(rows)} articles).')
    if DRY_RUN:
        print('MODE APERÇU : aucune publication GitHub. Aperçu :\n', report[:2500])
        return
    if not GH_TOKEN:
        raise RuntimeError('Secret PUBLIC_REPO_TOKEN manquant')
    old_readme, _ = get_file('README.md')
    # Générer d'abord le rapport, puis son index.
    put_file(path, report, f'Veille scientifique : rapport semaine {week:02d}/{year}')
    put_file('README.md', update_index(old_readme, year, week, len(rows)),
             f'Actualiser index rapports semaine {week:02d}/{year}')


if __name__ == '__main__':
    main()
