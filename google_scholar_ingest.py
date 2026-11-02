#!/usr/bin/env python3
"""Scientific Watch V16.4 — Import prudent des alertes Google Scholar depuis Gmail.

Aperçu par défaut. Aucun appel OpenAlex : Gemini évalue le titre repéré
dans les alertes Scholar. Les dates manquantes ne sont pas inventées. Ne modifie pas Gmail.
"""
import email
import hashlib
import html
import imaplib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser

STATE_FILE = pathlib.Path('data/scholar_mail_state.json')
REVIEW_FILE = pathlib.Path('data/scholar_review.json')
PRIMARY_FILE = pathlib.Path('data/history_records.json')
SECONDARY_FILE = pathlib.Path('data/history_secondary_records.json')
SCHOLAR_FILE = pathlib.Path('data/scholar_records.json')
APPLY = os.getenv('SCHOLAR_APPLY', 'false').lower() == 'true'
MAIL_LIMIT = max(1, min(100, int(os.getenv('SCHOLAR_MAIL_LIMIT', '20'))))
ARTICLE_LIMIT = max(1, min(25, int(os.getenv('SCHOLAR_ARTICLE_LIMIT', '10'))))
LOOKBACK = max(1, min(3650, int(os.getenv('SCHOLAR_LOOKBACK_DAYS', '45'))))
SENDER = 'scholaralerts-noreply@google.com'


def load(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_name(path.name + '.tmp')
    target.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    target.replace(path)


def normalized(text):
    text = html.unescape(str(text or '')).casefold()
    return re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).strip()


def decoded(value):
    try:
        return str(make_header(decode_header(value or '')))
    except (ValueError, UnicodeError):
        return str(value or '')


def safe_link(url):
    url = html.unescape(url or '').strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return ''
    host = (parsed.hostname or '').lower()
    if not host or host in {'localhost', '127.0.0.1'} or host.endswith('.notion.so'):
        return ''
    if host.endswith('.google.com') and parsed.path in ('/scholar_url', '/url'):
        for k in ('url', 'q'):
            found = urllib.parse.parse_qs(parsed.query).get(k, [])
            if found:
                return safe_link(found[0])
    return url


class AnchorReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.href = None
        self.parts = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and self.href is None:
            self.href = dict(attrs).get('href', '')
            self.parts = []
            self.depth = 1
        elif tag == 'a' and self.href is not None:
            self.depth += 1

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.depth -= 1
            if self.depth == 0:
                self.links.append((' '.join(''.join(self.parts).split()), self.href))
                self.href = None
                self.parts = []


def extract_candidates(message):
    """Get likely paper-title anchors, rejecting navigation/unsubscribe links."""
    pieces = message.walk() if message.is_multipart() else [message]
    links = []
    for part in pieces:
        if part.get_content_type() != 'text/html':
            continue
        try:
            raw = part.get_payload(decode=True) or b''
            charset = part.get_content_charset() or 'utf-8'
            reader = AnchorReader()
            reader.feed(raw.decode(charset, errors='replace'))
            links.extend(reader.links)
        except (LookupError, UnicodeError):
            continue
    found, seen = [], set()
    for title, url in links:
        title = title.strip()
        link = safe_link(url)
        host = (urllib.parse.urlparse(link).hostname or '').lower()
        bad = ('unsubscribe', 'désabonner', 'create alert', 'modifier l alerte',
               'view all', 'toutes les alertes', 'citation', 'cited by', 'articles connexes',
               'related articles', 'google scholar', 'scholar alerts', 'alerte google',
               'privacy', 'confidentialité')
        if (len(title) < 24 or len(title) > 350 or not link or
                (host.endswith('.google.com') and '/scholar' in urllib.parse.urlparse(link).path) or
                any(term in normalized(title) for term in bad)):
            continue
        key = normalized(title)
        if key and key not in seen:
            seen.add(key)
            found.append({'title': title, 'url': link})
    return found


def doi_from_link(link):
    parsed = urllib.parse.urlparse(link)
    if (parsed.hostname or '').lower() in ('doi.org', 'dx.doi.org'):
        return urllib.parse.unquote(parsed.path.lstrip('/'))
    match = re.search(r'10\.\d{4,9}/[^?\s&#]+', urllib.parse.unquote(link), re.I)
    return match.group(0).rstrip('.,;') if match else ''


