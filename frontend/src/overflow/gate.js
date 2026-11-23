import {badge, empty} from '../components.js';
import {esc, fmt} from '../utils.js';
import {utcClock, backfillStatus, batchSource} from './labels.js';
import {pageHeading, summary, windowSelector} from './shared.js';

function gateForm(window) {
    const exitOnly = window.status === 'paused' || Date.now() >= Date.parse(window.ends_at);
    return `<form class="stack overflow-gate-form" data-overflow-form="gate" data-window-id="${window.id}">
        <label>闸机单号<input name="receipt" minlength="3" maxlength="48" required placeholder="GATE-0701"></label>
        <label>进场车辆<input name="admitted" type="number" min="0" max="10000" value="0" required ${exitOnly ? 'readonly' : ''}></label>
        <label>离场车辆<input name="released" type="number" min="0" max="10000" value="0" required></label>
        <button class="button">登记流水</button>
        <div class="form-status" role="status"></div>
    </form>`;
}

function backfillRows() {
    return `<div class="backfill-rows" data-backfill-rows>
        ${backfillRow()}
    </div>
    <button type="button" class="button secondary" data-add-backfill-row>增加一条设备流水</button>`;
}

function backfillRow() {
    return `<div class="backfill-row">
        <label>设备流水号<input name="receipt" minlength="3" maxlength="48" required placeholder="DEV-1001"></label>
        <label>实际过闸时间 · UTC<input name="occurred_at" type="datetime-local" step="1" required></label>
        <label>进场<input name="admitted" type="number" min="0" max="10000" value="0" required></label>
        <label>离场<input name="released" type="number" min="0" max="10000" value="0" required></label>
        <button type="button" class="icon-button" data-remove-row title="删除该行">✕</button>
    </div>`;
}

function backfillForm(window) {
    return `<section class="overflow-panel"><h2>断网恢复 · 按设备流水号补传</h2>
        <p class="muted">晚到记录按实际过闸时间插回在线流水，先核销离场再占用车位；若落在停收时段、超配额或离场多于在场，整批挂起且车数不变。</p>
        <form class="stack" data-overflow-form="backfill" data-window-id="${window.id}">
            <label>补传批次单号<input name="reference" minlength="3" maxlength="48" required placeholder="BF-20261123-01"></label>
            ${backfillRows()}
            <div class="form-status" role="status"></div>
            <button class="button">提交补传</button>
        </form></section>`;
}

function backfillPanel(backfills) {
    if (!backfills?.length) return '';
    const cards = backfills.map(row => {
        const [label, tone] = backfillStatus[row.status];
        return `<article class="overflow-card backfill-card" data-backfill-card="${row.id}">
            <div class="overflow-card-top"><span class="code">${esc(row.reference)}</span>${badge(label, tone)}</div>
            <dl><div><dt>流水条数</dt><dd>${fmt(row.record_total)} 条</dd></div>
                <div><dt>进场差额</dt><dd>${row.admitted_delta > 0 ? '+' : ''}${fmt(row.admitted_delta)} 辆</dd></div>
                <div><dt>离场差额</dt><dd>${row.released_delta > 0 ? '+' : ''}${fmt(row.released_delta)} 辆</dd></div>
                <div><dt>提交时间 · UTC</dt><dd>${utcClock(row.submitted_at)}</dd></div></dl>
            ${row.reason ? `<p class="overflow-warning">${esc(row.reason)}</p>` : ''}
            <div class="backfill-detail" data-backfill-detail hidden></div>
            <button type="button" class="button secondary" data-backfill-toggle data-window-id="${row.window_id}" data-backfill-id="${row.id}">查看挂起原因 / 明细</button>
        </article>`;
    }).join('');
    return `<section class="overflow-panel"><div class="overflow-section-title"><h2>补传批次</h2>
        <span>重试同单号不重复记账</span></div>${cards}</section>`;
}

