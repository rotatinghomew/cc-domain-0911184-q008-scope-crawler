"""站点 robots 约定闸门。

关键规则：
- robots 约定与合作许可相互独立，必须同时满足，不能互相替代。
- robots.txt 暂时取不到（网络错误、超时、5xx）按“不可用即禁止”处理，
  绝不解释成允许；仅 404/410 表示站点未声明限制（RFC 9309）。
- 解析 Crawl-delay 作为站点整体访问间隔，交给 SitePacing 执行。
"""

import urllib.robotparser
from urllib.error import HTTPError, URLError

from . import reasons
from .clock import now
from .permission import origin_of

ROBOTS_PATH = '/robots.txt'
# 采集者标识，与 fetcher.GuardedFetcher 的默认 UA 保持一致
DEFAULT_USER_AGENT = 'PartnerCollector/1.0'
_UNAVAILABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class RobotsGuard:
    """按来源缓存 robots 规则；所有判定都区分“禁止”与“不可用”。"""

    def __init__(self, user_agent=DEFAULT_USER_AGENT, ttl_seconds=1800,
                 clock=None, url_opener=None, timeout=5):
        self.user_agent = user_agent
        # robots 规则按 UA 产品名匹配（RFC 9309），版本号不参与规则键
        self.ua_name = user_agent.split('/', 1)[0]
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        # url_opener(url) -> (status, body_bytes)，测试可注入
        self._open = url_opener or self._default_open
        self.timeout = timeout
        # origin -> (parser, crawl_delay, fetched_at, unavailable, status)
        self._cache = {}

    def _default_open(self, url):
        from urllib.request import urlopen
        try:
            with urlopen(url, timeout=self.timeout) as response:
                return response.status, response.read()
        except HTTPError as error:
            # 4xx/5xx 响应体也读出，交由上层按状态码分类
            return error.code, error.read()

    def invalidate(self, origin=None):
        """让一个来源（或全部来源）的缓存失效，下一次判定重新拉取。"""
        if origin is None:
            self._cache.clear()
        else:
            self._cache.pop(origin, None)

    def _refresh(self, origin, at):
        url = origin + ROBOTS_PATH
        try:
            status, body = self._open(url)
        except (URLError, OSError, ValueError):
            # 连接失败、超时等：不可用，fail-closed
            self._cache[origin] = (None, None, at, True, None)
            return self._cache[origin]
        if status in _UNAVAILABLE_STATUSES:
            self._cache[origin] = (None, None, at, True, status)
            return self._cache[origin]
        parser = urllib.robotparser.RobotFileParser()
        if status in (404, 410):
            # 站点明确没有 robots 约定：空规则即不限制
            parser.parse([])
        else:
            parser.parse(body.decode('utf-8', 'replace').splitlines())
        # 用 UA 产品名查询，与 can_fetch 的规则键口径保持一致
        delay = parser.crawl_delay(self.ua_name)
        self._cache[origin] = (parser, delay, at, False, status)
        return self._cache[origin]

    def _entry(self, origin, at):
        cached = self._cache.get(origin)
        if cached is None or (at - cached[2]).total_seconds() >= self.ttl_seconds:
            cached = self._refresh(origin, at)
        return cached

    def evaluate(self, url, at=None):
        """返回 (allowed, reason, crawl_delay_seconds)。"""
        at = at or now(self.clock)
        origin = origin_of(url)
        if origin is None:
            return False, reasons.REDIRECT_BAD_SCHEME, None
        _parser, delay, _fetched, unavailable, status = self._entry(origin, at)
        if unavailable:
            return False, reasons.ROBOTS_UNAVAILABLE, None
        if not _parser.can_fetch(self.ua_name, url):
            return False, reasons.ROBOTS_DISALLOW, delay
        return True, None, delay

    def crawl_delay(self, origin, at=None):
        """某来源当前约定的 Crawl-delay（秒），未声明返回 None。"""
        at = at or now(self.clock)
        _parser, delay, _f, unavailable, _s = self._entry(origin, at)
        if unavailable:
            return None
        return delay
