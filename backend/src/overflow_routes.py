from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from . import overflow_backfill, overflow_closeout, overflow_gate, overflow_queries, overflow_windows
from .auth import current_user, require
from .contracts import Role
from .db import connect
from .overflow_input import BackfillIn, CloseoutIn, DecisionIn, GateBatchIn, WindowIn
from .overflow_windows import WindowAction

router = APIRouter(prefix='/overflow', tags=['overflow'])
operator = require(Role.OPERATOR)
analyst = require(Role.ANALYST)
supervisor = require(Role.SUPERVISOR)


@router.get('/windows')
def windows(user=Depends(current_user)):
    return overflow_queries.list_windows()


@router.post('/windows', status_code=201)
def propose(item: WindowIn, user=Depends(operator)):
    return overflow_windows.propose(item, user['email'])


@router.post('/windows/{window_id}/decision/{action}')
def decide(window_id: int, action: WindowAction, item: DecisionIn, user=Depends(current_user)):
    role = Role.OPERATOR if action in (WindowAction.OPEN, WindowAction.END) else Role.SUPERVISOR
    if user['role'] != role.value:
        raise HTTPException(403, '当前岗位无权执行此窗口操作')
    return overflow_windows.change(window_id, action, user['email'], item.note)


@router.get('/windows/{window_id}/gate')
def gate(window_id: int, user=Depends(current_user)):
    return overflow_queries.gate_history(window_id)


@router.post('/windows/{window_id}/gate', status_code=201)
def record_batch(window_id: int, item: GateBatchIn, user=Depends(operator)):
    return overflow_gate.record_batch(window_id, item, user['email'])


@router.get('/windows/{window_id}/backfill')
def backfills(window_id: int, user=Depends(current_user)):
    with connect() as conn:
        return overflow_backfill.list_backfills(conn, window_id)


@router.get('/windows/{window_id}/backfill/{backfill_id}')
def backfill_detail(window_id: int, backfill_id: int, user=Depends(current_user)):
    return overflow_queries.backfill_detail(window_id, backfill_id)


@router.post('/windows/{window_id}/backfill', status_code=201)
def submit_backfill(window_id: int, item: BackfillIn, user=Depends(operator)):
    return overflow_backfill.submit_backfill(window_id, item, user['email'])


@router.get('/windows/{window_id}/closeout')
def closeout(window_id: int, user=Depends(current_user)):
    return overflow_queries.closeout_history(window_id)


@router.post('/windows/{window_id}/closeout', status_code=201)
def submit(window_id: int, item: CloseoutIn, user=Depends(analyst)):
    return overflow_closeout.submit(window_id, item, user['email'])


@router.post('/windows/{window_id}/closeout/{revision_id}/{decision}')
def review(window_id: int, revision_id: int, decision: Literal['accept', 'return'], item: DecisionIn,
           user=Depends(supervisor)):
    return overflow_closeout.review(window_id, revision_id, decision == 'accept', user['email'], item.note)
