import unittest
from datetime import timedelta

from scope.permission import Grant, PermitRegistry, origin_of
from scope import reasons
from tests.helpers import FakeClock


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.start = self.clock()
        self.grant = Grant('g1', 'http://partner.example', ('/public/',),
                           valid_from=self.start - timedelta(hours=1),
                           valid_until=self.start + timedelta(days=1),
                           retain_days=3)
        self.registry = PermitRegistry([self.grant], clock=self.clock)

    def test_valid_grant_allows_covered_path(self):
        decision = self.registry.evaluate('http://partner.example/public/a')
        self.assertTrue(decision.allowed)
        self.assertIs(decision.grant, self.grant)

    def test_origin_and_path_must_both_match(self):
        decision = self.registry.evaluate('http://other.example/public/a')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, reasons.PERMIT_NO_MATCH)
        decision = self.registry.evaluate('http://partner.example/private/a')
        self.assertEqual(decision.reason, reasons.PERMIT_NO_MATCH)

    def test_time_window_is_checked(self):
        future_grant = Grant('g2', 'http://partner.example', ('/soon/',),
                             valid_from=self.start + timedelta(days=1))
        self.registry.add(future_grant)
        decision = self.registry.evaluate('http://partner.example/soon/x')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, reasons.PERMIT_NOT_YET_VALID)

        self.clock.advance(2 * 24 * 3600)
        decision = self.registry.evaluate('http://partner.example/public/a')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, reasons.PERMIT_EXPIRED)

    def test_revoke_and_restore_change_evaluation(self):
        self.registry.revoke('g1')
        decision = self.registry.evaluate('http://partner.example/public/a')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, reasons.PERMIT_REVOKED)
        self.registry.restore('g1')
        self.assertTrue(self.registry.evaluate(
            'http://partner.example/public/a').allowed)

    def test_scheme_and_credentials_are_rejected(self):
        for url in ('file:///tmp/x', 'ftp://partner.example/a',
                    'http://user:pw@partner.example/public/a'):
            self.assertEqual(self.registry.evaluate(url).reason,
                             reasons.SCHEME_REJECTED)

    def test_grants_for_explains_all_covering_grants(self):
        self.registry.revoke('g1')
        grants = self.registry.grants_for('http://partner.example/public/a')
        self.assertEqual([g.grant_id for g in grants], ['g1'])
        self.assertTrue(grants[0].revoked)

    def test_snapshot_captures_retention_terms(self):
        self.assertEqual(self.grant.snapshot(), {
            'grant_id': 'g1', 'origin': 'http://partner.example',
            'path_prefixes': ['/public/'], 'retain_days': 3})

    def test_origin_of(self):
        self.assertEqual(origin_of('http://h:8080/a/b?x=1'), 'http://h:8080')
        self.assertIsNone(origin_of('/relative/path'))


if __name__ == '__main__':
    unittest.main()
