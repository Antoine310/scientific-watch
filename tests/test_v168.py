import os
import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('NOTION_TOKEN', 'TEST_NOT_REAL')
os.environ.setdefault('NOTION_DATA_SOURCE_ID', 'TEST_NOT_REAL')
os.environ.setdefault('NOTION_DATABASE_ID', 'TEST_NOT_REAL')
os.environ.setdefault('GEMINI_API_KEY', 'TEST_NOT_REAL')
import venue_v168 as venue
import publish_history
import publish_weekly
import history_multisource
import history
import collect
import venue_backfill


class VenueTests(unittest.TestCase):
    def test_crossref_proceedings(self):
        data = venue.from_crossref({'type': 'proceedings-article', 'event': {'name': 'USENIX Security 2025'}})
        self.assertEqual(data['venue_landmark'], 'USENIX Security')
        self.assertEqual(data['venue_priority'], 2)

    def test_no_priority_for_journal_even_if_keyword(self):
        data = venue.from_crossref({'type': 'journal-article', 'container-title': ['USENIX Security Bulletin']})
        self.assertEqual(data['publication_type'], 'revue')
        self.assertEqual(data['venue_priority'], 0)

    def test_preprint_not_automatically_conference(self):
        data = venue.from_arxiv('Accepted at USENIX Security 2025')
        self.assertEqual(data['publication_type'], 'prepublication')
        self.assertEqual(data['venue_priority'], 0)
        self.assertNotIn('USENIX', venue.display(data))

    def test_openalex_source(self):
        paper = {'primary_location': {'source': {'display_name': 'International Symposium on Computer Architecture', 'type': 'conference'}}}
        self.assertEqual(venue.from_openalex(paper)['venue_priority'], 2)

    def test_dblp_venue(self):
        data = venue.from_dblp({'venue': 'CHES', 'type': 'Conference and Workshop Papers'})
        self.assertEqual(data['venue_priority'], 1)

    def test_unknown_not_inferred(self):
        self.assertEqual(venue.from_crossref({'title': ['Spectre Security in CPU']})['venue_priority'], 0)

    def test_display_in_historical_markdown(self):
        row = {'title': 'Test secure processor', 'score': 92, 'publication_date': '2025-01-10',
               'source': 'https://example.org/paper', 'summary': 'Résumé',
               **venue.from_crossref({'type': 'proceedings-article', 'container-title': ['Proceedings of USENIX Security 2025']})}
        text = publish_history.build_month('2025-01', [row], [{'title':'Secondaire', 'score':67, 'source':'https://example.org/s', 'publication_date':'2025-01-21', **venue.info('IEEE HOST','conference','DBLP')}])
        self.assertIn('Conférence / Revue', text)
        self.assertIn('USENIX Security', text)
        self.assertIn('IEEE HOST', text)
        self.assertIn('92/100', text)
        self.assertIn('67/100', text)
        self.assertIn('pas un classement ICORE', text)

    def test_weekly_report_can_render_without_network(self):
        row = {'title': 'Test', 'score': 87, 'summary': 'Test résumé', 'themes': [],
               'source': 'https://example.org/paper', 'doi': '', 'publication_venue': 'NDSS', 'publication_type': 'conference'}
        txt, _, _ = publish_weekly.build_report([row], date(2026, 11, 2))
        self.assertIn('NDSS', txt)

    def test_routing_crossref_and_priority(self):
        item = {'title':['Time Protection'], 'type':'proceedings-article',
                'event': {'name': 'ISCA 2026'}, 'published': {'date-parts': [[2026, 10]]}}
        paper = history_multisource.crossref_convert(item, '2026-10')
        self.assertEqual(paper['publication_date_precision'], 'month')
        self.assertEqual(paper['venue_priority'], 2)

    def test_backfill_doi_parse(self):
        self.assertEqual(venue.doi_from_record({'doi':'10.1145/123.456'}), '10.1145/123.456')
        self.assertEqual(venue.doi_from_record({'source':'https://doi.org/10.1145/123.456'}), '10.1145/123.456')

    def test_priority_fairness(self):
        candidates = [{'paper':venue.annotate({'id':str(i)}, venue.info('USENIX Security','conference','DBLP'))} for i in range(10)]
        candidates += [{'paper':{'id':str(i)}} for i in range(10,20)]
        sample = venue.prioritize(candidates, 10)[:10]
        self.assertLessEqual(sum(venue.priority(x['paper'])>0 for x in sample), 5)
        self.assertEqual(len(sample), 10)
        self.assertEqual(len({x['paper']['id'] for x in venue.prioritize(candidates, 10)}), 20)

    def test_collect_merges_venue_despite_better_abstract(self):
        old = {'title':'RISC-V and Hardware Security', 'id':'https://doi.org/10.1145/test',
               'primary_location':{'landing_page_url':'https://doi.org/10.1145/test'},
               **venue.info('USENIX Security','conference','Crossref'),
               'abstract_inverted_index':{}}
        new = {'title':old['title'],'id':'https://arxiv.org/abs/2601.00001',
               **venue.from_arxiv(), 'abstract_inverted_index': {'details':[0,2], 'more':[1]}}
        found={}
        self.assertTrue(collect.add_candidate(found,['T03 — RISC-V TEE & Enclaves'],old,set(),set()))
        self.assertTrue(collect.add_candidate(found,['T03 — RISC-V TEE & Enclaves'],new,set(),set()))
        result=list(found.values())[0]['paper']
        self.assertEqual(result['venue_priority'], 2)
        self.assertTrue(collect.abstract(result['abstract_inverted_index']))

    def test_backfill_preview_does_not_touch_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / 'historical.json'
            other = Path(tmp) / 'secondary.json'
            state = Path(tmp) / 'progress.json'
            content = {'X': {'title': 'A Highly Specific Secure Processor Paper',
                             'score': 92, 'publication_date': '2025-01-20',
                             'source': 'https://doi.org/10.1145/123.456'}}
            records.write_text(json.dumps(content))
            with patch.object(venue_backfill, 'RECORDS', records), \
                 patch.object(venue_backfill, 'SECONDARY', other), \
                 patch.object(venue_backfill, 'STATE', state), \
                 patch.object(venue_backfill, 'APPLY', False), \
                 patch.object(venue_backfill, 'LIMIT', 20), \
                 patch.object(venue, 'crossref_lookup', return_value=venue.info('USENIX Security', 'conference', 'Crossref')), \
                 patch.object(venue_backfill.publish_history, 'publish') as mock_pub:
                with redirect_stdout(StringIO()):
                    venue_backfill.main()
                self.assertEqual(json.loads(records.read_text()), content)
                self.assertFalse(state.exists())
                mock_pub.assert_not_called()

    def test_backfill_apply_updates_venue_not_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / 'historical.json'
            other = Path(tmp) / 'secondary.json'
            state = Path(tmp) / 'progress.json'
            content = {'X': {'title': 'A Highly Specific Secure Processor Paper',
                             'score': 92, 'publication_date': '2025-01-20',
                             'source': 'https://doi.org/10.1145/123.456'}}
            records.write_text(json.dumps(content))
            with patch.object(venue_backfill, 'RECORDS', records), \
                 patch.object(venue_backfill, 'SECONDARY', other), \
                 patch.object(venue_backfill, 'STATE', state), \
                 patch.object(venue_backfill, 'APPLY', True), \
                 patch.object(venue_backfill, 'LIMIT', 20), \
                 patch.object(venue, 'crossref_lookup', return_value=venue.info('USENIX Security', 'conference', 'Crossref')), \
                 patch.object(venue_backfill.publish_history, 'publish') as mock_pub:
                with redirect_stdout(StringIO()):
                    venue_backfill.main()
                enriched = json.loads(records.read_text())['X']
                self.assertEqual(enriched['score'], 92)
                self.assertEqual(enriched['venue_priority'], 2)
                self.assertEqual(json.loads(state.read_text())['checked'], ['principal:X'])
                mock_pub.assert_called_once()

    def test_preserve_queries_and_state(self):
        self.assertEqual(len(history_multisource.MONTHLY_TASKS), 25)
        self.assertEqual(history_multisource.new_state()['month'], '2026-10')
        self.assertEqual(history_multisource.MAX_AI, 8)


if __name__ == '__main__':
    unittest.main()
