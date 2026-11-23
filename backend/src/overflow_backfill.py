from fastapi import HTTPException

from .db import connect
from .overflow_queries import lock_window
from .overflow_replay import ReplaySuspended, replay
from .overflow_rules import CloseoutStatus, WindowStatus, tally_variance
from .workflow import audit

REVIEWABLE = (CloseoutStatus.READY, CloseoutStatus.FLAGGED)


def _pause_ranges(conn, window_id):
    rows = conn.execute('''SELECT paused_at, coalesce(resumed_at, now()) AS resumed_at
        FROM overflow_window_pause WHERE window_id=%s ORDER BY paused_at''', (window_id,)).fetchall()
    return [(row['paused_at'], row['resumed_at']) for row in rows]


def _load_online(conn, window_id):
    return conn.execute('''SELECT id,receipt,admitted,released,occurred_at FROM overflow_gate_batch
        WHERE window_id=%s ORDER BY occurred_at, id''', (window_id,)).fetchall()


def _latest_revision(conn, window_id):
    return conn.execute('''SELECT * FROM overflow_closeout WHERE window_id=%s
        ORDER BY revision DESC LIMIT 1''', (window_id,)).fetchone()


def _auto_return_closeout(conn, actor, batch_reference, totals, previous):
    """补传改变累计数时，待主管复核的盘点退回重盘；已结案则只留差异。"""
    if not previous or previous['status'] not in REVIEWABLE:
        return
    item = type('Ledger', (), {
        'counted_admitted': previous['counted_admitted'],
        'counted_released': previous['counted_released'],
    })
    admission_delta, release_delta, _ = tally_variance(totals, item)
    note = f'断网补传 {batch_reference} 改变闸机累计数，盘点自动退回重盘'
    conn.execute('''UPDATE overflow_closeout SET status=%s, admission_delta=%s, release_delta=%s,
        review_note=%s WHERE id=%s''',
                 (CloseoutStatus.RETURNED.value, admission_delta, release_delta, note, previous['id']))
    audit(conn, actor, 'overflow_closeout', previous['id'], 'auto_returned', note)


