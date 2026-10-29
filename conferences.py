"""Calendrier des conférences pour Scientific Watch (indépendant de la veille).

Sources communautaires : conference-deadlines et sec-deadlines.
Les rangs sont fournis à titre indicatif : vérifier dans le portail ICORE.
Ne publie ni jeton ni document privé. Ne modifie que conferences/README.md.
"""
from __future__ import annotations

import base64
from datetime import datetime, date, timezone
import json
import os
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import yaml

REPO = os.environ.get('PUBLIC_REPO', 'Antoine310/scientific-watch-v2')
TOKEN = os.environ.get('PUBLIC_REPO_TOKEN', '')
DRY_RUN = os.environ.get('CONFERENCES_DRY_RUN', 'true').lower() != 'false'
CURRENT_YEAR = datetime.now(ZoneInfo('Europe/Paris')).year

FEEDS = [
    ('research', 'https://raw.githubusercontent.com/conference-deadlines/conference-deadlines.github.io/main/_data/conferences.yml'),
    ('security', 'https://raw.githubusercontent.com/sec-deadlines/sec-deadlines.github.io/master/_data/conferences.yml'),
]

# Conférences retenues : sélection par domaine, sans score de pertinence.
# Les variantes permettent de reconnaître les libellés différents des deux flux.
GROUPS = {
    'Sécurité matérielle et systèmes embarqués': {
        'HOST': ['HOST', 'IEEE HOST'], 'CHES': ['CHES', 'TCHES'],
        'AsianHOST': ['AsianHOST'], 'CARDIS': ['CARDIS'],
    },
    'Architecture et microarchitecture': {
        'ISCA': ['ISCA'], 'MICRO': ['MICRO'], 'ASPLOS': ['ASPLOS'],
        'HPCA': ['HPCA'], 'EuroSys': ['EuroSys'],
    },
    'Sécurité et confidentialité': {
        'IEEE S&P': ['S&P (Oakland)', 'IEEE S&P', 'IEEE SP', 'S&P'],
        'USENIX Security': ['USENIX Security', 'USENIX Sec', 'USENIX'],
        'ACM CCS': ['CCS', 'ACM CCS'], 'NDSS': ['NDSS'],
        'EuroS&P': ['Euro S&P', 'EuroS&P', 'EuroSP'], 'ACSAC': ['ACSAC'],
    },
    'Cryptographie': {
        'CRYPTO': ['CRYPTO'], 'EUROCRYPT': ['EUROCRYPT'],
        'ASIACRYPT': ['ASIACRYPT'],
    },
    'Conception électronique et systèmes sur puce': {
        'DAC': ['DAC'], 'DATE': ['DATE'], 'ICCAD': ['ICCAD'],
        'FPGA': ['FPGA'], 'FPL': ['FPL'],
    },
}


def get_text(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'scientific-watch-conferences/1.0', 'Accept': 'application/yaml,text/plain,*/*'})
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.read().decode('utf-8-sig')


def normalize(s):
    return re.sub(r'[^a-z0-9]+', '', str(s or '').casefold())


def identify(name):
    k = normalize(name)
    for group, items in GROUPS.items():
        for canonical, aliases in items.items():
            if k in {normalize(v) for v in aliases}:
                return group, canonical
    return None


def get_year(item):
    try:
        return int(item.get('year'))
    except (ValueError, TypeError):
        return 0


def rank(item):
    """Rangs dérivés d'un flux communautaire, jamais présentés comme ICORE vérifié."""
    raw = str(item.get('rank') or item.get('core_rank') or item.get('core') or '').upper().strip()
    tags = [str(x).upper() for x in (item.get('tags') or [])]
    if raw in {'A*', 'A', 'B', 'C'}:
        return raw
    for tag, label in [('ASTAR', 'A*'), ('CORE-A*', 'A*'), ('CORE-A', 'A'), ('CORE-B', 'B'), ('CORE-C', 'C')]:
        if tag in tags:
            return label
    return None


