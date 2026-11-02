"""Rattrapage facultatif d'un mois Crossref déjà traversé avant V16.7.3.

Utilise un fichier de progression SEPARE, sans modifier celui du multisource
principal. Réutilise cache, Notion, archives, limites Gemini et publication.
"""
import os
import re

month = os.environ.get('RECHECK_MONTH', '2026-10')
if not re.fullmatch(r'20(?:1[6-9]|2[0-6])-(?:0[1-9]|1[0-2])', month) or not '2016-01' <= month <= '2026-10':
    raise SystemExit('RECHECK_MONTH doit être compris entre 2016-01 et 2026-10.')

os.environ['MULTI_SOURCE_MODE'] = 'crossref_only'
os.environ['MULTI_START_MONTH'] = month
os.environ['MULTI_END_MONTH'] = month
os.environ['MULTI_STATE_PATH'] = f'data/history_multisource_recheck_{month}.json'
os.environ['MULTI_DBLP_ENABLED'] = 'false'
os.environ['MULTI_IACR_ENABLED'] = 'false'

import history_multisource

if __name__ == '__main__':
    print('Rattrapage ciblé Crossref des références à date partielle :', month)
    history_multisource.main()
