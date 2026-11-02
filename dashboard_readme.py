"""Tableau de bord du dépôt public, fondé sur les commits GitHub.

Ne publie aucune donnée privée. Les dates indiquent l'ajout au GitHub public,
non la date de parution scientifique. Aucune analyse Gemini.
"""
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

BEGIN = '<!-- SCIENTIFIC-WATCH-DASHBOARD:START -->'
END = '<!-- SCIENTIFIC-WATCH-DASHBOARD:END -->'


def _escape(s):
    return str(s or '').replace('|', '&#124;').replace('[', '\\[').replace(']', '\\]').replace('\n', ' ').strip()


def _kind(path):
    if re.fullmatch(r'reports/\d{4}/week-\d{2}\.md', path):
        return 'Rapport hebdomadaire'
    if re.fullmatch(r'historical/\d{4}/\d{2}\.md', path):
        return 'Bibliographie historique'
    if path == 'conferences/README.md':
        return 'Conférences'
    if path.startswith('scholar/') and path.endswith('.md'):
        return 'Google Scholar'
    return None


def _extract_new_titles(patch, path):
    """N'extrait que les titres véritablement ajoutés dans le diff d'un commit."""
    if not patch:
        return []
    titles = []
    for raw in patch.splitlines():
        if not raw.startswith('+') or raw.startswith('+++'):
            continue
        line = raw[1:].strip()
        # Titres des rapports hebdomadaires ou de l'historique.
        match = re.match(r'^##\s+(?:\d+\.\s*)?(.+)$', line)
        if match:
            title = match.group(1).strip()
            # Ignore les intitulés de sections (notamment Scholar avec emojis).
            normalized = re.sub(r'[^a-z0-9 ]+', ' ', title.casefold())
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            headings = ('articles secondaires', 'publications principales', 'resume',
                        'table des matieres', 'dernieres publications', 'bibliographie historique')
            if title and not any(heading in normalized for heading in headings):
                titles.append(title)
            continue
        # Tableau des articles secondaires : | 65/100 | titre | lien |
        m = re.match(r'^\|\s*\d{1,3}/100\s*\|\s*(.+?)\s*\|', line)
        if m:
            titles.append(m.group(1).strip())
    return titles


def get_activity(gh, *, days=7, max_commits=100, max_details=60):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    query = urllib.parse.urlencode({'since': since, 'per_page': max_commits})
    commits = gh.request_json(f'https://api.github.com/repos/{gh.REPO}/commits?{query}', headers=gh.github_headers())
    if not isinstance(commits, list):
        return [], []
    changes, articles, seen_changes, seen_articles = [], [], set(), set()
    for c in commits[:max_details]:
        sha = c.get('sha', '')
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            continue
        date = ((c.get('commit') or {}).get('committer') or {}).get('date', '')[:10]
        if not date:
            continue
        detail = gh.request_json(f'https://api.github.com/repos/{gh.REPO}/commits/{sha}', headers=gh.github_headers())
        for f in detail.get('files', []):
            path = f.get('filename', '')
            kind = _kind(path)
            if not kind:
                continue
            if (date, path) not in seen_changes:
                changes.append((date, kind, path))
                seen_changes.add((date, path))
            if kind == 'Conférences':
                continue
            for title in _extract_new_titles(f.get('patch', ''), path):
                key = re.sub(r'\W+', '', title.casefold())
                if not key or key in seen_articles:
                    continue
                articles.append((date, title, kind, path))
                seen_articles.add(key)
    return changes[:5], articles


def build_fragment(changes, articles):
    lines = [
        '## Accès rapide', '',
        '[Rapports hebdomadaires](reports/) · [Bibliographie historique](historical/) · '
        '[Conférences](conferences/README.md) · [Google Scholar](scholar/README.md)', '',
        '## Dernières publications ajoutées', '',
              '> Dates d’ajout sur GitHub (et non dates de publication scientifique). '
              'Liste fondée sur les ajouts visibles dans les commits récents.', '']
    recent = [a for a in articles if a[2] in ('Google Scholar', 'Rapport hebdomadaire')][:5]
    historical = [a for a in articles if a[2] == 'Bibliographie historique'][:5]

    def add_table(title, items):
        lines.extend([f'### {title}', ''])
        if not items:
            lines.extend(['Aucune nouvelle publication identifiable cette semaine.', ''])
            return
        lines.extend(['| Ajout | Publication | Source |', '| --- | --- | --- |'])
        for date, article_title, kind, path in items:
            lines.append(f'| {date} | [{_escape(article_title)}]({path}) | {_escape(kind)} |')
        lines.append('')

    add_table('Google Scholar et veille scientifique', recent)
    add_table('Bibliographie historique', historical)
    return '\n'.join(lines).rstrip() + '\n'


def update_readme(existing, fragment):
    existing = existing or '# scientific-watch-v2\n'
    block = BEGIN + '\n' + fragment + END
    if BEGIN in existing and END in existing:
        pre, tail = existing.split(BEGIN, 1)
        _, post = tail.split(END, 1)
        return pre + block + post
    title = re.match(r'^(#[^\n]*\n)', existing)
    if title:
        return title.group(1) + '\n' + block + '\n\n' + existing[title.end():].lstrip('\n')
    return '# scientific-watch-v2\n\n' + block + '\n\n' + existing


def refresh(gh):
    old, _ = gh.get_file('README.md')
    try:
        changes, articles = get_activity(gh)
        fragment = build_fragment(changes, articles)
    except Exception as exc:
        # Ne pas effacer un ancien tableau de bord si l'API des commits est indisponible.
        print('Tableau de bord : récupération des commits impossible :', exc)
        if old and BEGIN in old and END in old:
            return
        fragment = build_fragment([], [])
    new = update_readme(old, fragment)
    gh.put_file('README.md', new, 'Mise à jour du tableau de bord scientifique')


if __name__ == '__main__':
    import publish_weekly as gh
    if not gh.GH_TOKEN:
        raise SystemExit('PUBLIC_REPO_TOKEN manquant')
    refresh(gh)
