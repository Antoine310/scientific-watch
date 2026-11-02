#!/usr/bin/env python3
"""Scientific Watch V16.4 — Import prudent des alertes Google Scholar depuis Gmail.

Aperçu par défaut. Seuls les travaux confirmés dans OpenAlex sont soumis à
l'évaluation Gemini, puis aux sorties habituelles. Ne modifie jamais Gmail.
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


def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'scientific-watch-scholar/16.4 (bibliographic verification)', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def doi_from_link(link):
    parsed = urllib.parse.urlparse(link)
    if (parsed.hostname or '').lower() in ('doi.org', 'dx.doi.org'):
        return urllib.parse.unquote(parsed.path.lstrip('/'))
    match = re.search(r'10\.\d{4,9}/[^?\s&#]+', urllib.parse.unquote(link), re.I)
    return match.group(0).rstrip('.,;') if match else ''


def match_work(candidate):
    """Require exact DOI or strong normalized title match (>= .92)."""
    title = candidate['title']
    doi = doi_from_link(candidate['url'])
    try:
        if doi:
            query = 'https://api.openalex.org/works/https://doi.org/' + urllib.parse.quote(doi, safe='/')
            paper = fetch_json(query)
            if SequenceMatcher(None, normalized(title), normalized(paper.get('title'))).ratio() >= .74:
                return paper
        params = urllib.parse.urlencode({'search': title, 'per_page': 5})
        data = fetch_json('https://api.openalex.org/works?' + params)
        papers = data.get('results', [])
        ranked = sorted(((SequenceMatcher(None, normalized(title), normalized(p.get('title'))).ratio(), p)
                        for p in papers), key=lambda pair: pair[0], reverse=True)
        if ranked and ranked[0][0] >= .92 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= .04):
            return ranked[0][1]
    except Exception as exc:
        print('  Vérification OpenAlex indisponible :', str(exc)[:170])
    return None


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
    import publish_history
    cache = cachelib.read()
    records = load(PRIMARY_FILE, {})
    secondary = load(SECONDARY_FILE, {})
    titles, dois = core.existing_items() if apply else (set(), set())
    processed = 0
    published = 0
    for item in candidates:
        key = item['key']
        if key in states['items']:
            continue
        print(f"• {item['title'][:115]}")
        paper = match_work(item)
        if not paper or not paper.get('title') or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(paper.get('publication_date', ''))):
            review[key] = {**item, 'reason': 'Référence OpenAlex non confirmée / date manquante'}
            print('  → à vérifier (pas de publication)')
            continue
        if cachelib.done(cache, paper) or cachelib.key(paper) in records or cachelib.key(paper) in secondary:
            states['items'][key] = 'already_known'
            print('  → déjà connu')
            continue
        if not apply:
            print('  → aperçu : trouvé dans OpenAlex', paper.get('publication_date'))
            continue
        if processed >= ARTICLE_LIMIT:
            print('  → reporté : limite de nouveaux articles atteinte')
            continue
        record = {'paper': paper, 'themes': ['Google Scholar'], 'sources': {'Google Scholar', 'OpenAlex'}}
        decision_list = core.ai_assess([record])
        if len(decision_list) != 1:
            raise RuntimeError('Gemini a renvoyé un résultat incomplet ; état non marqué.')
        decision = decision_list[0]
        score = int(decision.get('score', 0))
        category = str(decision.get('categorie', '')).casefold().strip()
        status = 'rejected'
        identity = cachelib.key(paper)
        source = paper.get('doi') or (paper.get('primary_location') or {}).get('landing_page_url') or item['url']
        if (category in ('coeur', 'transfert_explicite') and
                score >= (core.MIN_SCORE_CORE if category == 'coeur' else core.MIN_SCORE_TRANSFER) and
                str(decision.get('decision', '')).casefold() in ('conserver', 'consigner', 'examiner')):
            # Recheck Notion immediately before write.
            if normalized(paper['title']) not in titles and not (paper.get('doi') and core.normalized_doi(paper['doi']) in dois):
                detail = core.analyze_detail(record)
                if not core.WRITE_NOTION:
                    raise RuntimeError('WRITE_NOTION doit être true en mode réel')
                schema = core.api('https://api.notion.com/v1/data_sources/' + core.SOURCE_ID).get('properties', {})
                core.notion_add(record, decision, detail, schema)
                records[identity] = {'title': paper['title'], 'summary': detail.get('resume_court', ''),
                                     'score': score, 'themes': ['Google Scholar'], 'source': source,
                                     'publication_date': paper['publication_date']}
                titles.add(normalized(paper['title']))
                if paper.get('doi'):
                    dois.add(core.normalized_doi(paper['doi']))
                status = 'imported'
                published += 1
        elif 60 <= score <= 74 and category in ('coeur', 'transfert_explicite'):
            secondary[identity] = {'title': paper['title'], 'score': score, 'source': source,
                                   'publication_date': paper['publication_date']}
            published += 1
        cachelib.set_status(cache, paper, decision, status)
        states['items'][key] = status
        review.pop(key, None)
        processed += 1
        # Persist before attempting publication; retry by next run if publish fails.
        cachelib.write(cache)
        save(PRIMARY_FILE, records)
        save(SECONDARY_FILE, secondary)
        save(STATE_FILE, states)
        save(REVIEW_FILE, review)
        print(f'  → {status} ({score}/100)')
    if published:
        publish_history.publish(records, secondary)
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
