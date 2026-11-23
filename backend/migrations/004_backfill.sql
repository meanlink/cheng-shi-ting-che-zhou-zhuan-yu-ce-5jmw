-- 断网恢复后的闸机补传：按设备流水号把晚到记录按实际过闸时间插回在线流水。

-- 收车暂停区间，用于回放时判断进场是否落在停收时段。
CREATE TABLE IF NOT EXISTS overflow_window_pause (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    window_id bigint NOT NULL REFERENCES overflow_window(id),
    paused_at timestamptz NOT NULL,
    resumed_at timestamptz,
    reason text NOT NULL,
    CHECK (resumed_at IS NULL OR resumed_at > paused_at)
);
CREATE INDEX IF NOT EXISTS overflow_pause_window ON overflow_window_pause (window_id);

-- 在线闸机流水补上实际过闸时间与来源（在线登记 / 断网补传）。
ALTER TABLE overflow_gate_batch ADD COLUMN IF NOT EXISTS occurred_at timestamptz;
ALTER TABLE overflow_gate_batch ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'online'
    CHECK (source IN ('online','backfill'));
UPDATE overflow_gate_batch SET occurred_at = recorded_at WHERE occurred_at IS NULL;
ALTER TABLE overflow_gate_batch ALTER COLUMN occurred_at SET NOT NULL;

-- 断网补传整批单（按设备流水号组织）。挂起时不写入在线流水、车数不变。
CREATE TABLE IF NOT EXISTS overflow_backfill_batch (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    window_id bigint NOT NULL REFERENCES overflow_window(id),
    reference text NOT NULL,
    status text NOT NULL CHECK (status IN ('applied','suspended','variance_only')),
    reason text,
    admitted_delta integer NOT NULL DEFAULT 0,
    released_delta integer NOT NULL DEFAULT 0,
    record_count integer NOT NULL CHECK (record_count > 0),
    submitted_by text NOT NULL REFERENCES staff(email),
    submitted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (window_id, reference)
);

-- 补传明细：即使整批挂起也逐条留痕，挂起原因可在闸机页面查询。
-- 同一设备流水号允许在不同补传单号中重发（重试不重复记账），故只建普通索引。
CREATE TABLE IF NOT EXISTS overflow_backfill_record (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backfill_id bigint NOT NULL REFERENCES overflow_backfill_batch(id) ON DELETE CASCADE,
    window_id bigint NOT NULL REFERENCES overflow_window(id),
    receipt text NOT NULL,
    occurred_at timestamptz NOT NULL,
    admitted integer NOT NULL CHECK (admitted >= 0),
    released integer NOT NULL CHECK (released >= 0),
    gate_batch_id bigint REFERENCES overflow_gate_batch(id),
    CHECK (admitted > 0 OR released > 0)
);
CREATE INDEX IF NOT EXISTS overflow_backfill_window ON overflow_backfill_record (window_id);
CREATE INDEX IF NOT EXISTS overflow_backfill_receipt ON overflow_backfill_record (window_id, receipt);

-- 结案窗口的补传只留差异；待复核盘点可能被补传退回，退回版允许携带新差额。
-- 003 中的两条内联 CHECK 由数据库自动命名，这里按约束定义精确定位后替换为具名约束。
DO $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid = 'overflow_closeout'::regclass AND contype = 'c'
    LOOP
        IF position('admission_delta' IN target.definition) > 0
           OR target.definition LIKE '%reviewed_by IS NOT NULL%' THEN
            EXECUTE format('ALTER TABLE overflow_closeout DROP CONSTRAINT %I', target.conname);
        END IF;
    END LOOP;
END $$;

ALTER TABLE overflow_closeout
    ADD CONSTRAINT overflow_closeout_status_delta_chk
    CHECK ((status = 'flagged') = (admission_delta <> 0 OR release_delta <> 0) OR status = 'returned');
ALTER TABLE overflow_closeout
    ADD CONSTRAINT overflow_closeout_status_review_chk
    CHECK (status <> 'accepted' OR reviewed_by IS NOT NULL);