def _save_batch(conn, window_id, reference, status, reason, records, totals, actor, gate_ids):
    row = conn.execute('''INSERT INTO overflow_backfill_batch
        (window_id,reference,status,reason,admitted_delta,released_delta,record_count,submitted_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                       (window_id, reference, status, reason,
                        totals['admitted_delta'] if totals else 0,
                        totals['released_delta'] if totals else 0, len(records), actor)).fetchone()
    for index, record in enumerate(records):
        # 新插回流水取重放后新建的在线行；重发流水沿用其原在线行或原补传记录的关联。
        gate_batch_id = gate_ids.get(('backfill', index)) or gate_ids.get(record['receipt'])
        conn.execute('''INSERT INTO overflow_backfill_record
            (backfill_id,window_id,receipt,occurred_at,admitted,released,gate_batch_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                     (row['id'], window_id, record['receipt'], record['occurred_at'],
                      record['admitted'], record['released'], gate_batch_id))
    return row


def submit_backfill(window_id, item, actor):
    with connect() as conn:
        window = lock_window(conn, window_id)
        if window['status'] not in (WindowStatus.OPEN, WindowStatus.PAUSED,
                                    WindowStatus.ENDED, WindowStatus.RECONCILED):
            raise HTTPException(409, '仅已开放后的窗口可补传断网流水')

        existing_batch = conn.execute('''SELECT * FROM overflow_backfill_batch
            WHERE window_id=%s AND reference=%s''', (window_id, item.reference)).fetchone()
        if existing_batch:
            # 重试不重复记账：同补传单号只允许原样重发（条数、流水号与进出数一致）。
            previous_records = conn.execute('''SELECT receipt, admitted, released
                FROM overflow_backfill_record WHERE backfill_id=%s ORDER BY id''',
                                            (existing_batch['id'],)).fetchall()
            incoming = [(r.receipt, r.admitted, r.released) for r in item.records]
            stored = [(r['receipt'], r['admitted'], r['released']) for r in previous_records]
            if incoming != stored:
                raise HTTPException(409, '该补传单号已提交，流水内容与原批次不一致，请更换单号')
            return existing_batch

        records = [record.model_dump() for record in item.records]
        receipts = [record['receipt'] for record in records]
        if len(set(receipts)) != len(receipts):
            raise HTTPException(422, '同一补传批次内设备流水号重复')
        known = conn.execute('''SELECT id, receipt, admitted, released FROM overflow_gate_batch
            WHERE window_id=%s AND receipt = ANY(%s)''', (window_id, receipts)).fetchall()
        # 已入账补传批次中出现过的设备流水号同样视为已登记，重试只允许原样重发；
        # 挂起批次从未写入在线流水，其流水号仍可在后续补传中重新提交。
        prior_backfill = conn.execute('''SELECT DISTINCT ON (r.receipt) r.receipt, r.admitted,
            r.released, r.gate_batch_id
            FROM overflow_backfill_record r JOIN overflow_backfill_batch b ON b.id = r.backfill_id
            WHERE r.window_id=%s AND r.receipt = ANY(%s) AND b.status IN ('applied','variance_only')
            ORDER BY r.receipt, r.id DESC''', (window_id, receipts)).fetchall()
        known_map = {row['receipt']: row for row in [*known, *prior_backfill]}
        # 重发流水关联到既有在线行，使补传明细的“已入账”状态可追溯。
        receipt_gate_ids = {row['receipt']: row['id'] for row in known}
        receipt_gate_ids.update({row['receipt']: row['gate_batch_id']
                                 for row in prior_backfill if row['gate_batch_id']})
        fresh = []
        for record in records:
            prior = known_map.get(record['receipt'])
            if prior is None:
                fresh.append(record)
            elif (prior['admitted'], prior['released']) != (record['admitted'], record['released']):
                raise HTTPException(409, f"设备流水号 {record['receipt']} 已登记，进出数与原流水不一致")
        if not fresh:
            # 全部为已入账流水的重发：幂等返回，不产生任何记账变化。
            return _save_batch(conn, window_id, item.reference, 'applied', None, records,
                               {'admitted_delta': 0, 'released_delta': 0}, actor, receipt_gate_ids)

        try:
            # 重放只纳入尚未入账的流水；已知流水已在在线序列中，重复参与会双重计数。
            result = replay(window, _load_online(conn, window_id), fresh, _pause_ranges(conn, window_id))
        except ReplaySuspended as suspended:
            # 整批挂起：只落补传留痕，在线流水与窗口车数保持不变。
            row = _save_batch(conn, window_id, item.reference, 'suspended', suspended.message,
                              records, None, actor, receipt_gate_ids)
            audit(conn, actor, 'overflow_backfill_batch', row['id'], 'suspended', suspended.code)
            return row

        reconciled = window['status'] == WindowStatus.RECONCILED
        gate_ids = {}
        if not reconciled:
            if result.admitted != result.released and window['status'] == WindowStatus.ENDED:
                row = _save_batch(conn, window_id, item.reference, 'suspended',
                                  '窗口已清场结案，补传后仍有分流车辆在场，须人工核对，整批挂起',
                                  records, None, actor, receipt_gate_ids)
                audit(conn, actor, 'overflow_backfill_batch', row['id'], 'suspended', 'reopen_balance')
                return row
            # 补传确实改变累计数（存在新插回流水）时，先取出待复核盘点以便自动退回。
            previous_revision = _latest_revision(conn, window_id)
            for index, record in enumerate(fresh):
                gate_row = conn.execute('''INSERT INTO overflow_gate_batch
                    (window_id,receipt,admitted,released,parked_after,recorded_by,occurred_at,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'backfill') RETURNING id''',
                                        (window_id, record['receipt'], record['admitted'], record['released'],
                                         result.parked_after[('backfill', index)], actor,
                                         record['occurred_at'])).fetchone()
                gate_ids[('backfill', index)] = gate_row['id']
                receipt_gate_ids[record['receipt']] = gate_row['id']
            # 晚到记录插回后，在线流水的“登记后在场”全部按过闸时间重算。
            for batch in _load_online(conn, window_id):
                if batch['id'] in result.parked_after:
                    conn.execute('UPDATE overflow_gate_batch SET parked_after=%s WHERE id=%s',
                                 (result.parked_after[batch['id']], batch['id']))
            conn.execute('''UPDATE overflow_window SET admitted=%s, released=%s, peak_parked=%s WHERE id=%s''',
                         (result.admitted, result.released, result.peak_parked, window_id))
            totals = {'admitted': result.admitted, 'released': result.released,
                      'admitted_delta': result.admitted - window['admitted'],
                      'released_delta': result.released - window['released']}
            _auto_return_closeout(conn, actor, item.reference, totals, previous_revision)
            row = _save_batch(conn, window_id, item.reference, 'applied', None, records, totals,
                              actor, {**gate_ids, **receipt_gate_ids})
            audit(conn, actor, 'overflow_backfill_batch', row['id'], 'applied',
                  f'进 {totals["admitted_delta"]} · 出 {totals["released_delta"]}')
            return row

        # 已结案窗口：结案先办完则保留原盘点，补传只留差异。
        totals = {'admitted_delta': result.admitted - window['admitted'],
                  'released_delta': result.released - window['released']}
        row = _save_batch(conn, window_id, item.reference, 'variance_only',
                          '窗口已结案，补传不改在线累计，仅保留差异供追溯', records, totals, actor,
                          receipt_gate_ids)
        audit(conn, actor, 'overflow_backfill_batch', row['id'], 'variance_only',
              f'进差 {totals["admitted_delta"]} · 出差 {totals["released_delta"]}')
        return row


def list_backfills(conn, window_id):
    return conn.execute('''SELECT b.*,
        (SELECT count(*) FROM overflow_backfill_record r WHERE r.backfill_id=b.id) AS record_total
        FROM overflow_backfill_batch b WHERE b.window_id=%s
        ORDER BY b.submitted_at DESC, b.id DESC''', (window_id,)).fetchall()