def timestamps(item, field):
    """Garde les échéances avec fuseau explicite, sans inventer de timezone."""
    value = item.get(field)
    values = value if isinstance(value, list) else [value] if value else []
    out = []
    for v in values:
        if isinstance(v, dict):
            v = v.get('date') or v.get('deadline')
        if isinstance(v, datetime):
            dt = v
        else:
            s = str(v or '').strip()
            if not s or '%' in s or s.upper() in {'TBA', 'TBD'}:
                # Les modèles de dates sec-deadlines sont traités séparément.
                if '%' in s:
                    y = get_year(item)
                    if y:
                        s = s.replace('%Y', str(y - 1)).replace('%y', str(y))
                    else:
                        continue
                else:
                    continue
            try:
                dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            except ValueError:
                try:
                    dt = datetime.strptime(s[:16], '%Y-%m-%d %H:%M')
                except ValueError:
                    try:
                        dt = datetime.strptime(s[:10], '%Y-%m-%d')
                    except ValueError:
                        continue
        if dt.tzinfo is None:
            # Convention propre au flux : AoE par défaut pour sec-deadlines.
            tz = item.get('timezone') or ('Etc/GMT+12' if item.get('_feed') == 'security' else None)
            try:
                dt = dt.replace(tzinfo=ZoneInfo(tz)) if tz else None
            except (KeyError, ValueError):
                dt = None
        if dt is not None:
            out.append(dt.astimezone(ZoneInfo('Europe/Paris')))
    return sorted(set(out))


def dates(item, *fields):
    result = []
    for field in fields:
        result += timestamps(item, field)
    return sorted(set(result))


def as_date(dt):
    return dt.strftime('%d/%m/%Y') if dt else 'À confirmer'


def escape(s):
    return str(s or '').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ').strip()


def safe_url(s):
    from urllib.parse import urlparse
    u = str(s or '').strip()
    p = urlparse(u)
    if p.scheme != 'https' or not p.hostname:
        return ''
    if p.hostname.endswith(('.notion.so', '.notion.com')) or p.hostname in {'notion.so', 'notion.com'}:
        return ''
    return u.replace(')', '%29')


def parse_feed(text, feed):
    body = yaml.safe_load(text)
    # Certaines versions des projets exposent une liste, d'autres un conteneur.
    if isinstance(body, dict):
        body = body.get('conferences') or body.get('data') or []
    if not isinstance(body, list):
        raise ValueError(f'Format YAML inattendu pour {feed}')
    return [dict(item, _feed=feed) for item in body if isinstance(item, dict)]


def load_all(fetch=get_text):
    combined = []
    success = 0
    for feed, url in FEEDS:
        try:
            result = parse_feed(fetch(url), feed)
            print(f'Flux {feed} : {len(result)} entrées')
            combined.extend(result)
            success += 1
        except Exception as exc:
            print(f'AVERTISSEMENT : flux {feed} indisponible ({type(exc).__name__}: {exc})')
    if not success:
        raise RuntimeError('Aucune source de conférences disponible ; ne pas remplacer le rapport existant.')
    return combined


def selected(items, today=None):
    today = today or datetime.now(ZoneInfo('Europe/Paris')).date()
    # Garder l'édition en cours + les deux années suivantes ; ne pas afficher des éditions anciennes.
    lookup = {}
    for item in items:
        found = identify(item.get('name', ''))
        year = get_year(item)
        if not found or not (today.year <= year <= today.year + 2):
            continue
        group, name = found
        key = (group, name, year)
        current = lookup.get(key)
        # Préférer l'entrée qui contient des dates et un lien officiel,
        # puis la source généraliste (résumé et article distincts).
        info = dates(item, 'abstract_deadline', 'abstract_deadlines')
        papers = dates(item, 'deadline', 'deadlines', 'paper_deadline', 'paper_deadlines')
        quality = (bool(info) + bool(papers) + bool(item.get('date')) + bool(safe_url(item.get('link') or item.get('url'))), item.get('_feed') == 'research')
        if current is None or quality > current[0]:
            lookup[key] = (quality, item)
    # Pas de supposition de date pour les conférences sans édition répertoriée.
    return [(g, n, y, v[1]) for (g, n, y), v in lookup.items()]