def scholar_paper(item):
    """OpenAlex-compatible minimal data, with NO fabricated date or abstract."""
    doi = doi_from_link(item['url'])
    return {
        'id': item['url'], 'title': item['title'],
        'doi': 'https://doi.org/' + doi if doi else None,
        'publication_date': None,
        'authorships': [], 'abstract_inverted_index': None,
        'primary_location': {'landing_page_url': item['url']},
    }


def scholar_markdown(entries):
    """For undated work, an honest separate public page avoids wrong month placement."""
    lines = ['# Publications découvertes via Google Scholar', '',
             '> Pertinence évaluée par Gemini sur le titre disponible dans l’alerte.',
             '> Sans résumé de l’article, cette évaluation a une confiance limitée.',
             '> Date de publication inconnue : aucune date n’est déduite de l’e-mail.', '']
    for group, label in (('primary', '⭐ Publications principales'), ('secondary', '📖 Articles secondaires (60–74/100)')):
        rows = [r for r in entries.values() if r.get('group') == group]
        lines.extend(['## '+label, '', '| Score | Publication | Lien |', '| ---: | --- | --- |'])
        for row in sorted(rows, key=lambda x: (-int(x['score']), x['title'])):
            title = row['title'].replace('|','\\|').replace('\n',' ').replace('<','&lt;')
            url = row['source'].replace(')', '%29')
            lines.append(f"| {row['score']}/100 | {title} | [Lire]({url}) |")
        if not rows:
            lines.append('| — | Aucun article pour le moment | — |')
        lines.append('')
    return '\n'.join(lines)


