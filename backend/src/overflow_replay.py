"""断网补传的时间序回放引擎。

晚到记录按实际过闸时间插回在线流水：先把所有流水拆成“离场核销、进场占车”
两个动作并按过闸时间重放。当时离场腾出的车位因此可以算给后面的进场。
任一动作违反规则即整批挂起，由调用方保证车数不变（不写在线流水）。
"""
from dataclasses import dataclass
from datetime import datetime


class ReplaySuspended(Exception):
    """整批挂起：code 为机器可读原因，message 为闸机页面展示说明。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GateEvent:
    key: object                 # 在线批次 id 或补传明细键
    receipt: str
    occurred_at: datetime
    admitted: int
    released: int
    backfill: bool
    seq: int                    # 同窗口内的稳定次序（在线 id / 补传数组下标）


@dataclass
class ReplayResult:
    admitted: int
    released: int
    peak_parked: int
    parked_after: dict           # key -> 该批次核销后的在场车辆


def _within_pauses(occurred_at, pauses):
    return any(start <= occurred_at < end for start, end in pauses)


def _is_stop_collection(window, occurred_at, pauses):
    """进场时刻是否落在停收时段：未开放、暂停中或计划收车时段已结束。"""
    if occurred_at < window['opened_at'] or occurred_at >= window['ends_at']:
        return True
    return _within_pauses(occurred_at, pauses)


def build_steps(window, online_batches, backfill_records, pauses):
    """合并在线流水与补传明细，返回按过闸时间排序的原子动作。

    同一时刻先核销离场再登记进场（与在线混合批次规则一致）；
    同类型动作里在线流水优先于补传，再按稳定次序排序，保证结果可复现。
    """
    events = []
    for batch in online_batches:
        events.append(GateEvent(batch['id'], batch['receipt'], batch['occurred_at'],
                                batch['admitted'], batch['released'], False, batch['id']))
    for index, record in enumerate(backfill_records):
        events.append(GateEvent(('backfill', index), record['receipt'], record['occurred_at'],
                                record['admitted'], record['released'], True, index))
    steps = []
    for event in events:
        if event.released:
            steps.append((event.occurred_at, 0, event.backfill, event.seq, event, 'release'))
        if event.admitted:
            steps.append((event.occurred_at, 1, event.backfill, event.seq, event, 'admit'))
    steps.sort(key=lambda step: (step[0], step[1], step[2], step[3]))

    for event in events:
        if not window['starts_at'] <= event.occurred_at <= window['ends_at']:
            raise ReplaySuspended(
                'outside_window',
                f"流水 {event.receipt} 的过闸时间 {event.occurred_at:%Y-%m-%d %H:%M} 不在窗口收车时段内")
        if event.admitted and _is_stop_collection(window, event.occurred_at, pauses):
            raise ReplaySuspended(
                'stop_collection',
                f"流水 {event.receipt} 的进场发生在停收时段（{event.occurred_at:%H:%M} UTC），整批挂起")
    return steps


def replay(window, online_batches, backfill_records, pauses):
    """重放全部流水，返回目标累计与每批在场数；违规时抛出 ReplaySuspended。"""
    steps = build_steps(window, online_batches, backfill_records, pauses)
    bay_limit = window['bay_limit']
    parked = admitted = released = peak = 0
    parked_after = {}

    for _, kind, _, _, event, _label in steps:
        if kind == 0:  # 离场：任何时点离场多于在场即整批挂起
            if event.released > parked:
                raise ReplaySuspended(
                    'negative_balance',
                    f"流水 {event.receipt} 离场 {event.released} 辆，超过当时在场 {parked} 辆，整批挂起")
            parked -= event.released
            released += event.released
        else:          # 进场：当时腾出的车位已由先前离场动作释放
            available = bay_limit - parked
            if event.admitted > available:
                raise ReplaySuspended(
                    'over_quota',
                    f"流水 {event.receipt} 进场 {event.admitted} 辆，超过当时余位 {available} 辆（配额 {bay_limit} 位），整批挂起")
            parked += event.admitted
            admitted += event.admitted
        peak = max(peak, parked)
        # 混合批次的两个动作完成后才记录该批次在场数
        parked_after[event.key] = parked

    return ReplayResult(admitted, released, peak, parked_after)
