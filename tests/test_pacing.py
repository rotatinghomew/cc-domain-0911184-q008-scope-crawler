import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from scope.collector import Collector
from scope.fetcher import GuardedFetcher
from scope.pacing import SitePacing
from scope.robots import RobotsGuard
from scope.site import serve
from scope.store import Store
from tests.helpers import FakeClock, build_collector, make_grant, page_requests

ROBOTS_WITH_DELAY = ('User-agent: PartnerCollector\n'
                     'Disallow: /private/\nCrawl-delay: 2\n')


class PacingTests(unittest.TestCase):
    def test_crawl_delay_is_shared_across_tasks_of_one_site(self):
        with serve() as (url, state):
            state['robots'] = ROBOTS_WITH_DELAY
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _robots, pacing = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            collector.enqueue(url + '/public/1')
            collector.enqueue(url + '/public/2')
            collector.enqueue(url + '/public/3')
            # 非阻塞 tick：一轮内同站点首个任务发送，其余按节奏延后；
            # 逐轮推进直至全部完成，每轮真实请求前都等同一份站点间隔
            for _ in range(3):
                collector.tick()
            self.assertEqual(clock.sleeps, [2.0, 2.0])
            self.assertEqual(len(page_requests(state)), 3)
            site = store.site_state(url)
            self.assertEqual(site['crawl_delay'], 2.0)
            # 相邻请求的持久化时间戳相差恰好一个约定间隔
            self.assertTrue(all(t['state'] == 'received'
                                for t in store.list_tasks()))

    def test_waiting_does_not_spend_quota_and_blocked_tasks_send_nothing(self):
        with serve() as (url, state):
            state['robots'] = ROBOTS_WITH_DELAY
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            collector, registry, store, _r, pacing = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)])
            # 无许可任务：被闸门挡住，不调用 reserve，不推进站点时间
            collector.enqueue(url + '/private/1')
            collector.tick()
            self.assertEqual(clock.sleeps, [])
            self.assertIsNone(store.site_state(url))
            self.assertEqual(page_requests(state), [])

    def test_retry_uses_same_site_quota_no_extra_budget(self):
        with serve() as (url, state):
            state['robots'] = ROBOTS_WITH_DELAY
            state['fail_paths'].add('/public/flaky')
            directory = tempfile.mkdtemp()
            clock = FakeClock()
            # 退避 1 秒 < 站点间隔 2 秒：重试到期时仍在节奏窗口内，
            # 必须继续等满同一份站点额度，证明重试不重新获得额度
            collector, registry, store, _r, pacing = build_collector(
                Path(directory) / 'c.sqlite3', clock,
                grants=[make_grant('a', url)], backoff_seconds=1,
                max_attempts=2)
            task_id = collector.enqueue(url + '/public/flaky')
            first = collector.tick()
            self.assertEqual(first['deferred'], 1)
            self.assertEqual(store.get_task(task_id)['attempts'], 1)
            first_request_at = store.site_state(url)['last_request_at']
            # 退避 t=1 到期，但站点节奏要到 t=2 才允许下一次请求
            self.assertEqual(store.get_task(task_id)['due_at'],
                             first_request_at + timedelta(seconds=1))

            # t=1：退避刚结束，重试前还须等待 1 秒站点节奏
            clock.advance(1)
            self.assertAlmostEqual(pacing.wait_time(url + '/public/flaky'), 1.0)
            second = collector.tick()
            self.assertEqual(second['failed'], 1)
            self.assertEqual(clock.sleeps, [1.0])
            self.assertEqual(len(page_requests(state)), 2)
            self.assertEqual(store.get_task(task_id)['attempts'], 2)
            # 第二次请求落在 t=2，恰好一个完整站点间隔
            self.assertEqual(store.site_state(url)['last_request_at'],
                             first_request_at + timedelta(seconds=2))

    def test_pacing_and_block_state_survive_process_restart(self):
        with serve() as (url, state):
            state['robots'] = ROBOTS_WITH_DELAY
            directory = tempfile.mkdtemp()
            db_path = Path(directory) / 'c.sqlite3'
            clock = FakeClock()
            collector, registry, store, robots_guard, _p = build_collector(
                db_path, clock, grants=[make_grant('a', url)])
            task_id = collector.enqueue(url + '/public/1')
            blocked_id = collector.enqueue(url + '/private/1')
            collector.tick()
            self.assertEqual(store.get_task(blocked_id)['state'], 'blocked')
            last_request = store.site_state(url)['last_request_at']
            collector.close()

            # 模拟进程重启：新对象、同一份持久化文件
            clock2 = FakeClock(start=clock.current)
            store2 = Store(db_path, clock=clock2)
            from scope.permission import PermitRegistry
            registry2 = PermitRegistry([make_grant('a', url)], clock=clock2)
            robots2 = RobotsGuard(ttl_seconds=10**9, clock=clock2)
            pacing2 = SitePacing(store2, clock=clock2, sleep=clock2.sleep)
            fetcher2 = GuardedFetcher(registry2, robots2, pacing2, clock=clock2)
            collector2 = Collector(store2, registry2, robots_guard=robots2,
                                   pacing=pacing2, fetcher=fetcher2, clock=clock2)
            # 重启后节奏不重置：仍记得上次请求时间，新任务要继续等间隔
            self.assertEqual(store2.site_state(url)['last_request_at'],
                             last_request)
            collector2.enqueue(url + '/public/2')
            collector2.tick()
            self.assertEqual(clock2.sleeps, [2.0])
            # 被阻断任务的状态与原因同样保留
            self.assertEqual(store2.get_task(blocked_id)['state'], 'blocked')
            collector2.close()


if __name__ == '__main__':
    unittest.main()
