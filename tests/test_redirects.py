import tempfile
import unittest
from pathlib import Path

from scope import reasons
from scope.site import serve_pair
from tests.helpers import FakeClock, build_collector, make_grant, page_requests


class RedirectTests(unittest.TestCase):
    def _build(self, grants):
        directory = tempfile.mkdtemp()
        clock = FakeClock()
        collector, registry, store, robots_guard, pacing = build_collector(
            Path(directory) / 'c.sqlite3', clock, grants=grants)
        # 测试站点的 robots 不含 Crawl-delay，避免真实等待
        return collector, registry, store

    def test_redirect_each_hop_passes_both_gates(self):
        with serve_pair() as ((url_a, state_a), (url_b, state_b)):
            state_a['redirects']['/public/old'] = url_b + '/public/new'
            collector, registry, store = self._build([
                make_grant('a', url_a), make_grant('b', url_b)])
            task_id = collector.enqueue(url_a + '/public/old')
            summary = collector.tick()
            self.assertEqual(summary['received'], 1)
            # 两跳各发出一次真实请求（robots.txt 为基础设施请求，不计入）
            self.assertEqual([r['path'] for r in page_requests(state_a)],
                             ['/public/old'])
            self.assertEqual([r['path'] for r in page_requests(state_b)],
                             ['/public/new'])
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'received')
            self.assertEqual(len(task['detail']['hops']), 2)
            page = store.get_page(url_b + '/public/new')
            self.assertIsNotNone(page['body'])

    def test_redirect_leaving_permitted_origin_is_blocked_per_hop(self):
        with serve_pair() as ((url_a, state_a), (url_b, state_b)):
            state_a['redirects']['/public/old'] = url_b + '/public/new'
            # 只授权来源 A：第二跳越界，B 永远不应收到请求
            collector, registry, store = self._build([make_grant('a', url_a)])
            task_id = collector.enqueue(url_a + '/public/old')
            summary = collector.tick()
            self.assertEqual(summary['blocked'], 1)
            self.assertEqual(page_requests(state_b), [])
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.PERMIT_NO_MATCH)
            self.assertIsNone(store.get_page(url_b + '/public/new'))

    def test_redirect_to_robots_disallowed_path_is_blocked(self):
        with serve_pair() as ((url_a, state_a), (url_b, state_b)):
            state_a['redirects']['/public/old'] = url_b + '/private/new'
            collector, registry, store = self._build([
                make_grant('a', url_a),
                make_grant('b', url_b, ('/private/',))])
            task_id = collector.enqueue(url_a + '/public/old')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.ROBOTS_DISALLOW)
            # 许可允许但 robots 禁止，两者不能互相兜底
            self.assertEqual(page_requests(state_b), [])

    def test_sensitive_headers_stripped_on_cross_origin_redirect(self):
        with serve_pair() as ((url_a, state_a), (url_b, state_b)):
            state_a['redirects']['/public/old'] = url_b + '/public/new'
            collector, registry, store = self._build([
                make_grant('a', url_a), make_grant('b', url_b)])
            collector.enqueue(
                url_a + '/public/old',
                headers={'Cookie': 'session=secret',
                         'Authorization': 'Bearer token',
                         'X-Trace': 'keep-me'})
            collector.tick()
            first = page_requests(state_a)[0]
            second = page_requests(state_b)[0]
            self.assertEqual(first['cookie'], 'session=secret')
            self.assertEqual(first['authorization'], 'Bearer token')
            # 跨来源：敏感凭据不得带过去
            self.assertIsNone(second['cookie'])
            self.assertIsNone(second['authorization'])
            # 非敏感自定义头可以继续携带
            self.assertEqual(len(page_requests(state_b)), 1)

    def test_sensitive_headers_kept_for_same_origin_redirect(self):
        with serve_pair() as ((url_a, state_a), _unused):
            state_a['redirects']['/public/old'] = '/public/new'
            collector, registry, store = self._build([make_grant('a', url_a)])
            collector.enqueue(
                url_a + '/public/old',
                headers={'Cookie': 'session=secret'})
            collector.tick()
            self.assertEqual(len(page_requests(state_a)), 2)
            self.assertEqual(page_requests(state_a)[1]['cookie'], 'session=secret')

    def test_redirect_limit_is_enforced(self):
        with serve_pair() as ((url_a, state_a), _unused):
            # 自指重定向，超过跳数上限后停止
            state_a['redirects']['/public/loop'] = '/public/loop'
            collector, registry, store = self._build([make_grant('a', url_a)])
            task_id = collector.enqueue(url_a + '/public/loop')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.REDIRECT_LIMIT)
            self.assertLessEqual(len(page_requests(state_a)), 6)


if __name__ == '__main__':
    unittest.main()
