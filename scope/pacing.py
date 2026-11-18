"""站点整体访问节奏。

约定的访问间隔属于站点整体：多个任务、多个工作协程访问同一来源时共享
同一份额度。重试不重新获得额度——重试请求同样要等间隔。状态持久化在
Store 中，进程重启后继续生效，不靠重启清空限制。
"""

from datetime import timedelta

from . import reasons
from .clock import now
from .permission import origin_of


class SitePacing:
    def __init__(self, store, clock=None, sleep=None):
        self.store = store
        self.clock = clock
        # 可注入 sleep 便于测试；生产默认 time.sleep
        self._sleep = sleep

    def _sleep_impl(self, seconds):
        if self._sleep is not None:
            self._sleep(seconds)
            return
        import time
        time.sleep(seconds)

    def next_allowed(self, origin, at=None):
        """该来源下一次允许发请求的最早时刻；无历史则为当前时刻。"""
        at = at or now(self.clock)
        state = self.store.site_state(origin)
        if state is None or state['last_request_at'] is None:
            return at
        delay = state['crawl_delay'] or 0
        return max(at, state['last_request_at'] + timedelta(seconds=delay))

    def wait_time(self, url, at=None):
        at = at or now(self.clock)
        origin = origin_of(url)
        target = self.next_allowed(origin, at)
        return max(0.0, (target - at).total_seconds())

    def reserve(self, url, crawl_delay=None, at=None):
        """为一次真实请求占用站点额度。

        间隔未到时先等待；无论等待与否，都会把“上次请求时间”推进到实际
        发出时刻，并记录该来源最新的 Crawl-delay。robots 不可用等原因
        导致请求没有发出时，调用方不应调用 reserve，额度不被消耗。
        """
        at = at or now(self.clock)
        origin = origin_of(url)
        target = self.next_allowed(origin, at)
        waited = (target - at).total_seconds()
        if waited > 0:
            self._sleep_impl(waited)
            at = now(self.clock)
        self.store.upsert_site_state(origin, last_request_at=at,
                                     crawl_delay=crawl_delay)
        return {'origin': origin, 'waited_seconds': max(0.0, waited),
                'reason': reasons.PACING_WAIT if waited > 0 else None}