def get_messages():
    user = os.environ.get('GMAIL_FROM', '').strip()
    password = os.environ.get('GMAIL_APP_PASSWORD', '').strip()
    if not user or not password:
        raise RuntimeError('Secrets GMAIL_FROM et GMAIL_APP_PASSWORD requis.')
    conn = imaplib.IMAP4_SSL('imap.gmail.com', 993, timeout=30)
    try:
        conn.login(user, password)
        typ, _ = conn.select('INBOX', readonly=True)
        if typ != 'OK':
            raise RuntimeError('Ouverture de INBOX impossible')
        since = (date.today() - timedelta(days=LOOKBACK)).strftime('%d-%b-%Y')
        typ, result = conn.uid('search', None, 'SINCE', since, 'FROM', SENDER)
        if typ != 'OK':
            raise RuntimeError('Recherche IMAP échouée')
        uids = (result[0] or b'').split()[-MAIL_LIMIT:]
        messages = []
        for uid in uids:
            typ, data = conn.uid('fetch', uid, '(RFC822)')
            if typ != 'OK':
                continue
            payload = next((x[1] for x in data if isinstance(x, tuple) and len(x) > 1), None)
            if not payload:
                continue
            msg = email.message_from_bytes(payload)
            # IMAP FROM is a search hint: verify the actual From field.
            if parseaddr(decoded(msg.get('From', '')))[1].lower() != SENDER:
                continue
            msgid = msg.get('Message-ID', '') or ('uid-' + uid.decode('ascii'))
            messages.append((hashlib.sha256(msgid.encode('utf-8')).hexdigest()[:24], msg))
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def process(apply, candidates, states, review):
    import cache_v16 as cachelib
    import collect as core
    import publish_weekly as gh
    cache = cachelib.read()
    records = load(PRIMARY_FILE, {})
    secondary = load(SECONDARY_FILE, {})
    scholar_records = load(SCHOLAR_FILE, {})
    # Même en aperçu : interroger Notion en lecture seule pour identifier les articles
    # déjà publiés par la veille quotidienne avant tout appel Gemini.
    try:
        titles, dois = core.existing_items()
        print(f'Déduplication Notion : {len(titles)} titre(s) connu(s).')
    except Exception as exc:
        # En mode réel on préfère s'arrêter plutôt que de risquer des doublons.
        raise RuntimeError('Impossible de vérifier les doublons Notion ; aucun article évalué : ' + str(exc)) from exc
    local_titles = {normalized(r.get('title')) for d in (records, secondary, scholar_records)
                    for r in d.values() if isinstance(r, dict)}
    # Le cache garde également le titre de publications déjà évaluées.
    # La clé du cache repose parfois sur le DOI ou l'URL, différente du lien Scholar.
    for row in cache.get('articles', {}).values():
        if isinstance(row, dict) and row.get('title'):
            local_titles.add(normalized(row['title']))
    notion_titles = {normalized(t) for t in titles}
    processed = published = 0
    for item in candidates:
        key = item['key']
        if key in states['items']:
            continue
        paper = scholar_paper(item)
        identity = cachelib.key(paper)
        print(f"• {item['title'][:115]}")
        if (normalized(item['title']) in local_titles or cachelib.done(cache, paper)
                or identity in records or identity in secondary
                or identity in scholar_records or normalized(item['title']) in notion_titles
                or (doi_from_link(item['url']).lower() and
                    doi_from_link(item['url']).lower() in {str(d).lower().removeprefix('https://doi.org/') for d in dois})):
            print('  → déjà connu')
            if apply:
                states['items'][key] = 'already_known'
            continue
        if not apply:
            print('  → aperçu : candidat à évaluer directement par Gemini (titre seul)')
            continue
        if processed >= ARTICLE_LIMIT:
            print('  → reporté : limite de nouveaux articles atteinte')
            continue
        record = {'paper': paper, 'themes': ['T03 — Google Scholar'], 'sources': {'Google Scholar'}}
        decisions = core.ai_assess([record])
        if not isinstance(decisions, dict) or 0 not in decisions:
            raise RuntimeError('Gemini a renvoyé un résultat incomplet ; état non marqué.')
        decision = decisions[0]
        score = int(decision.get('score', 0))
        category = str(decision.get('categorie', '')).casefold().strip()
        status = 'rejected'
        source = item['url']
        primary = (category in ('coeur', 'transfert_explicite') and
                   score >= (core.MIN_SCORE_CORE if category == 'coeur' else core.MIN_SCORE_TRANSFER) and
                   str(decision.get('decision', '')).casefold() in ('conserver', 'consigner', 'examiner'))
        if primary:
            # Confirmation in Notion just before write. No invented publication date.
            if normalized(item['title']) not in notion_titles:
                if not core.WRITE_NOTION:
                    raise RuntimeError('WRITE_NOTION doit être true en mode réel')
                detail = core.analyze_detail(record)
                schema = core.api('https://api.notion.com/v1/data_sources/' + core.SOURCE_ID).get('properties', {})
                core.notion_add(record, decision, detail, schema)
                scholar_records[identity] = {
                    'title': item['title'], 'score': score, 'source': source,
                    'summary': detail.get('resume_court', ''), 'group': 'primary',
                }
                titles.add(core.norm(item['title']))
                notion_titles.add(normalized(item['title']))
                local_titles.add(normalized(item['title']))
                status = 'imported'
                published += 1
        elif 60 <= score <= 74 and category in ('coeur', 'transfert_explicite'):
            scholar_records[identity] = {
                'title': item['title'], 'score': score, 'source': source,
                'group': 'secondary',
            }
            local_titles.add(normalized(item['title']))
            published += 1
        cachelib.set_status(cache, paper, decision, status)
        states['items'][key] = status
        review.pop(key, None)
        processed += 1
        # Persist after each item to avoid re-evaluation on partial errors.
        cachelib.write(cache)
        save(SCHOLAR_FILE, scholar_records)
        save(STATE_FILE, states)
        save(REVIEW_FILE, review)
        print(f'  → {status} ({score}/100)')
    if published:
        if not gh.GH_TOKEN:
            raise RuntimeError('PUBLIC_REPO_TOKEN manquant')
        gh.put_file('scholar/README.md', scholar_markdown(scholar_records),
                    'Google Scholar : publications retenues')
    return processed, published


def main():
    states = load(STATE_FILE, {'items': {}, 'messages': []})
    states.setdefault('items', {})
    states.setdefault('messages', [])
    review = load(REVIEW_FILE, {})
    messages = get_messages()
    print(f'Google Scholar : {len(messages)} email(s) lus. Mode : {"RÉEL" if APPLY else "APERÇU"}.')
    candidates = []
    for message_id, message in messages:
        links = extract_candidates(message)
        print('Email', decoded(message.get('Subject', ''))[:100], '→', len(links), 'lien(s) candidats')
        for candidate in links:
            key = hashlib.sha256(normalized(candidate['title']).encode('utf-8')).hexdigest()[:24]
            candidates.append({**candidate, 'key': key})
    if not candidates:
        print('Aucun candidat détecté. Aucun changement.')
        return
    unique = {item['key']: item for item in candidates}
    imported, published = process(APPLY, list(unique.values()), states, review)
    print(f'Bilan : {len(unique)} candidat(s), {imported} nouveau(x) évalué(s), {published} publié(s).')
    if APPLY:
        save(STATE_FILE, states)
        save(REVIEW_FILE, review)
    else:
        print('APERÇU : aucune écriture, aucun appel Gemini et aucune modification Notion/GitHub.')


if __name__ == '__main__':
    main()