function backfillDetailTable(records) {
    if (!records.length) return empty('该批次没有明细。');
    return `<div class="overflow-table"><table><thead><tr>
        <th>设备流水号</th><th>实际过闸 · UTC</th><th>进场</th><th>离场</th><th>在线流水</th>
    </tr></thead><tbody>${records.map(row => `<tr><td class="code">${esc(row.receipt)}</td>
        <td>${utcClock(row.occurred_at)}</td><td>${fmt(row.admitted)}</td><td>${fmt(row.released)}</td>
        <td>${row.gate_batch_id ? '已入账' : '未入账'}</td></tr>`).join('')}</tbody></table></div>`;
}

function batchTable(batches) {
    if (!batches.length) return empty('当前窗口还没有闸机流水。');
    return `<div class="overflow-table"><table><thead><tr>
        <th>闸机单号</th><th>来源</th><th>过闸时间 · UTC</th><th>登记时间 · UTC</th><th>进场</th><th>离场</th><th>登记后场内</th>
    </tr></thead><tbody>${batches.map(row => `<tr><td class="code">${esc(row.receipt)}</td>
        <td>${esc(batchSource[row.source] || row.source)}</td>
        <td>${utcClock(row.occurred_at)}</td><td>${utcClock(row.recorded_at)}</td>
        <td>${fmt(row.admitted)}</td><td>${fmt(row.released)}</td>
        <td>${fmt(row.parked_after)}</td></tr>`).join('')}</tbody></table></div>`;
}

export function gatePage(windows, data, role) {
    const heading = pageHeading('溢流闸机', '核对接收场的实时收车与离场流水，断网恢复后按设备流水号补传。');
    if (!data) return heading + empty('开放入口后，可在此登记闸机流水。');
    const {window, batches, backfills} = data;
    const paused = window.status === 'paused';
    const expired = window.status === 'open' && Date.now() >= Date.parse(window.ends_at);
    const canOperate = role === 'operator' && ['open','paused','ended','reconciled'].includes(window.status);
    return `${heading}${windowSelector(windows, window.id)}${summary(window)}
        ${paused ? `<p class="overflow-warning">暂停收车 · ${esc(window.pause_reason)}。仍可登记离场。</p>` : ''}
        ${expired ? '<p class="overflow-warning">收车时段已结束，仍可登记离场。</p>' : ''}
        ${role === 'operator' && ['open','paused'].includes(window.status) ? `<section class="overflow-panel">
            <h2>登记闸机批次</h2><p class="muted">同时有进出记录时，先核销离场再计算可用车位。</p>${gateForm(window)}</section>` : ''}
        ${canOperate ? backfillForm(window) : ''}
        ${backfillPanel(backfills)}
        <section class="overflow-panel"><div class="overflow-section-title"><h2>最近流水</h2><a href="#/overflow-dispatch">查看开放计划</a></div>
            ${batchTable(batches)}</section>`;
}

export function bindBackfillRows(root) {
    const rows = root.querySelector('[data-backfill-rows]');
    if (!rows) return;
    rows.addEventListener('click', event => {
        if (event.target.matches('[data-remove-row]') && rows.children.length > 1) {
            event.target.closest('.backfill-row').remove();
        }
    });
    root.querySelector('[data-add-backfill-row]')?.addEventListener('click', () => {
        rows.insertAdjacentHTML('beforeend', backfillRow());
    });
}

export async function bindBackfillDetail(root, api) {
    for (const button of root.querySelectorAll('[data-backfill-toggle]')) {
        button.addEventListener('click', async () => {
            const card = button.closest('[data-backfill-card]');
            const detail = card.querySelector('[data-backfill-detail]');
            if (!detail.hidden) { detail.hidden = true; return; }
            const {windowId, backfillId} = button.dataset;
            try {
                const data = await api(`/overflow/windows/${windowId}/backfill/${backfillId}`);
                detail.innerHTML = backfillDetailTable(data.records);
                detail.hidden = false;
            } catch (error) {
                detail.innerHTML = `<p class="overflow-warning">${esc(error.message)}</p>`;
                detail.hidden = false;
            }
        });
    }
}
