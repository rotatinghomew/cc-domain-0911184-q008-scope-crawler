"""来源许可模型。

- ``Permit``：项目原有的静态来源/路径前缀许可，保持兼容。
- ``Grant``：带来源、路径前缀、有效时间窗口与保留条款的单条合作许可。
- ``PermitRegistry``：可撤销、可恢复、可替换的许可注册表，判定时只依据
  当前生效的授权，且与 robots 约定相互独立、需要同时满足。
"""

from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import reasons
from .clock import now


class Permit:
    def __init__(self, origins):
        self.origins = {origin.rstrip('/'): tuple(prefixes) for origin, prefixes in origins.items()}

    def allowed(self, url):
        parsed = urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return False
        return any(parsed.path.startswith(prefix) for prefix in self.origins.get(origin, ()))


def origin_of(url):
    """从 URL 提取来源（scheme://host:port），非法 URL 返回 None。"""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f'{parsed.scheme}://{parsed.netloc}'


@dataclass(frozen=True)
class Grant:
    """一条合作许可。

    valid_from / valid_until 为 UTC 时间窗口：生效时刻含、截止时刻不含。
    retain_days 为抓取成功后正文在库中的保留天数（保留条款），为 None 时
    表示长期保留；retain_until 可直接给出绝对保留截止时间。
    revoked 为合作方撤销标记，撤销判定优先于时间窗口。
    """

    grant_id: str
    origin: str
    path_prefixes: tuple
    valid_from: object = None
    valid_until: object = None
    retain_days: float = None
    retain_until: object = None
    revoked: bool = False

    def covers(self, parsed):
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin != self.origin:
            return False
        return any(parsed.path.startswith(prefix) for prefix in self.path_prefixes)

    def snapshot(self):
        """返回可 JSON 序列化的许可快照，随抓取结果固化保留条款。"""
        return {
            'grant_id': self.grant_id,
            'origin': self.origin,
            'path_prefixes': list(self.path_prefixes),
            'retain_days': self.retain_days,
        }


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = None
    grant: Grant = None
    detail: dict = field(default_factory=dict)


class PermitRegistry:
    """许可注册表。许可按来源+路径+时间窗口限定，支持撤销与恢复。"""

    def __init__(self, grants=(), clock=None):
        self.clock = clock
        self._grants = {}
        for grant in grants:
            self.add(grant)

    def add(self, grant):
        self._grants[grant.grant_id] = grant

    def remove(self, grant_id):
        self._grants.pop(grant_id, None)

    def replace_all(self, grants):
        self._grants = {grant.grant_id: grant for grant in grants}

    def get(self, grant_id):
        return self._grants.get(grant_id)

    def revoke(self, grant_id):
        grant = self._grants.get(grant_id)
        if grant is None:
            raise KeyError(grant_id)
        self._grants[grant_id] = Grant(**{**grant.__dict__, 'revoked': True})

    def restore(self, grant_id):
        grant = self._grants.get(grant_id)
        if grant is None:
            raise KeyError(grant_id)
        self._grants[grant_id] = Grant(**{**grant.__dict__, 'revoked': False})

    def grants_for(self, url):
        """返回覆盖该 URL 的全部许可（含未生效/已过期/已撤销），供运维解释。"""
        parsed = urlparse(url)
        return [grant for grant in self._grants.values() if grant.covers(parsed)]

    def evaluate(self, url, at=None):
        """按当前注册表判定 URL 是否有有效许可。

        判定顺序：URL 形态 -> 是否存在覆盖许可 -> 撤销 -> 时间窗口。
        任何一项不通过都给出明确原因码，绝不与 robots 互相兜底。
        """
        at = at or now(self.clock)
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return Decision(False, reasons.SCHEME_REJECTED)
        matches = [grant for grant in self._grants.values() if grant.covers(parsed)]
        if not matches:
            return Decision(False, reasons.PERMIT_NO_MATCH,
                            detail={'origin': origin_of(url)})
        for grant in matches:
            if grant.revoked:
                continue
            if grant.valid_from is not None and at < grant.valid_from:
                continue
            if grant.valid_until is not None and at >= grant.valid_until:
                continue
            return Decision(True, grant=grant)
        # 存在覆盖许可但全部不可用，给出最贴切的原因
        if any(grant.revoked for grant in matches):
            reason = reasons.PERMIT_REVOKED
        elif any(grant.valid_from is not None and at < grant.valid_from for grant in matches):
            reason = reasons.PERMIT_NOT_YET_VALID
        else:
            reason = reasons.PERMIT_EXPIRED
        return Decision(False, reason, detail={
            'grant_ids': [grant.grant_id for grant in matches]})
