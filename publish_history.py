"""Publie les archives mensuelles dans le dépôt PUBLIC, sans liens Notion."""
import os
import re
from collections import defaultdict
from datetime import date
from urllib.parse import urlparse

import publish_weekly as gh


def safe_url(link):
    parsed = urlparse(str(link or ''))
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or not host:
        return ''
    if host in {'localhost', '127.0.0.1'} or host.endswith('.notion.so') or host.endswith('.notion.com'):
        return ''
    return link.replace(')', '%29')


def escape(value):
    return str(value or '').replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('<', '&lt;').replace('>', '&gt;')


def build_month(year_month, rows, secondary_rows=None):
    lines = [f'# Bibliographie historique — {year_month}', '',
             '> Sélection progressive : uniquement les travaux détectés par le rattrapage historique OpenAlex.',
             '> Les scores expriment la pertinence pour la thèse, pas une évaluation de qualité absolue.',
             '', f'**{len(rows)} publication(s)**', '']
    for row in sorted(rows, key=lambda x: (-int(x.get('score') or 0), x.get('title', ''))):
        lines.extend([f"## {escape(row['title'])}", '',
                      f"- **Date de publication :** {escape(row.get('publication_date'))}",
                      f"- **Pertinence :** {row.get('score', '?')}/100", '',
                      escape(row.get('summary') or 'Résumé indisponible.'), ''])
        link = safe_url(row.get('source'))
        if link:
            lines.extend([f'[Lire la publication]({link})', ''])
    secondary_rows = secondary_rows or []
    if secondary_rows:
        lines.extend(['---', '', '## 📖 Articles secondaires (60–74/100)', '',
                      'Références techniques complémentaires non retenues dans la sélection principale.', '',
                      '| Score | Publication | Lien |', '| ---: | --- | --- |'])
        for row in sorted(secondary_rows, key=lambda x: (-int(x.get('score') or 0), x.get('title', ''))):
            link = safe_url(row.get('source'))
            title = escape(row.get('title', 'Sans titre')).replace('|', '&#124;').replace('\n', ' ')
            linked = f'[Lire]({link})' if link else '—'
            lines.append(f"| {int(row.get('score') or 0)}/100 | {title} | {linked} |")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def index_region(existing, months):
    begin, end = '<!-- SCIENTIFIC-WATCH-HISTORY:START -->', '<!-- SCIENTIFIC-WATCH-HISTORY:END -->'
    lines = ['## Bibliographie historique (depuis janvier 2025)', '',
             'Ces archives sont enrichies progressivement. Le rattrapage OpenAlex n’est pas exhaustif.', '']
    for ym, count in sorted(months.items(), reverse=True):
        year, month = ym.split('-')
        path = f'historical/{year}/{month}.md'
        lines.append(f'- [{ym} — {count} publication(s)]({path})')
    fragment = '\n'.join(lines) + '\n'
    if existing and begin in existing and end in existing:
        pre, rest = existing.split(begin, 1)
        _, post = rest.split(end, 1)
        return pre + begin + '\n' + fragment + end + post
    return (existing or '# Scientific Watch\n').rstrip() + '\n\n' + begin + '\n' + fragment + end + '\n'


def publish(records, secondary_records=None):
    if not gh.GH_TOKEN:
        raise RuntimeError('PUBLIC_REPO_TOKEN manquant')
    grouped = defaultdict(list)
    secondary_grouped = defaultdict(list)
    for rec in records.values():
        publication_date = str(rec.get('publication_date') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', publication_date):
            continue
        grouped[publication_date[:7]].append(rec)
    for rec in (secondary_records or {}).values():
        publication_date = str(rec.get('publication_date') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', publication_date):
            continue
        secondary_grouped[publication_date[:7]].append(rec)
    for ym in sorted(set(grouped) | set(secondary_grouped)):
        rows = grouped.get(ym, [])
        yyyy, mm = ym.split('-')
        path = f'historical/{yyyy}/{mm}.md'
        gh.put_file(path, build_month(ym, rows, secondary_grouped.get(ym, [])), f'Bibliographie historique : {ym}')
    old, _ = gh.get_file('README.md')
    gh.put_file('README.md', index_region(old, {ym: len(grouped.get(ym, [])) for ym in set(grouped) | set(secondary_grouped)}),
                'Mise à jour index historique')
