"""处置原因码与面向运维的解释文本。

所有闸门（来源许可、robots 约定、重定向、站点节奏、保留条款）都只能使用
这里登记的原因码，保证对单个页面的停止原因可以逐条解释。
"""

# 来源许可类
SCHEME_REJECTED = 'scheme_rejected'
PERMIT_NO_MATCH = 'permit_no_match'
PERMIT_NOT_YET_VALID = 'permit_not_yet_valid'
PERMIT_EXPIRED = 'permit_expired'
PERMIT_REVOKED = 'permit_revoked'

# 站点 robots 约定类
ROBOTS_DISALLOW = 'robots_disallow'
ROBOTS_UNAVAILABLE = 'robots_unavailable'

# 重定向类
REDIRECT_LIMIT = 'redirect_limit'
REDIRECT_BAD_SCHEME = 'redirect_bad_scheme'

# 节奏与重试类
PACING_WAIT = 'pacing_wait'
RETRY_WAIT = 'retry_wait'
FETCH_ERROR = 'fetch_error'
UNEXPECTED_CONTENT_TYPE = 'unexpected_content_type'

# 在途撤销与保留条款类
REVOKED_IN_FLIGHT = 'revoked_in_flight'
RETENTION_REDACTED = 'retention_redacted'

REASON_TEXT = {
    SCHEME_REJECTED: '仅允许 http/https 且 URL 不得携带用户名口令，该地址被拒绝',
    PERMIT_NO_MATCH: '当前来源许可中没有覆盖该来源与路径前缀的授权',
    PERMIT_NOT_YET_VALID: '来源许可尚未到达生效时间',
    PERMIT_EXPIRED: '来源许可已超过有效截止时间',
    PERMIT_REVOKED: '来源许可已被合作方撤销，排队中的访问不再发出',
    ROBOTS_DISALLOW: '站点 robots.txt 明确禁止抓取该路径，许可与 robots 需同时满足',
    ROBOTS_UNAVAILABLE: '站点 robots.txt 暂时无法获取，按“不可用即禁止”处理，不解释为允许',
    REDIRECT_LIMIT: '重定向跳数超过上限，停止跟随',
    REDIRECT_BAD_SCHEME: '重定向目标不是 http/https 地址，拒绝前往',
    PACING_WAIT: '站点级访问间隔尚未到达，等待约定节奏，不占用新额度',
    RETRY_WAIT: '请求失败后按退避等待重试，重试仍受同一站点节奏约束',
    FETCH_ERROR: '站点返回错误或连接失败，按可重试性决定重试或终止',
    UNEXPECTED_CONTENT_TYPE: '响应不是 text/html 页面，不入库',
    REVOKED_IN_FLIGHT: '请求在途期间许可失效，响应正文丢弃，仅保留处置记录',
    RETENTION_REDACTED: '按抓取时许可快照中的保留条款，正文已到期脱敏，抓取事实保留',
}


def reason_text(code):
    """返回原因码对应的中文解释，未知码返回码本身。"""
    return REASON_TEXT.get(code, code)
