"""采集调度器：把许可/robots/节奏闸门接到任务队列与入库行为上。

处置原则（许可变化时行为必须真正变化）：
- 已排队任务在发出前逐次过双闸门，失去许可的访问不会发出，也不占用站点额度；
- 已发出的请求返回后再次核对许可：在途撤销的响应只留处置记录（含丢弃摘要、
  跳数链），不产生可用正文；
- 已确认接收的页面按抓取时许可快照中的保留条款处理，到期仅脱敏正文，
  不追溯篡改当时的抓取事实；
- 软阻断（撤销、robots 不可用等）保留任务，恢复许可后由 tick 重新推进仍有效
  的任务；所有状态持久化，进程重启不清空限制。
"""

import hashlib
from dataclasses import asdict
from datetime import timedelta

from . import reasons
from .clock import now, to_iso
from .fetcher import FetchError
from .pacing import SitePacing
from .robots import RobotsGuard
from .store import Store


class Collector:
    def __init__(self, store_or_path, registry, robots_guard=None, pacing=None,
                 fetcher=None, max_attempts=3, backoff_seconds=1.0,
                 clock=None, sleep=None, before_store=None):
        self.clock = clock
        self.registry = registry
        self.store = store_or_path if isinstance(store_or_path, Store) \
            else Store(store_or_path, clock=clock)
        self.robots_guard = robots_guard or RobotsGuard(clock=clock)
        self.pacing = pacing or SitePacing(self.store, clock=clock, sleep=sleep)
        self.fetcher = fetcher or self._build_fetcher(sleep)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        # 测试钩子：抓取成功、入库前回调（可在此撤销许可模拟在途变化）
        self.before_store = before_store
        # 敏感凭据只保存在内存，不入库；重启后仅以默认 UA 继续
        self._headers = {}

    def _build_fetcher(self, sleep):
        from .fetcher import GuardedFetcher
        return GuardedFetcher(self.registry, self.robots_guard, self.pacing,
                              clock=self.clock)

    def close(self):
        self.store.close()

    # ---- 入队 ----------------------------------------------------------
    def enqueue(self, url, not_after=None, headers=None):
        task_id = self.store.create_task(url, not_after=not_after)
        if headers:
            self._headers[task_id] = dict(headers)
        self.store.add_event(url, 'enqueued', detail={'task_id': task_id},
                             task_id=task_id)
        return task_id

    # ---- 调度 ----------------------------------------------------------
    def tick(self):
        """推进所有到期任务一次，返回本轮处置摘要。"""
        at = now(self.clock)
        summary = {'advanced': 0, 'blocked': 0, 'deferred': 0,
                   'received': 0, 'failed': 0}
        for task in self.store.workable_tasks(at):
            summary['advanced'] += 1
            outcome = self._process(task, at)
            summary[outcome] = summary.get(outcome, 0) + 1
        return summary

    def _process(self, task, at):
        url = task['url']
        # 任务自身的有效截止时间：过期任务不再推进
        if task['not_after'] is not None and at >= task['not_after']:
            self.store.update_task(task['task_id'], state='expired',
                                   reason=reasons.PERMIT_EXPIRED)
            self.store.add_event(url, 'task_expired', reasons.PERMIT_EXPIRED,
                                 {'not_after': to_iso(task['not_after'])},
                                 task['task_id'])
            return 'failed'

        # 闸门一：来源许可
        decision = self.registry.evaluate(url, at=at)
        if not decision.allowed:
            return self._soft_block(task, decision.reason, 'gate_blocked', at)
        # 闸门二：robots 约定（与许可独立，缺一不可）
        allowed, robots_reason, _delay = self.robots_guard.evaluate(url, at=at)
        if not allowed:
            return self._soft_block(task, robots_reason, 'gate_blocked', at)

        # 站点节奏：间隔未到则等待同一站点的额度，等待本身不产生请求；
        # 重试走同一入口，不重新获得额度
        wait_seconds = self.pacing.wait_time(url, at=at)
        if wait_seconds > 0:
            self.store.add_event(url, 'pacing_wait', reasons.PACING_WAIT,
                                 {'wait_seconds': round(wait_seconds, 6)},
                                 task['task_id'])

        return self._dispatch(task, at)

    def _soft_block(self, task, reason, kind, at):
        self.store.update_task(task['task_id'], state='blocked', reason=reason,
                               due_at=at)
        self.store.add_event(task['url'], kind, reason,
                             {'task_id': task['task_id']}, task['task_id'])
        return 'blocked'

    def _dispatch(self, task, at):
        url = task['url']
        task_id = task['task_id']
        attempts = task['attempts'] + 1
        self.store.update_task(task_id, state='in_flight', attempts=attempts)
        self.store.add_event(url, 'fetch_attempt',
                             detail={'attempt': attempts}, task_id=task_id)
        try:
            result = self.fetcher.fetch(url, headers=self._headers.get(task_id))
        except FetchError as error:
            return self._handle_fetch_error(task, error, at)
        if not result.ok:
            # 重定向越界等策略性阻断：保留任务，许可恢复后可重新推进
            return self._soft_block(task, result.reason, 'gate_blocked', at)

        # 在途撤销核对：以响应最终 URL 按当前许可再判一次
        if self.before_store is not None:
            self.before_store(task, result)
        recheck = self.registry.evaluate(result.url, at=now(self.clock))
        if not recheck.allowed:
            return self._handle_inflight_revocation(task, result, recheck.reason)

        return self._store_received(task, result, recheck.grant)

    def _handle_fetch_error(self, task, error, at):
        task_id = task['task_id']
        # task 是抓取前的快照，实际尝试次数以库中刚更新的值为准
        attempts_done = self.store.get_task(task_id)['attempts']
        if error.retryable and attempts_done < self.max_attempts:
            # 退避从请求实际结束的当前时刻起算；节奏等待耗时已计入时钟
            now_at = now(self.clock)
            backoff = self.backoff_seconds * (2 ** (attempts_done - 1))
            due = now_at + timedelta(seconds=backoff)
            self.store.update_task(task_id, state='queued', due_at=due,
                                   reason=reasons.RETRY_WAIT)
            self.store.add_event(task['url'], 'retry_scheduled',
                                 reasons.RETRY_WAIT,
                                 {'backoff_seconds': backoff,
                                  'attempts': attempts_done,
                                  'detail': error.detail}, task_id)
            return 'deferred'
        self.store.update_task(task_id, state='failed', reason=error.reason,
                               detail=error.detail)
        self.store.add_event(task['url'], 'task_failed', error.reason,
                             error.detail, task_id)
        return 'failed'

    def _handle_inflight_revocation(self, task, result, reason):
        """在途撤销：正文丢弃，只保留必要处置记录。"""
        discarded_digest = hashlib.sha256(result.body).hexdigest()
        detail = {'final_url': result.url, 'discarded_digest': discarded_digest,
                  'hops': [asdict(hop) for hop in result.hops],
                  'status': result.status}
        self.store.update_task(task['task_id'], state='blocked', reason=reason,
                               detail=detail)
        self.store.add_event(task['url'], 'inflight_revoked',
                             reasons.REVOKED_IN_FLIGHT, detail, task['task_id'])
        return 'blocked'

    def _store_received(self, task, result, grant):
        fetched_at = now(self.clock)
        retain_until = self._retain_until(grant, fetched_at)
        digest = self.store.save_page(
            result.url, result.body, result.status, result.content_type,
            grant.snapshot(), retain_until, fetched_at)
        detail = {'digest': digest, 'final_url': result.url,
                  'hops': [asdict(hop) for hop in result.hops],
                  'retain_until': to_iso(retain_until)}
        self.store.update_task(task['task_id'], state='received',
                               reason=None, detail=detail)
        self.store.add_event(result.url, 'stored', detail=detail,
                             task_id=task['task_id'])
        return 'received'

    @staticmethod
    def _retain_until(grant, fetched_at):
        if grant.retain_until is not None:
            return grant.retain_until
        if grant.retain_days is not None:
            return fetched_at + timedelta(days=grant.retain_days)
        return None

    # ---- 保留条款 ------------------------------------------------------
    def retention_sweep(self):
        """按抓取时快照的保留截止时间脱敏到期正文，抓取事实保持不变。"""
        at = now(self.clock)
        redacted = []
        for page in self.store.retention_due(at):
            self.store.redact_page(page['url'])
            self.store.add_event(
                page['url'], 'retention_redacted', reasons.RETENTION_REDACTED,
                {'digest': page['digest'], 'permit_snapshot': page['permit_snapshot']})
            redacted.append(page['url'])
        return redacted

    # ---- 运维解释 ------------------------------------------------------
    def explain(self, url):
        """按具体页面给出完整约定处置链，每条事件附中文解释。

        重定向任务的入库与存储事件登记在最终 URL 下；这里通过任务详情里的
        跳数链自动关联最终页面，保证从源 URL 也能解释完整去向。
        """
        tasks = [t for t in self.store.list_tasks() if t['url'] == url]
        page = self.store.get_page(url)
        event_urls = [url]
        for task in tasks:
            detail = task.get('detail') or {}
            final_url = detail.get('final_url')
            if final_url and final_url != url:
                event_urls.append(final_url)
                if page is None:
                    page = self.store.get_page(final_url)
        seen = set()
        events = []
        for event_url in event_urls:
            for event in self.store.events_for(event_url):
                if event['event_id'] not in seen:
                    seen.add(event['event_id'])
                    events.append(event)
        events.sort(key=lambda event: event['created_at'])
        return {
            'url': url,
            'page_url': None if page is None else page['url'],
            'page': None if page is None else {
                'digest': page['digest'], 'status': page['status'],
                'redacted': bool(page['redacted']),
                'retain_until': to_iso(page['retain_until']),
                'permit_snapshot': page['permit_snapshot'],
                'fetched_at': to_iso(page['fetched_at']),
                'has_body': page['body'] is not None,
            },
            'tasks': [{'task_id': t['task_id'], 'state': t['state'],
                       'reason': t['reason'],
                       'reason_text': reasons.reason_text(t['reason'])
                       if t['reason'] else None,
                       'attempts': t['attempts'],
                       'due_at': to_iso(t['due_at']),
                       'not_after': to_iso(t['not_after'])} for t in tasks],
            'events': [{'kind': e['kind'], 'reason': e['reason'],
                        'reason_text': reasons.reason_text(e['reason'])
                        if e['reason'] else None,
                        'detail': e['detail'],
                        'created_at': to_iso(e['created_at'])} for e in events],
        }

    def stats(self):
        counts = {}
        for task in self.store.list_tasks():
            counts[task['state']] = counts.get(task['state'], 0) + 1
        pages = self.store.list_pages()
        return {'tasks': counts,
                'pages': len(pages),
                'redacted_pages': sum(1 for page in pages if page['redacted'])}
