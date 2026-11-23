"""断网补传服务的伪连接集成测试。

不依赖 PostgreSQL/Redis：用 FakeConn 精确模拟 overflow_backfill 用到的 SQL 形状，
覆盖挂起、幂等重试、盘点自动退回、结案仅留差异等关键业务规则。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from fastapi import HTTPException
except ModuleNotFoundError:  # 本地无第三方依赖时使用等价轻量桩
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

from src import overflow_backfill as ob  # noqa: E402
from src.overflow_rules import CloseoutStatus, WindowStatus  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)


def at(minute):
    return T0 + timedelta(minutes=minute)


class Model:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        s = self.state
        q = ' '.join(sql.split())
        if 'FOR UPDATE' in q:
            return FakeResult([s['window'].copy()])
        if 'FROM overflow_gate_batch' in q:
            if 'ANY(%s)' in q:
                return FakeResult([b for b in s['batches'].values() if b['receipt'] in params[1]])
            if 'count(*)' in q:
                return FakeResult([{'total': 0}])
            return FakeResult(list(s['batches'].values()))
        if 'DISTINCT ON' in q:
            booked = set()
            for bid, batch in s['backfill_by_id'].items():
                if batch['status'] in ('applied', 'variance_only'):
                    booked.update(r['receipt'] for r in s['bf_records'][bid])
            seen = {}
            for row in s['bf_rows']:
                if row['receipt'] in params[1] and row['receipt'] in booked:
                    seen[row['receipt']] = row
            return FakeResult(list(seen.values()))
        if 'FROM overflow_backfill_record WHERE backfill_id=%s ORDER BY id' in q:
            return FakeResult(list(s['bf_records'].get(params[0], [])))
        if 'FROM overflow_window_pause' in q:
            return FakeResult([{'paused_at': p[0], 'resumed_at': p[1]} for p in s['pauses']])
        if 'FROM overflow_closeout' in q:
            return FakeResult([s['revision']] if s['revision'] else [])
        if 'FROM overflow_backfill_batch' in q:
            return FakeResult([b for b in s['backfills']
                               if b['window_id'] == params[0] and b['reference'] == params[1]])
        if 'INSERT INTO overflow_gate_batch' in q:
            gid = s['next_gate_id']
            s['next_gate_id'] += 1
            wid, receipt, admitted, released, parked, actor, occurred = params[:7]
            s['batches'][gid] = {'id': gid, 'window_id': wid, 'receipt': receipt,
                                 'admitted': admitted, 'released': released, 'parked_after': parked,
                                 'recorded_by': actor, 'occurred_at': occurred, 'source': 'backfill'}
            return FakeResult([{'id': gid}])
        if 'UPDATE overflow_gate_batch SET parked_after' in q:
            s['batches'][params[1]]['parked_after'] = params[0]
            return FakeResult([])
        if 'UPDATE overflow_window SET admitted=%s' in q:
            s['window']['admitted'], s['window']['released'], s['window']['peak_parked'] = params[:3]
            return FakeResult([])
        if 'INSERT INTO overflow_backfill_batch' in q:
            bid = s['next_bf_id']
            s['next_bf_id'] += 1
            row = dict(zip(['window_id', 'reference', 'status', 'reason', 'admitted_delta',
                            'released_delta', 'record_count', 'submitted_by'], params))
            row['id'] = bid
            s['backfills'].append(row)
            s['bf_records'][bid] = []
            s['backfill_by_id'][bid] = row
            return FakeResult([row])
        if 'INSERT INTO overflow_backfill_record' in q:
            bid, _wid, receipt, _occurred, admitted, released, gate_batch_id = params
            record = {'receipt': receipt, 'admitted': admitted, 'released': released,
                      'gate_batch_id': gate_batch_id}
            s['bf_records'][bid].append(record)
            s['bf_rows'].append(record)
            return FakeResult([])
        if 'UPDATE overflow_closeout SET status=%s' in q:
            s['revision']['status'] = params[0]
            s['revision']['admission_delta'] = params[1]
            s['revision']['release_delta'] = params[2]
            return FakeResult([])
        if 'count(*) AS total FROM overflow_backfill_record' in q:
            return FakeResult([{'total': len(s['bf_records'][params[0]])}])
        if 'INSERT INTO audit_event' in q:
            s['audit'].append(params)
            return FakeResult([])
        raise AssertionError('unhandled SQL: ' + q[:100])


def make_state():
    return {
        'window': {'id': 1, 'status': WindowStatus.OPEN.value, 'starts_at': T0,
                   'ends_at': at(120), 'opened_at': T0, 'bay_limit': 5,
                   'admitted': 5, 'released': 0, 'peak_parked': 5},
        'batches': {1: {'id': 1, 'receipt': 'G-1', 'admitted': 5, 'released': 0,
                        'occurred_at': at(5), 'parked_after': 5}},
        'pauses': [], 'revision': None, 'backfills': [], 'bf_records': {}, 'bf_rows': [],
        'backfill_by_id': {}, 'next_gate_id': 2, 'next_bf_id': 1, 'audit': [],
    }


def backfill_item(reference, records):
    return Model(reference=reference, records=[
        Model(receipt=receipt, occurred_at=occurred, admitted=admitted, released=released)
        for receipt, occurred, admitted, released in records])


class BackfillServiceTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        ob.connect = lambda: FakeConn(self.state)
        ob.lock_window = lambda conn, wid: self.state['window']
        ob.audit = lambda conn, *args, **kwargs: self.state['audit'].append(args)

    def test_late_exit_then_admission_is_inserted_and_totals_recomputed(self):
        row = ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        self.assertEqual(row['status'], 'applied')
        self.assertEqual((self.state['window']['admitted'], self.state['window']['released']), (7, 2))
        self.assertEqual(self.state['window']['peak_parked'], 5)
        # 在线旧流水的登记后在场也按过闸时间重算。
        self.assertEqual(self.state['batches'][1]['parked_after'], 5)

    def test_same_backfill_reference_retry_is_idempotent(self):
        first = ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        batch_count = len(self.state['batches'])
        again = ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(len(self.state['batches']), batch_count)

    def test_over_quota_suspends_without_changing_counts(self):
        row = ob.submit_backfill(1, backfill_item('BF-2', [('DEV-9', at(10), 1, 0)]),
                                 'operator@metro.demo')
        self.assertEqual(row['status'], 'suspended')
        self.assertEqual(self.state['window']['admitted'], 5)
        self.assertEqual(len(self.state['batches']), 1)
        # 挂起明细仍然留痕，供闸机页面查询原因。
        self.assertTrue(self.state['bf_records'][row['id']])

    def test_exit_exceeding_parked_suspends(self):
        row = ob.submit_backfill(1, backfill_item('BF-3', [('DEV-8', at(10), 0, 9)]),
                                 'operator@metro.demo')
        self.assertEqual(row['status'], 'suspended')
        self.assertEqual(self.state['window']['released'], 0)

    def test_applied_receipt_in_another_batch_is_idempotent(self):
        ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        row = ob.submit_backfill(1, backfill_item('BF-2', [('DEV-1', at(10), 0, 2)]),
                                 'operator@metro.demo')
        self.assertEqual(row['status'], 'applied')
        self.assertEqual((row['admitted_delta'], row['released_delta']), (0, 0))

    def test_conflicting_receipt_counts_are_rejected(self):
        ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        with self.assertRaises(HTTPException) as ctx:
            ob.submit_backfill(1, backfill_item('BF-3', [('DEV-1', at(10), 0, 3)]),
                               'operator@metro.demo')
        self.assertEqual(ctx.exception.status_code, 409)

    def test_mixed_duplicate_and_fresh_records_only_books_fresh(self):
        first = ob.submit_backfill(1, backfill_item('BF-1', [
            ('DEV-1', at(10), 0, 2), ('DEV-2', at(20), 2, 0)]), 'operator@metro.demo')
        before = self.state['window']['released']
        row = ob.submit_backfill(1, backfill_item('BF-4', [
            ('DEV-2', at(20), 2, 0), ('DEV-4', at(40), 0, 1)]), 'operator@metro.demo')
        self.assertEqual(row['status'], 'applied')
        self.assertEqual(self.state['window']['released'], before + 1)
        self.assertEqual(self.state['window']['admitted'], 7)
        # 重发流水关联原在线行，新流水关联新建在线行；明细均可追溯。
        records = self.state['bf_records'][row['id']]
        linked = {r['receipt']: r['gate_batch_id'] for r in records}
        self.assertIsNotNone(linked['DEV-4'])
        self.assertIn(linked['DEV-2'], self.state['batches'])
        first_records = self.state['bf_records'][first['id']]
        self.assertTrue(all(r['gate_batch_id'] is not None for r in first_records))

    def test_suspended_receipt_can_be_retried_after_capacity_frees(self):
        suspended = ob.submit_backfill(1, backfill_item('BF-5', [('DEV-9', at(10), 1, 0)]),
                                      'operator@metro.demo')
        self.assertEqual(suspended['status'], 'suspended')
        applied = ob.submit_backfill(1, backfill_item('BF-6', [
            ('DEV-X', at(15), 0, 1), ('DEV-9', at(20), 1, 0)]), 'operator@metro.demo')
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual((self.state['window']['admitted'], self.state['window']['released']), (6, 1))

    def test_ready_closeout_is_auto_returned_with_new_deltas(self):
        self.state['revision'] = {'id': 7, 'status': CloseoutStatus.READY.value,
                                  'counted_admitted': 5, 'counted_released': 0,
                                  'admission_delta': 0, 'release_delta': 0}
        ob.submit_backfill(1, backfill_item('BF-7', [
            ('DEV-5', at(10), 0, 2), ('DEV-6', at(20), 2, 0)]), 'operator@metro.demo')
        self.assertEqual(self.state['revision']['status'], CloseoutStatus.RETURNED.value)
        # 差额口径沿用交班盘点：台账数 - 闸机累计。
        self.assertEqual((self.state['revision']['admission_delta'],
                          self.state['revision']['release_delta']), (-2, -2))

    def test_no_change_backfill_keeps_review_pending(self):
        self.state['revision'] = {'id': 3, 'status': CloseoutStatus.READY.value,
                                  'counted_admitted': 5, 'counted_released': 0,
                                  'admission_delta': 0, 'release_delta': 0}
        row = ob.submit_backfill(1, backfill_item('BF-8', [('G-1', at(5), 5, 0)]),
                                 'operator@metro.demo')
        self.assertEqual(row['status'], 'applied')
        self.assertEqual(self.state['revision']['status'], CloseoutStatus.READY.value)

    def test_reconciled_window_keeps_closeout_and_records_variance_only(self):
        self.state['window']['status'] = WindowStatus.RECONCILED.value
        self.state['revision'] = {'id': 9, 'status': CloseoutStatus.ACCEPTED.value,
                                  'counted_admitted': 5, 'counted_released': 0,
                                  'admission_delta': 0, 'release_delta': 0}
        row = ob.submit_backfill(1, backfill_item('BF-9', [
            ('G-1', at(5), 5, 0), ('DEV-7', at(30), 0, 1)]), 'operator@metro.demo')
        self.assertEqual(row['status'], 'variance_only')
        self.assertEqual((row['admitted_delta'], row['released_delta']), (0, 1))
        self.assertEqual(self.state['window']['admitted'], 5)
        self.assertEqual(self.state['revision']['status'], CloseoutStatus.ACCEPTED.value)
        self.assertEqual(len(self.state['batches']), 1)

    def test_ended_window_suspends_when_replay_leaves_cars_parked(self):
        self.state['window'].update(status=WindowStatus.ENDED.value)
        row = ob.submit_backfill(1, backfill_item('BF-10', [('A13', at(30), 1, 0)]),
                                 'operator@metro.demo')
        self.assertEqual(row['status'], 'suspended')
        self.assertEqual(self.state['window']['admitted'], 5)

    def test_non_open_window_cannot_backfill(self):
        self.state['window']['status'] = WindowStatus.APPROVED.value
        with self.assertRaises(HTTPException) as ctx:
            ob.submit_backfill(1, backfill_item('BF-11', [('A14', at(30), 1, 0)]),
                               'operator@metro.demo')
        self.assertEqual(ctx.exception.status_code, 409)

    def test_retry_with_changed_record_count_is_rejected(self):
        ob.submit_backfill(1, backfill_item('BF-12', [('D15', at(10), 0, 1)]),
                           'operator@metro.demo')
        with self.assertRaises(HTTPException) as ctx:
            ob.submit_backfill(1, backfill_item('BF-12', [
                ('D15', at(10), 0, 1), ('A15', at(20), 1, 0)]), 'operator@metro.demo')
        self.assertEqual(ctx.exception.status_code, 409)

    def test_retry_with_changed_counts_is_rejected(self):
        ob.submit_backfill(1, backfill_item('BF-13', [('D16', at(10), 0, 1)]),
                           'operator@metro.demo')
        with self.assertRaises(HTTPException) as ctx:
            ob.submit_backfill(1, backfill_item('BF-13', [('D16', at(10), 0, 2)]),
                               'operator@metro.demo')
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
