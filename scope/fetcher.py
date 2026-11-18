"""带许可与站点约定的页面抓取器。

抓取行为要点：
- 每一次重定向都是独立的一步，每一步都要同时通过来源许可与 robots 闸门，
  任何一步越界即停止，绝不“先跳了再说”。
- Cookie、Authorization 等敏感凭据只在同一来源内携带；跨来源跳转时剥离，
  不会把合作方站点的凭据带给另一个来源。
- 每个来源的每次实际请求都经过站点节奏闸；重定向到新来源同样排队等待。
- 全程返回结构化跳数记录，供任务处置与运维解释使用。
"""

from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import reasons
from .clock import now
from .permission import origin_of

# 跳转时不得跨来源携带的敏感请求头（小写匹配）
SENSITIVE_HEADERS = ('cookie', 'authorization', 'proxy-authorization')
MAX_REDIRECTS = 5


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


@dataclass
class Hop:
    url: str
    origin: str
    permit_grant_id: str = None
    robots_reason: str = None
    pacing_wait_seconds: float = 0.0
    status: int = None
    location: str = None
    stripped_headers: tuple = ()


@dataclass
class FetchResult:
    ok: bool
    url: str
    reason: str = None
    status: int = None
    body: bytes = None
    content_type: str = None
    hops: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    retryable: bool = False


class FetchError(Exception):
    def __init__(self, reason, retryable=False, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.detail = detail or {}


class GuardedFetcher:
    def __init__(self, registry, robots_guard, pacing, user_agent='PartnerCollector/1.0',
                 max_redirects=MAX_REDIRECTS, timeout=5, clock=None):
        self.registry = registry
        self.robots_guard = robots_guard
        self.pacing = pacing
        self.user_agent = user_agent
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.clock = clock
        self.opener = build_opener(_NoRedirect())

    def _check_gates(self, url, at):
        """双闸门：来源许可与 robots 必须同时满足，互不兜底。"""
        decision = self.registry.evaluate(url, at=at)
        if not decision.allowed:
            return False, decision.reason, None, None
        allowed, robots_reason, delay = self.robots_guard.evaluate(url, at=at)
        if not allowed:
            return False, robots_reason, decision.grant, delay
        return True, None, decision.grant, delay

    @staticmethod
    def _sanitize_headers(headers, from_origin, to_origin):
        """跨来源时剥离敏感头；同来源原样保留。"""
        if from_origin == to_origin:
            return headers, ()
        stripped = tuple(name for name in headers
                         if name.lower() in SENSITIVE_HEADERS)
        return {name: value for name, value in headers.items()
                if name.lower() not in SENSITIVE_HEADERS}, stripped

    def fetch(self, url, headers=None):
        at = now(self.clock)
        headers = dict(headers or {})
        headers.setdefault('User-Agent', self.user_agent)
        current = url
        hops = []
        for step in range(self.max_redirects + 1):
            at = now(self.clock)
            parsed = urlparse(current)
            if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
                return FetchResult(False, current, reasons.REDIRECT_BAD_SCHEME,
                                   hops=hops, detail={'step': step})
            origin = origin_of(current)
            hop = Hop(url=current, origin=origin)
            hops.append(hop)

            allowed, reason, grant, delay = self._check_gates(current, at)
            if grant is not None:
                hop.permit_grant_id = grant.grant_id
            if not allowed:
                hop.robots_reason = reason
                return FetchResult(False, current, reason, hops=hops,
                                   detail={'step': step, 'origin': origin})
            # 站点整体节奏：每跳的每个来源都要排队，重试也走同一入口
            pacing_info = self.pacing.reserve(current, crawl_delay=delay, at=at)
            hop.pacing_wait_seconds = pacing_info['waited_seconds']

            request = Request(current, headers=headers)
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                if error.code in (301, 302, 303, 307, 308):
                    hop.status = error.code
                    hop.location = error.headers.get('Location')
                    location = hop.location
                    next_url = urljoin(current, location)
                    next_origin = origin_of(next_url)
                    if next_origin is None:
                        return FetchResult(False, next_url, reasons.REDIRECT_BAD_SCHEME,
                                           hops=hops, detail={'step': step})
                    headers, stripped = self._sanitize_headers(
                        headers, origin, next_origin)
                    hop.stripped_headers = stripped
                    current = next_url
                    continue
                # 4xx 不重试；5xx 可重试，且重试不额外获得站点额度
                retryable = 500 <= error.code < 600
                raise FetchError(reasons.FETCH_ERROR, retryable=retryable,
                                 detail={'status': error.code, 'step': step})
            except (URLError, OSError, ValueError) as error:
                raise FetchError(reasons.FETCH_ERROR, retryable=True,
                                 detail={'error': str(error), 'step': step})

            with response:
                hop.status = response.status
                content_type = response.headers.get('Content-Type', '')
                if response.headers.get_content_type() != 'text/html':
                    return FetchResult(False, current,
                                       reasons.UNEXPECTED_CONTENT_TYPE,
                                       status=response.status, hops=hops,
                                       detail={'content_type': content_type})
                body = response.read()
            return FetchResult(True, current, status=response.status, body=body,
                               content_type=content_type, hops=hops)

        return FetchResult(False, current, reasons.REDIRECT_LIMIT,
                           hops=hops, detail={'max_redirects': self.max_redirects})
