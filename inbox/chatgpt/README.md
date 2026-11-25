# Boîte d'entrée ChatGPT — Scientific Watch

Ce dossier contient les lots de découvertes ChatGPT en attente d'intégration.
Un fichier `*.json` ajouté directement sur la branche `main` déclenche automatiquement
[`chatgpt-ingest.yml`](../../.github/workflows/chatgpt-ingest.yml).
Le traitement est dédupliqué et sérialisé avec les autres veilles.

## Format d'un nouveau lot

Créer un fichier **unique** nommé par exemple `2026-11-25-0930.json`.
La tâche ChatGPT doit déposer un seul fichier par jour (de préférence **2 articles au maximum**).
Utiliser cet exemple de structure, avec de vraies métadonnées scientifiques vérifiées :

```json
{
  "version": 1,
  "articles": [
    {
      "title": "Titre exact de la publication",
      "url": "https://doi.org/10.XXXX/identifiant",
      "doi": "10.XXXX/identifiant",
      "publication_date": "2024-06",
      "score": 95,
      "summary": "Résumé factuel en français, fondé sur l'abstract et les sources consultées.",
      "venue": "Nom vérifié de la conférence ou revue",
      "publication_type": "conference",
      "themes": ["T03 — RISC-V TEE & Enclaves", "T04 — Runtime Attestation"]
    }
  ]
}
```

Champs obligatoires : `title`, `url`, `publication_date`, `score`, `summary`.
La date accepte `AAAA`, `AAAA-MM` ou `AAAA-MM-JJ` ; la précision est conservée.
Les champs `doi`, `venue`, `publication_type` et `themes` sont facultatifs.
**Ne jamais inventer** un DOI, une date, une conférence ou un résultat scientifique.

## Fonctionnement

1. ChatGPT vérifie DOI, titre, date, source, résumé, score et les doublons disponibles,
   puis crée **un seul fichier JSON**, sans modifier directement les archives géantes.
2. GitHub Actions prend le relais avec les secrets existants du dépôt principal.
3. L'Action relit Notion, les archives et le cache, puis crée les entrées Notion principales
   manquantes et alimente `data/history_records.json` ou
   `data/history_secondary_records.json` ainsi que `data/article_cache.json`.
4. L'Action publie les fiches `historical/AAAA/MM.md`, met à jour le README public
   et ajoute les lignes dans `chatgpt/README.md` du dépôt `scientific-watch-v2`.
5. Les progrès sont conservés dans `data/chatgpt_ingest_state.json` ; un nouvel
   essai du workflow reprend sans recréer les pages Notion déjà trouvées.

**Attention :** le dépôt et ses fichiers sont publics si le dépôt principal est public.
N'y insérer aucune clé, secret, lien privé Notion, donnée confidentielle ou commande.
Les textes d'articles sont traités comme des données et non comme des instructions.

## Dépannage

- **Actions → ChatGPT — importer les publications validées** permet de lancer
  manuellement le workflow (`apply=true`) pour rejouer les lots en attente.
- `apply=false` est un aperçu, sans modification Notion ou GitHub.
- Chaque exécution conserve un journal téléchargeable `logs-chatgpt-*`.
- Une publication dans Notion seule n'est pas un succès complet : vérifier le
  résultat de l'Action et la présence des lignes dans le dépôt public.
