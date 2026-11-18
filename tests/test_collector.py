import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from scope import reasons
from scope.site import serve
from tests.helpers import FakeClock, build_collector, make_grant, page_requests


class CollectorLifecycleTests(unittest.TestCase):
    def test_queued_revocation_never_sends_request(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            task_id = collector.enqueue(url + '/public/1')
            registry.revoke('a')
            summary = collector.tick()
            self.assertEqual(summary['blocked'], 1)
            self.assertEqual(page_requests(state), [])
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.PERMIT_REVOKED)

    def test_restored_permit_resumes_only_still_valid_tasks(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            task_id = collector.enqueue(url + '/public/1')
            registry.revoke('a')
            collector.tick()
            self.assertEqual(page_requests(state), [])
            # 恢复许可后无需重启进程，下一轮 tick 自动重新推进
            registry.restore('a')
            summary = collector.tick()
            self.assertEqual(summary['received'], 1)
            self.assertEqual(len(page_requests(state)), 1)
            self.assertEqual(store.get_task(task_id)['state'], 'received')

    def test_expired_task_is_not_resumed_after_permit_returns(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            start = clock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            task_id = collector.enqueue(
                url + '/public/1', not_after=start + timedelta(hours=1))
            registry.revoke('a')
            collector.tick()
            clock.advance(2 * 3600)
            registry.restore('a')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'expired')
            self.assertEqual(task['reason'], reasons.PERMIT_EXPIRED)
            self.assertEqual(page_requests(state), [])

    def test_inflight_revocation_keeps_disposition_but_no_body(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            revoked_once = {'done': False}

            def revoke_during_inflight(task, result):
                # 仅模拟一次“在途撤销”，恢复后的重推不再触发
                if not revoked_once['done']:
                    revoked_once['done'] = True
                    registry.revoke('a')

            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)],
                before_store=revoke_during_inflight)
            task_id = collector.enqueue(url + '/public/1')
            summary = collector.tick()
            self.assertEqual(summary['blocked'], 1)
            # 请求确实发出了
            self.assertEqual(len(page_requests(state)), 1)
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.PERMIT_REVOKED)
            # 只留处置记录：丢弃摘要 + 跳数链，不产生可用正文
            self.assertIsNone(store.get_page(url + '/public/1'))
            event = [e for e in store.events_for(url + '/public/1')
                     if e['kind'] == 'inflight_revoked'][0]
            self.assertEqual(len(event['detail']['discarded_digest']), 64)
            self.assertEqual(event['reason'], reasons.REVOKED_IN_FLIGHT)
            # 许可恢复后任务仍有效，重新推进并正常入库
            registry.restore('a')
            collector.tick()
            self.assertEqual(store.get_task(task_id)['state'], 'received')
            self.assertIsNotNone(store.get_page(url + '/public/1')['body'])

    def test_robots_outage_blocks_and_recovers_without_restart(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, robots_guard, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            task_id = collector.enqueue(url + '/public/1')
            state['robots_status'] = 503
            robots_guard.invalidate(url)
            collector.tick()
            self.assertEqual(store.get_task(task_id)['state'], 'blocked')
            self.assertEqual(store.get_task(task_id)['reason'],
                             reasons.ROBOTS_UNAVAILABLE)
            self.assertEqual(page_requests(state), [])
            state['robots_status'] = 200
            robots_guard.invalidate(url)
            collector.tick()
            self.assertEqual(store.get_task(task_id)['state'], 'received')

    def test_retention_redacts_body_but_keeps_fetch_facts(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            start = clock()
            grant = make_grant('a', url, retain_days=1)
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock, grants=[grant])
            collector.enqueue(url + '/public/1')
            collector.tick()
            page = store.get_page(url + '/public/1')
            original_digest = page['digest']
            self.assertIsNotNone(page['body'])
            self.assertEqual(page['permit_snapshot']['grant_id'], 'a')
            # 保留期内不动
            clock.advance(3600)
            self.assertEqual(collector.retention_sweep(), [])
            self.assertIsNotNone(store.get_page(url + '/public/1')['body'])
            # 到期后仅脱敏正文：digest、状态、抓取时刻、许可快照全部保留
            clock.advance(25 * 3600)
            redacted = collector.retention_sweep()
            self.assertEqual(redacted, [url + '/public/1'])
            page = store.get_page(url + '/public/1')
            self.assertIsNone(page['body'])
            self.assertTrue(page['redacted'])
            self.assertEqual(page['digest'], original_digest)
            event = [e for e in store.events_for(url + '/public/1')
                     if e['kind'] == 'retention_redacted'][0]
            self.assertEqual(event['reason'], reasons.RETENTION_REDACTED)
            # 重复执行不会再产生脱敏事件
            self.assertEqual(collector.retention_sweep(), [])

    def test_revocation_does_not_touch_already_received_pages(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url, retain_days=30)])
            collector.enqueue(url + '/public/1')
            collector.tick()
            registry.revoke('a')
            collector.tick()
            page = store.get_page(url + '/public/1')
            self.assertIsNotNone(page['body'])
            self.assertFalse(page['redacted'])

    def test_explain_reports_which_convention_stopped_a_page(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            target = url + '/public/1'
            collector.enqueue(target)
            registry.revoke('a')
            collector.tick()
            explanation = collector.explain(target)
            self.assertEqual(explanation['tasks'][0]['state'], 'blocked')
            self.assertEqual(explanation['tasks'][0]['reason'],
                             reasons.PERMIT_REVOKED)
            self.assertIn('撤销', explanation['tasks'][0]['reason_text'])
            kinds = [event['kind'] for event in explanation['events']]
            self.assertIn('enqueued', kinds)
            self.assertIn('gate_blocked', kinds)

    def test_non_html_response_is_not_stored(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            # 许可与 robots 都覆盖 /robots.txt，但它是 text/plain：
            # 抓取成功却不是 HTML，按策略终止且不入库
            state['robots'] = 'User-agent: PartnerCollector\nAllow: /\n'
            grant = make_grant('a', url, ('/robots.txt',))
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock, grants=[grant])
            task_id = collector.enqueue(url + '/robots.txt')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'],
                             reasons.UNEXPECTED_CONTENT_TYPE)
            self.assertIsNone(store.get_page(url + '/robots.txt'))

    def test_retryable_error_stops_after_max_attempts(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            state['fail_paths'].add('/public/broken')
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)], max_attempts=1,
                backoff_seconds=0.5)
            task_id = collector.enqueue(url + '/public/broken')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['reason'], reasons.FETCH_ERROR)
            self.assertEqual(task['attempts'], 1)
            self.assertIsNone(store.get_page(url + '/public/broken'))

    def test_permit_and_robots_are_independent_gates(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            # 许可明确覆盖 /private/，但 robots 禁止该路径：两者不能互相兜底
            grant = make_grant('a', url, ('/private/',))
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock, grants=[grant])
            task_id = collector.enqueue(url + '/private/1')
            collector.tick()
            task = store.get_task(task_id)
            self.assertEqual(task['state'], 'blocked')
            self.assertEqual(task['reason'], reasons.ROBOTS_DISALLOW)
            self.assertEqual(page_requests(state), [])
            # 反向场景：robots 允许但无许可
            state['robots'] = 'User-agent: PartnerCollector\nAllow: /\n'
            task2 = collector.enqueue(url + '/elsewhere/1')
            collector.tick()
            self.assertEqual(store.get_task(task2)['reason'],
                             reasons.PERMIT_NO_MATCH)
            self.assertEqual(page_requests(state), [])

    def test_inflight_task_survives_process_crash_and_resumes(self):
        with serve() as (url, state):
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, _p = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            task_id = collector.enqueue(url + '/public/1')
            # 模拟进程在请求期间退出：任务停在 in_flight
            store.update_task(task_id, state='in_flight', attempts=1)
            collector.close()

            # 新进程用同一份持久化文件恢复，任务应被重新推进并入库
            collector2, _reg2, store2, _r2, _p2 = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            collector2.tick()
            task = store2.get_task(task_id)
            self.assertEqual(task['state'], 'received')
            self.assertIsNotNone(store2.get_page(url + '/public/1')['body'])

    def test_robots_unavailable_does_not_spend_site_quota(self):
        with serve() as (url, state):
            state['robots'] = ('User-agent: PartnerCollector\n'
                               'Disallow: /private/\nCrawl-delay: 5\n')
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, robots_guard, pacing = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            blocked_id = collector.enqueue(url + '/public/x')
            state['robots_status'] = 503
            robots_guard.invalidate(url)
            collector.tick()
            # 闸门在节奏之前拦截：没有真实请求，站点无节奏状态、无等待
            self.assertIsNone(store.site_state(url))
            self.assertEqual(clock.sleeps, [])
            # 恢复后首个请求无需等待，证明之前没占用额度
            state['robots_status'] = 200
            robots_guard.invalidate(url)
            collector.tick()
            self.assertEqual(clock.sleeps, [])
            self.assertEqual(store.get_task(blocked_id)['state'], 'received')


if __name__ == '__main__':
    unittest.main()
