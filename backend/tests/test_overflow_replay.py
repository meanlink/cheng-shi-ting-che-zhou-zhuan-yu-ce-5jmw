import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.overflow_replay import ReplaySuspended, replay  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)
WINDOW = {
    'starts_at': T0,
    'ends_at': T0 + timedelta(hours=2),
    'opened_at': T0,
    'bay_limit': 5,
}


def at(minute):
    return T0 + timedelta(minutes=minute)


def online(id_, receipt, minute, admitted=0, released=0):
    return {'id': id_, 'receipt': receipt, 'occurred_at': at(minute),
            'admitted': admitted, 'released': released}


def backfill(receipt, minute, admitted=0, released=0):
    return {'receipt': receipt, 'occurred_at': at(minute),
            'admitted': admitted, 'released': released}


class ReplayTests(unittest.TestCase):
    def test_late_exit_frees_bay_for_later_admission(self):
        # 在线时已登记满 5 辆；断网期间先有离场，后有进场，按实际过闸时间插回。
        online_batches = [online(1, 'G-1', 5, admitted=5)]
        records = [backfill('DEV-1', 10, released=2), backfill('DEV-2', 20, admitted=2)]
        result = replay(WINDOW, online_batches, records, [])
        self.assertEqual(result.admitted, 7)
        self.assertEqual(result.released, 2)
        self.assertEqual(result.peak_parked, 5)
        self.assertEqual(result.parked_after[('backfill', 1)], 5)

    def test_over_quota_suspends_whole_batch(self):
        online_batches = [online(1, 'G-1', 5, admitted=5)]
        records = [backfill('DEV-1', 10, admitted=1)]
        with self.assertRaises(ReplaySuspended) as ctx:
            replay(WINDOW, online_batches, records, [])
        self.assertEqual(ctx.exception.code, 'over_quota')

    def test_exit_more_than_parked_suspends(self):
        online_batches = [online(1, 'G-1', 5, admitted=2)]
        records = [backfill('DEV-1', 10, released=3)]
        with self.assertRaises(ReplaySuspended) as ctx:
            replay(WINDOW, online_batches, records, [])
        self.assertEqual(ctx.exception.code, 'negative_balance')

    def test_admission_during_pause_is_stop_collection(self):
        online_batches = [online(1, 'G-1', 5, admitted=1)]
        records = [backfill('DEV-1', 30, admitted=1)]
        pauses = [(at(20), at(40))]
        with self.assertRaises(ReplaySuspended) as ctx:
            replay(WINDOW, online_batches, records, pauses)
        self.assertEqual(ctx.exception.code, 'stop_collection')

    def test_exit_during_pause_is_allowed(self):
        online_batches = [online(1, 'G-1', 5, admitted=3)]
        records = [backfill('DEV-1', 30, released=2)]
        result = replay(WINDOW, online_batches, records, [(at(20), at(40))])
        self.assertEqual(result.released, 2)
        self.assertEqual(result.parked_after[('backfill', 0)], 1)

    def test_admission_after_window_end_suspends(self):
        online_batches = []
        records = [backfill('DEV-1', 120, admitted=1)]
        with self.assertRaises(ReplaySuspended) as ctx:
            replay(WINDOW, online_batches, records, [])
        self.assertEqual(ctx.exception.code, 'stop_collection')

    def test_event_outside_window_rejected(self):
        with self.assertRaises(ReplaySuspended) as ctx:
            replay(WINDOW, [], [backfill('DEV-1', 200, admitted=1)], [])
        self.assertEqual(ctx.exception.code, 'outside_window')

    def test_mixed_batch_clears_exit_before_admit_same_timestamp(self):
        online_batches = [online(1, 'G-1', 5, admitted=5)]
        records = [backfill('DEV-1', 10, admitted=3, released=3)]
        result = replay(WINDOW, online_batches, records, [])
        self.assertEqual(result.admitted, 8)
        self.assertEqual(result.released, 3)
        self.assertEqual(result.parked_after[('backfill', 0)], 5)

    def test_peak_recomputed_including_backfilled_history(self):
        # 在线流水只看到 2 辆的小高峰；补传记录显示更早还有一轮 5 辆的满场高峰。
        online_batches = [online(1, 'G-1', 30, admitted=2), online(2, 'G-2', 40, released=2)]
        records = [backfill('DEV-1', 10, admitted=5), backfill('DEV-2', 20, released=5)]
        result = replay(WINDOW, online_batches, records, [])
        self.assertEqual(result.peak_parked, 5)
        self.assertEqual(result.admitted, 7)
        self.assertEqual(result.released, 7)
        self.assertEqual(result.parked_after[2], 0)


if __name__ == '__main__':
    unittest.main()
