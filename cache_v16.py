"""Cache persistant des évaluations Gemini, partagé par quotidien et historique.

Important : les candidats acceptés MAIS non importés ne sont pas ignorés.
Le dépôt privé garde ce fichier; le workflow le commite après succès.
"""
import json
import pathlib
import re
import unicodedata
from datetime import datetime, timezone

PATH = pathlib.Path('data/article_cache.json')


def key(paper):
    doi = str(paper.get('doi') or '').strip().lower()
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi)
    if doi:
        return 'doi:' + doi
    title = unicodedata.normalize('NFKD', paper.get('title') or '').casefold()
    title = ''.join(ch for ch in title if ch.isalnum())
    return 'title:' + title if title else ''


def read():
    if not PATH.exists():
        return {'version': 1, 'articles': {}}
    data = json.loads(PATH.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('articles'), dict):
        raise ValueError('Format du cache inattendu')
    return data


def set_status(cache, paper, assessment, status):
    assert status in {'rejected', 'imported'}
    identity = key(paper)
    if not identity:
        return
    cache['articles'][identity] = {
        'title': (paper.get('title') or '')[:400],
        'status': status,
        'score': assessment.get('score'),
        'category': assessment.get('categorie', ''),
        'decision': assessment.get('decision', ''),
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }


def done(cache, paper):
    """Déduplication inter-sources : DOI si connu, sinon titre canonique.

    Une publication évaluée par Scholar (sans DOI) sera reconnue plus tard
    lorsque OpenAlex/Crossref lui associera un DOI. Pas de correspondance floue :
    mieux vaut une réévaluation rare qu'une fusion de deux travaux différents.
    """
    identity = key(paper)
    articles = cache.get('articles', {})
    terminal = {'rejected', 'imported'}
    if identity and articles.get(identity, {}).get('status') in terminal:
        return True
    title_key = key({'title': paper.get('title')})
    if not title_key or title_key == '':
        return False
    if articles.get(title_key, {}).get('status') in terminal:
        return True
    return any(
        isinstance(row, dict) and row.get('status') in terminal
        and key({'title': row.get('title')}) == title_key
        for row in articles.values()
    )


def write(cache):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(PATH)
