#!/usr/bin/env python3
"""Search ranking tests for the SQLite FTS5 session indexer.

Tests that ranking behaves correctly: field weights, exact phrases,
short tokens, negation, project filters, recency tiebreakers.

Run: python3 test_search.py
"""

import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(__file__))
from session_indexer import init_db, index_session, search


class SearchTestCase(unittest.TestCase):
    """Base class that creates a fresh in-memory database per test."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def add(self, sid, title='', preview='', body='', project='test', created=100, mtime=100):
        """Helper to add a session and commit."""
        index_session(self.conn, sid, title, preview, body, project, created, mtime)
        self.conn.commit()

    def top(self, query, n=1):
        """Return top N session IDs from search."""
        results = search(self.conn, query)
        return [r['sid'] for r in results[:n]]

    def all_sids(self, query):
        """Return all session IDs from search."""
        return [r['sid'] for r in search(self.conn, query)]


class TestExactPhrase(SearchTestCase):
    """FTS5 handles exact phrase matching natively."""

    def test_quoted_phrase_finds_exact_match(self):
        self.add('a', body='We planned week 12 goals and deliverables for week 12')
        self.add('b', body='The 12 items on the weekly agenda were reviewed this week')
        results = self.all_sids('"week 12"')
        self.assertIn('a', results)
        # 'b' has both 'week' and '12' but not as a phrase
        self.assertNotIn('b', results)

    def test_unquoted_terms_match_separately(self):
        """Without quotes, 'week 12' matches both terms anywhere."""
        self.add('a', body='We planned week 12 goals')
        self.add('b', body='The 12 items on the weekly agenda this week')
        results = self.all_sids('week 12')
        self.assertIn('a', results)
        self.assertIn('b', results)


class TestShortTokens(SearchTestCase):
    """The whole reason for the FTS5 migration: short tokens work."""

    def test_two_digit_number(self):
        self.add('a', body='week 12 planning session')
        results = self.all_sids('12')
        self.assertIn('a', results)

    def test_single_digit(self):
        self.add('a', body='phase 3 of the rollout')
        results = self.all_sids('3')
        self.assertIn('a', results)

    def test_version_string(self):
        self.add('a', body='upgraded to v2 of the API')
        results = self.all_sids('v2')
        self.assertIn('a', results)

    def test_issue_id(self):
        self.add('a', body='Fixed EARTH-42 in the sprint')
        results = self.all_sids('42')
        self.assertIn('a', results)


class TestFieldWeights(SearchTestCase):
    """Title match (10x) > preview match (5x) > body match (1x)."""

    def test_title_beats_body(self):
        self.add('title', title='Week 12 Planning', body='just a session')
        self.add('body', title='', body='we discussed week 12 goals')
        self.assertEqual(self.top('"week 12"'), ['title'])

    def test_title_beats_preview(self):
        self.add('title', title='Outreach Campaign', preview='standup', body='stuff')
        self.add('preview', title='', preview='outreach campaign metrics', body='stuff')
        self.assertEqual(self.top('outreach campaign'), ['title'])

    def test_preview_beats_body(self):
        self.add('preview', preview='Cold email outreach feature', body='stuff')
        self.add('body', preview='standup', body='cold email outreach feature details')
        self.assertEqual(self.top('cold email outreach'), ['preview'])


class TestFrequency(SearchTestCase):
    """BM25 rewards higher term frequency (up to saturation)."""

    def test_more_mentions_rank_higher(self):
        self.add('many', body='week 12 plan. week 12 goals. week 12 review. week 12 deliverables.')
        self.add('few', body='we briefly mentioned week 12')
        self.assertEqual(self.top('"week 12"'), ['many'])

    def test_rare_term_outranks_common(self):
        """IDF: a term appearing in fewer sessions is more distinctive."""
        # 'standup' appears in 1 session, 'email' in 3
        # FTS5 default is AND, so use OR to test IDF ranking across sessions
        self.add('s1', body='morning standup dashboard review')
        self.add('s2', body='email outreach campaign')
        self.add('s3', body='email template design')
        self.add('s4', body='email notifications feature')
        results = self.all_sids('standup OR email')
        # s1 should rank first — 'standup' has higher IDF (1/4 sessions vs 3/4)
        self.assertEqual(results[0], 's1')


class TestCoverage(SearchTestCase):
    """Multi-term queries reward sessions that contain more distinct terms.

    Regression: a huge session mentioning ONE query term 50x used to out-rank
    smaller sessions that actually contained ALL query terms. Coverage² fixes that.
    """

    def test_all_terms_beat_one_term_many_times(self):
        # Mimics the real case: "daily gdpr dpa" — 'daily' is a common word
        # in a Daily.co-heavy session with 0 hits of gdpr/dpa.
        huge_one_term = 'daily ' * 50 + 'video call split session bug'
        all_three = 'daily gdpr dpa discussion about processor agreement'
        self.add('huge', title='reduce-intro-no-shows', body=huge_one_term, mtime=2000)
        self.add('small', title='', body=all_three, mtime=2000)
        self.assertEqual(self.top('daily gdpr dpa'), ['small'])

    def test_two_of_three_beats_one_of_three(self):
        self.add('one', body='daily ' * 20)
        self.add('two', body='daily gdpr compliance review')
        self.assertEqual(self.top('daily gdpr dpa'), ['two'])

    def test_single_term_query_unaffected(self):
        """Coverage is always 1.0 for single-term queries — no behavior change."""
        self.add('many', body='standup ' * 10)
        self.add('few', body='standup once')
        self.assertEqual(self.top('standup'), ['many'])

    def test_focused_preview_beats_sprawling_body(self):
        """Both sessions have full coverage, but target's preview contains BOTH
        query terms — a strong 'this session is about X' signal. Regression case:
        'Stripe Laura' used to surface a 175KB no-show session where Laura was
        mentioned 81x in unrelated context and Stripe 4x, beating a focused
        session whose preview was literally 'Laura enabled Stripe for her account'.
        """
        fat_body = ('laura ' * 81) + ('stripe ' * 4) + 'no-show therapist data'
        self.add('fat', title='reduce-intro-no-shows', body=fat_body, mtime=2000)
        self.add('focused',
                 preview='Therapist Laura just enabled Stripe for her account',
                 body='stripe payment flow. laura setup. stripe onboarding.',
                 mtime=2000)
        self.assertEqual(self.top('stripe laura'), ['focused'])


class TestRecencyTiebreaker(SearchTestCase):
    """Among equally relevant results, newer sessions rank first."""

    def test_newer_session_wins_tie(self):
        self.add('old', body='standup morning routine', mtime=1000)
        self.add('new', body='standup morning routine', mtime=2000)
        results = self.top('standup morning', n=2)
        self.assertEqual(results[0], 'new')
        self.assertEqual(results[1], 'old')


class TestProjectFilter(SearchTestCase):

    def test_filter_includes_matching_project(self):
        self.add('a', body='email feature', project='kaufmann-health')
        self.add('b', body='email personal', project='Personal-Support')
        results = self.all_sids('email --project kaufmann')
        self.assertIn('a', results)
        self.assertNotIn('b', results)

    def test_filter_is_case_insensitive(self):
        self.add('a', body='email feature', project='Kaufmann-Health')
        results = self.all_sids('email --project kaufmann')
        self.assertIn('a', results)


class TestNegation(SearchTestCase):

    def test_dash_excludes_term(self):
        self.add('standup', body='morning standup with email review')
        self.add('coding', body='email template coding session')
        results = self.all_sids('email -standup')
        self.assertNotIn('standup', results)
        self.assertIn('coding', results)

    def test_exclude_flag(self):
        self.add('a', body='standup email')
        self.add('b', body='coding email')
        results = self.all_sids('email --exclude standup')
        self.assertNotIn('a', results)
        self.assertIn('b', results)


class TestEdgeCases(SearchTestCase):

    def test_empty_query_returns_nothing(self):
        self.add('a', body='some content')
        self.assertEqual(search(self.conn, ''), [])

    def test_special_chars_dont_crash(self):
        self.add('a', body='function() { return 42; }')
        # These queries have FTS5 special chars — should not raise
        results = search(self.conn, 'function()')
        self.assertIsInstance(results, list)

    def test_unbalanced_quotes_dont_crash(self):
        self.add('a', body='week 12 planning')
        results = search(self.conn, '"week 12')
        self.assertIsInstance(results, list)

    def test_hyphenated_terms(self):
        """unicode61 tokenizer splits on hyphens."""
        self.add('a', body='pre-commit hook failed')
        results = self.all_sids('pre-commit')
        self.assertIn('a', results)

    def test_dehyphenated_compound_matches(self):
        """Searching without hyphen matches hyphenated terms via dehyphenation."""
        # Note: dehyphenation happens at extraction time (_extract_one), not in
        # index_session. This test verifies FTS5 would match if the joined form
        # were present in the body (simulating what _extract_one produces).
        self.add('a', body='Therapie-Kompass chatbot TherapieKompass')
        results = self.all_sids('therapiekompass')
        self.assertIn('a', results)

    def test_german_text(self):
        """unicode61 handles accents and umlauts."""
        self.add('a', body='Die Therapeutin hat die Zahlungseinrichtung abgeschlossen')
        results = self.all_sids('Therapeutin')
        self.assertIn('a', results)


class TestToolUseExclusion(SearchTestCase):
    """Body text should come from user messages and assistant text blocks,
    not from tool_use JSON. This is handled by the extraction layer."""

    def test_body_content_is_searchable(self):
        self.add('a', preview='standup', body='cold email outreach sales pipeline')
        results = self.all_sids('outreach')
        self.assertIn('a', results)


class TestOutputFormat(SearchTestCase):
    """Search results contain the expected fields."""

    def test_result_has_all_fields(self):
        self.add('a', title='Test', preview='Hello', body='content',
                 project='myproject', created=1000, mtime=2000)
        results = search(self.conn, 'content')
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r['sid'], 'a')
        self.assertEqual(r['title'], 'Test')
        self.assertEqual(r['preview'], 'Hello')
        self.assertEqual(r['project'], 'myproject')
        self.assertEqual(r['created_epoch'], 1000)
        self.assertEqual(r['mtime_epoch'], 2000)
        self.assertIn('rank', r)


if __name__ == '__main__':
    unittest.main()
