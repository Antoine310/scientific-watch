"""Notifications Gmail pour la veille scientifique V13.

Utilise SMTP avec un mot de passe d'application Gmail (jamais le mot de passe
principal). Si les secrets SMTP sont absents, n'envoie rien et affiche
une instruction claire : le moteur de collecte fonctionne sans Gmail.

Modes : new (nouveaux articles du lancement), weekly (7 derniers jours dans Notion),
       failure (signalement succinct de panne).
Bibliothèque standard seulement.
"""
import json
import os
import pathlib
import smtplib
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

FROM = os.getenv('GMAIL_FROM', '').strip()
TO = os.getenv('GMAIL_TO', '').strip()
PASSWORD = os.getenv('GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
NOTION_TOKEN = os.getenv('NOTION_TOKEN', '').strip()
SOURCE_ID = os.getenv('NOTION_DATA_SOURCE_ID', '').strip()


def send(subject, body):
    """Un seul e-mail, pas de notification si Gmail n'est pas configuré."""
    if not all((FROM, TO, PASSWORD)):
        print('Gmail désactivé : ajouter GMAIL_FROM, GMAIL_TO et GMAIL_APP_PASSWORD.')
        return
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = FROM
    msg['To'] = TO
    msg.set_content(body)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as smtp:
        smtp.login(FROM, PASSWORD)
        smtp.send_message(msg)
    print('Notification Gmail envoyée.')


def article_lines(rows):
    chunks = []
    for a in rows:
        chunks.append(
            f"• {a.get('title', 'Sans titre')}"
            + (f" — {a['score']}/100" if a.get('score') is not None else '')
            + '\n' + (a.get('summary') or 'Résumé à consulter dans Notion')[:350]
            + '\n' + (a.get('url') or '')
        )
    return '\n\n'.join(chunks)


def recent_from_notion():
    """Lit les fiches datées des 7 derniers jours ; pagination comprise."""
    if not NOTION_TOKEN or not SOURCE_ID:
        raise RuntimeError('Secrets Notion nécessaires pour le bilan hebdomadaire.')
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    url = f'https://api.notion.com/v1/data_sources/{SOURCE_ID}/query'
    cursor = None
    rows = []
    while True:
        body = {'page_size': 100, 'filter': {'property': 'Date de détection',
                                             'date': {'on_or_after': since}}}
        if cursor:
            body['start_cursor'] = cursor
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + NOTION_TOKEN,
                     'Notion-Version': '2025-09-03',
                     'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=40) as result:
            data = json.load(result)
        for item in data.get('results', []):
            p = item.get('properties', {})
            def plain(key, kind):
                return ''.join(x.get('plain_text', '') for x in p.get(key, {}).get(kind, []))
            rows.append({'title': plain('Titre', 'title'),
                         'summary': plain('Résumé court', 'rich_text'),
                         'score': p.get('Pertinence', {}).get('number'),
                         'url': item.get('url', '')})
        cursor = data.get('next_cursor')
        if not cursor:
            break
    rows.sort(key=lambda row: row.get('score') or 0, reverse=True)
    return rows


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else 'new').lower()
    if mode == 'new':
        file = pathlib.Path('new_articles.json')
        articles = json.loads(file.read_text(encoding='utf-8')) if file.exists() else []
        if articles:
            send(f'🔬 Veille scientifique — {len(articles)} nouvelle(s) publication(s)',
                 'Nouvelles publications ajoutées à Notion :\n\n' + article_lines(articles))
        else:
            print('Aucune nouvelle publication : pas d’e-mail.')
    elif mode == 'weekly':
        articles = recent_from_notion()
        send(f'📚 Récapitulatif scientifique — {len(articles)} publication(s) cette semaine',
             ('Voici les fiches détectées sur les 7 derniers jours :\n\n'
              + article_lines(articles)) if articles else 'Aucune nouvelle publication enregistrée cette semaine.')
    elif mode == 'failure':
        url = os.getenv('RUN_URL', '')
        send('⚠️ Veille scientifique — exécution en échec',
             'Une collecte a échoué. Elle sera retentée au prochain créneau programmé.\n'
             f'Consulter les journaux GitHub Actions : {url}\n')
    else:
        raise ValueError('Mode attendu : new, weekly ou failure.')

if __name__ == '__main__':
    main()
