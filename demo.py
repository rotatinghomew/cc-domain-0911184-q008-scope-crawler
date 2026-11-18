"""端到端演示：许可与站点约定如何驱动真实的请求与入库行为。

全程只访问容器内环回测试站，不访问外网。演示覆盖：
1. 正常许可页面入库；2. robots 禁止/不可用时请求不发出；
3. 跨来源重定向逐跳过闸、敏感头剥离；4. 排队中撤销不发请求；
5. 在途撤销丢弃正文只留处置记录；6. 恢复许可后自动重推；
7. 保留条款到期脱敏但不篡改抓取事实；8. 按页面解释停在哪条约定。
"""

import json
import tempfile
from datetime import timedelta
from pathlib import Path

from scope.clock import now
from scope.collector import Collector
from scope.permission import Grant, PermitRegistry
from scope.reasons import reason_text
from scope.robots import RobotsGuard
from scope.site import serve_pair

# 演示用 robots 不声明 Crawl-delay，保持节奏演示在单测中、演示本身快速
FAST_ROBOTS = 'User-agent: PartnerCollector\nDisallow: /private/\n'


def main():
    with tempfile.TemporaryDirectory() as directory, serve_pair() as (a, b):
        (url_a, state_a), (url_b, state_b) = a, b
        state_a['robots'] = FAST_ROBOTS
        state_b['robots'] = FAST_ROBOTS
        state_a['redirects']['/public/old'] = url_b + '/public/new'

        start = now()
        grant_a = Grant('partner-a', url_a, ('/public/',),
                        valid_from=start - timedelta(minutes=1),
                        valid_until=start + timedelta(days=1),
                        retain_days=0)
        grant_b = Grant('partner-b', url_b, ('/public/',),
                        valid_from=start - timedelta(minutes=1),
                        valid_until=start + timedelta(days=1),
                        retain_days=7)
        grant_revokable = Grant('partner-a-temp', url_a, ('/temp/',),
                                valid_from=start - timedelta(minutes=1),
                                valid_until=start + timedelta(days=1))
        # 专门用于演示在途撤销的许可，与正常 /public/ 许可分开，
        # 恢复后重推不会重复触发撤销
        grant_inflight = Grant('partner-a-inflight', url_a,
                               ('/inflight/',),
                               valid_from=start - timedelta(minutes=1),
                               valid_until=start + timedelta(days=1),
                               retain_days=7)
        registry = PermitRegistry(
            [grant_a, grant_b, grant_revokable, grant_inflight])

        db_path = Path(directory) / 'collector.sqlite3'
        robots_guard = RobotsGuard()
        collector = Collector(db_path, registry, robots_guard=robots_guard,
                              backoff_seconds=0.01)

        t_ok = collector.enqueue(url_a + '/public/event-1')
        t_private = collector.enqueue(url_a + '/private/secret')
        t_redirect = collector.enqueue(
            url_a + '/public/old',
            headers={'Cookie': 'session=demo', 'Authorization': 'Bearer demo-token'})
        t_revoked = collector.enqueue(url_a + '/temp/page')
        # 独立前缀 /inflight/，只由专门的 inflight 许可覆盖
        t_inflight = collector.enqueue(url_a + '/inflight/page')

        # 排队中撤销：该任务的请求永远不应发出
        registry.revoke('partner-a-temp')

        # 在途撤销：该任务响应已返回、入库前许可失效，只触发一次
        inflight_revoked = {'done': False}

        def revoke_on_inflight(task, result):
            if task['task_id'] == t_inflight and not inflight_revoked['done']:
                inflight_revoked['done'] = True
                registry.revoke('partner-a-inflight')

        collector.before_store = revoke_on_inflight

        # robots 暂时不可用：不能解释成允许，第一轮不应有任何页面请求
        state_a['robots_status'] = 503
        robots_guard.invalidate(url_a)
        t_robots_down = collector.enqueue(url_a + '/public/event-2')

        first = collector.tick()

        # 恢复 robots（许可仍有效），在途撤销钩子将在 inflight 任务返回时触发
        state_a['robots_status'] = 200
        robots_guard.invalidate(url_a)
        second = collector.tick()

        # 恢复在途撤销的许可后，只重新推进仍有效的任务
        registry.restore('partner-a-inflight')
        third = collector.tick()
        fourth = collector.tick()

        # 保留条款：grant_a 的 retain_days=0，正文此刻即到期，脱敏但留事实
        redacted = collector.retention_sweep()

        report = {
            'tick_rounds': [first, second, third, fourth],
            'stats': collector.stats(),
            'redacted_by_retention': redacted,
            'site_a_requests': state_a['requests'],
            'site_b_requests': state_b['requests'],
            'explain_private': collector.explain(url_a + '/private/secret'),
            'explain_redirect_source': collector.explain(url_a + '/public/old'),
            'explain_redirect_target': collector.explain(url_b + '/public/new'),
            'explain_inflight': collector.explain(url_a + '/inflight/page'),
            'explain_queued_revoked': collector.explain(
                url_a + '/temp/page'),
            'explain_robots_down': collector.explain(url_a + '/public/event-2'),
            'reason_glossary': {
                'permit_revoked': reason_text('permit_revoked'),
                'robots_unavailable': reason_text('robots_unavailable'),
                'robots_disallow': reason_text('robots_disallow'),
                'revoked_in_flight': reason_text('revoked_in_flight'),
                'retention_redacted': reason_text('retention_redacted')},
        }
        collector.close()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
