"""测试辅助：可控时钟与常用对象构造。"""

from datetime import datetime, timedelta, timezone

from scope.collector import Collector
from scope.fetcher import GuardedFetcher
from scope.pacing import SitePacing
from scope.permission import Grant, PermitRegistry
from scope.robots import RobotsGuard
from scope.store import Store


class FakeClock:
    def __init__(self, start=None):
        self.current = start or datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
        self.sleeps = []

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


def make_grant(grant_id, origin, prefixes=('/public/',), **kwargs):
    return Grant(grant_id, origin, tuple(prefixes), **kwargs)


def page_requests(state):
    """测试站把 robots.txt 基础设施请求也记入日志，断言页面请求时过滤掉。"""
    return [item for item in state['requests'] if item['path'] != '/robots.txt']


def build_collector(db_path, clock, grants=None, fetcher=None,
                    robots_guard=None, backoff_seconds=0.01,
                    max_attempts=3, before_store=None):
    registry = PermitRegistry(grants or [], clock=clock)
    store = Store(db_path, clock=clock)
    robots_guard = robots_guard or RobotsGuard(ttl_seconds=10**9, clock=clock)
    pacing = SitePacing(store, clock=clock, sleep=clock.sleep)
    fetcher = fetcher or GuardedFetcher(registry, robots_guard, pacing,
                                        clock=clock)
    collector = Collector(store, registry, robots_guard=robots_guard,
                          pacing=pacing, fetcher=fetcher,
                          backoff_seconds=backoff_seconds,
                          max_attempts=max_attempts, clock=clock,
                          before_store=before_store)
    return collector, registry, store, robots_guard, pacing
