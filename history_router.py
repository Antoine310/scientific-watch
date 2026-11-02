"""Oriente le même mode historique vers la période récente puis ancienne.

La progression 2025–2026 n'est jamais réinitialisée. Lorsqu'elle est
terminée, le prochain lancement passe à 2024–2016.
"""
import json
from pathlib import Path


def forward_finished():
    path = Path('data/history_progress.json')
    if not path.exists():
        return False
    return bool(json.loads(path.read_text(encoding='utf-8')).get('completed'))


def main():
    if forward_finished():
        print('Historique récent terminé : lancement du rattrapage 2024 → 2016.')
        import history_backward
        history_backward.main()
    else:
        print('Historique récent encore en cours : poursuite de 2025 → octobre 2026.')
        import history
        history.main()


if __name__ == '__main__':
    main()