def build(rows, today=None):
    today = today or datetime.now(ZoneInfo('Europe/Paris')).date()
    lines = ['# 📅 Conférences scientifiques — échéances et classements', '',
             'Domaines : sécurité matérielle, architecture, cybersécurité, cryptographie et conception électronique.', '',
             '> **Attention :** dates provenant de calendriers communautaires. Vérifier impérativement les échéances sur le site officiel avant toute soumission. Les rangs CORE affichés sont **indicatifs** et ne constituent pas une vérification du classement ICORE 2026.', '',
             f'**Actualisé le :** {today:%d/%m/%Y} · **Fuseau horaire des échéances :** Europe/Paris (conversion lorsque le flux fournit un fuseau).', '',
             '[Consulter le classement officiel ICORE](https://portal.core.edu.au/conf-ranks/?by=all) · '
             '[Research Conference Deadlines](https://conference-deadlines.github.io/) · '
             '[Security Deadlines](https://sec-deadlines.github.io/)', '']
    upcoming = []
    for group, name, year, item in rows:
        abstract = dates(item, 'abstract_deadline', 'abstract_deadlines')
        papers = dates(item, 'deadline', 'deadlines', 'paper_deadline', 'paper_deadlines')
        # Plusieurs rounds peuvent exister, pas de déduction à partir de comment.
        future = [(d, 'Résumé' if d in abstract else 'Article') for d in abstract + papers if d.date() >= today]
        if future:
            dt, kind = min(future, key=lambda p: p[0])
            upcoming.append((dt, name, year, kind, item))
    lines += ['## ⏳ Prochaines échéances annoncées', '',
              '| Conférence | Échéance | Type | Site officiel |', '|---|---|---|---|']
    if not upcoming:
        lines.append('| *Aucune échéance future renseignée* | — | — | — |')
    for deadline, name, year, kind, item in sorted(upcoming)[:40]:
        link = safe_url(item.get('link') or item.get('url'))
        lines.append(f'| {escape(name)} {year} | {as_date(deadline)} | {kind} | ' + (f'[Site]({link})' if link else 'À confirmer') + ' |')
    lines.append('')
    priority = {'A*': 0, 'A': 1, 'B': 2, 'C': 3, None: 4}
    for group in GROUPS:
        subset = [(n, y, i) for g, n, y, i in rows if g == group]
        if not subset:
            continue
        subset.sort(key=lambda t: (priority[rank(t[2])], t[0].casefold(), -t[1]))
        lines += [f'## {group}', '',
                  '| Conférence | Rang CORE¹ | Résumé | Article complet | Dates de la conférence | Lieu | Site |',
                  '|---|---|---|---|---|---|---|']
        for name, year, item in subset:
            abstract = dates(item, 'abstract_deadline', 'abstract_deadlines')
            papers = dates(item, 'deadline', 'deadlines', 'paper_deadline', 'paper_deadlines')
            fmt = lambda ds: ', '.join(as_date(d) for d in ds) if ds else 'À confirmer'
            url = safe_url(item.get('link') or item.get('url'))
            lines.append('| ' + ' | '.join([
                f'{escape(name)} {year}', rank(item) or 'Non renseigné',
                fmt(abstract), fmt(papers), escape(item.get('date')) or 'À confirmer',
                escape(item.get('place') or item.get('location')) or 'À confirmer',
                f'[Officiel]({url})' if url else 'À confirmer']) + ' |')
        lines.append('')
    lines += ['---', '',
              '¹ **Classement CORE indicatif** transmis par les flux communautaires, **non vérifié automatiquement contre ICORE 2026**. Une conférence non renseignée n’est pas nécessairement de moindre qualité.', '',
              '**Informations manquantes :** « À confirmer » signifie que le flux ne permet pas de fournir une donnée fiable ; cela ne signifie pas qu’aucun appel à communications n’existe.', '',
              '**Mises à jour :** chaque lundi, sans notification Gmail. La veille scientifique et Notion ne sont pas modifiés.', '']
    return '\n'.join(lines)


def gh_json(path, method='GET', payload=None):
    if not TOKEN:
        raise RuntimeError('Secret PUBLIC_REPO_TOKEN absent')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPO):
        raise ValueError('PUBLIC_REPO invalide')
    url = f'https://api.github.com/repos/{REPO}/contents/{path}'
    headers = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
               'User-Agent': 'scientific-watch-conferences'}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)


def publish(content):
    path = 'conferences/README.md'
    try:
        existing = gh_json(path)
        sha = existing['sha']
        previous = base64.b64decode(existing['content']).decode('utf-8')
        if previous == content:
            print('Calendrier inchangé, aucun commit nécessaire.')
            return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        sha = None
    payload = {'message': 'Mise à jour hebdomadaire du calendrier des conférences',
               'content': base64.b64encode(content.encode('utf-8')).decode('ascii')}
    if sha:
        payload['sha'] = sha
    gh_json(path, method='PUT', payload=payload)
    print('Publié : ' + path)


def main():
    rows = selected(load_all())
    if not rows:
        raise RuntimeError('Aucune conférence reconnue ; publication annulée.')
    print(f'{len(rows)} éditions de conférences sélectionnées')
    content = build(rows)
    if DRY_RUN:
        print('MODE APERÇU : aucune publication GitHub. Début du calendrier :')
        print('\n'.join(content.splitlines()[:18]))
        return
    publish(content)


if __name__ == '__main__':
    main()
