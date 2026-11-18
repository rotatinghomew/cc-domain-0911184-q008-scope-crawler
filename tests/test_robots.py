import unittest
from urllib.error import URLError

from scope import reasons
from scope.robots import RobotsGuard
from tests.helpers import FakeClock

ROBOTS_BODY = (b'User-agent: PartnerCollector\n'
               b'Disallow: /private/\n'
               b'Crawl-delay: 2\n')


class FakeOpener:
    def __init__(self, responses):
        # origin+path -> (status, body) 或抛 Exception
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


class RobotsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.origin = 'http://partner.example'

    def _guard(self, responses):
        return RobotsGuard(ttl_seconds=60, clock=self.clock,
                           url_opener=FakeOpener(responses))

    def test_disallowed_path_is_blocked_and_delay_parsed(self):
        guard = self._guard({self.origin + '/robots.txt': (200, ROBOTS_BODY)})
        allowed, reason, delay = guard.evaluate(
            self.origin + '/private/1')
        self.assertFalse(allowed)
        self.assertEqual(reason, reasons.ROBOTS_DISALLOW)
        self.assertEqual(delay, 2.0)

    def test_allowed_path_passes(self):
        guard = self._guard({self.origin + '/robots.txt': (200, ROBOTS_BODY)})
        allowed, reason, delay = guard.evaluate(self.origin + '/public/1')
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(delay, 2.0)

    def test_unavailable_robots_is_fail_closed_on_5xx(self):
        for status in (500, 503):
            guard = self._guard({self.origin + '/robots.txt': (status, b'busy')})
            allowed, reason, _ = guard.evaluate(self.origin + '/public/1')
            self.assertFalse(allowed)
            self.assertEqual(reason, reasons.ROBOTS_UNAVAILABLE)

    def test_unavailable_robots_is_fail_closed_on_network_error(self):
        guard = self._guard(
            {self.origin + '/robots.txt': URLError('connection refused')})
        allowed, reason, _ = guard.evaluate(self.origin + '/public/1')
        self.assertFalse(allowed)
        self.assertEqual(reason, reasons.ROBOTS_UNAVAILABLE)

    def test_404_means_no_declared_restrictions(self):
        guard = self._guard({self.origin + '/robots.txt': (404, b'')})
        allowed, _reason, delay = guard.evaluate(self.origin + '/anything')
        self.assertTrue(allowed)
        self.assertIsNone(delay)

    def test_cache_is_used_until_ttl_then_refetched(self):
        opener = FakeOpener({self.origin + '/robots.txt': (200, ROBOTS_BODY)})
        guard = RobotsGuard(ttl_seconds=60, clock=self.clock, url_opener=opener)
        guard.evaluate(self.origin + '/public/1')
        guard.evaluate(self.origin + '/public/2')
        self.assertEqual(len(opener.calls), 1)
        self.clock.advance(61)
        guard.evaluate(self.origin + '/public/3')
        self.assertEqual(len(opener.calls), 2)

    def test_invalidate_forces_refetch(self):
        opener = FakeOpener({self.origin + '/robots.txt': (200, ROBOTS_BODY)})
        guard = RobotsGuard(ttl_seconds=10**9, clock=self.clock,
                            url_opener=opener)
        guard.evaluate(self.origin + '/public/1')
        guard.invalidate(self.origin)
        guard.evaluate(self.origin + '/public/1')
        self.assertEqual(len(opener.calls), 2)

    def test_recovering_from_failure_reopens_after_refresh(self):
        opener = FakeOpener({
            self.origin + '/robots.txt': (503, b'busy')})
        guard = RobotsGuard(ttl_seconds=60, clock=self.clock,
                            url_opener=opener)
        self.assertEqual(guard.evaluate(self.origin + '/public/1')[1],
                         reasons.ROBOTS_UNAVAILABLE)
        opener.responses[self.origin + '/robots.txt'] = (200, ROBOTS_BODY)
        # 缓存未到期前仍按不可用处理，失效后才重新拉取
        self.assertEqual(guard.evaluate(self.origin + '/public/1')[1],
                         reasons.ROBOTS_UNAVAILABLE)
        guard.invalidate(self.origin)
        self.assertTrue(guard.evaluate(self.origin + '/public/1')[0])


if __name__ == '__main__':
    unittest.main()
