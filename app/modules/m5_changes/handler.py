"""M5 路由：变更序列、抵扣行、超额、重算、用量上传。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from ... import web
from ...errors import BadRequest
from . import service

router = APIRouter(prefix="/api/changes", tags=["M5 变更抵扣"])


@router.get("")
def changes(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    return {
        "run_id": run["run_id"],
        "summary": service.summary(store, run["run_id"]),
        "changes": service.change_rows(store, run["run_id"]),
        "overages": store.list_overages(run["run_id"]),
    }


@router.post("")
def rebuild(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    settings = request.app.state.settings
    with store.rebuild_transaction(run["run_id"]):
        built = service.rebuild(store, run["run_id"], settings)
        from ..m4_proration import service as proration_service
        from ..m6_invoices import service as invoice_service

        amounts = proration_service.recompute_amounts(store, run["run_id"], settings)
        invoices = invoice_service.rebuild(store, run["run_id"], settings)
    return {
        "run_id": run["run_id"],
        "changes": built,
        "proration": amounts,
        "invoices": invoices["summary"],
        "reconciliations": invoices["ledgers"],
    }


@router.post("/usage/import")
def import_usage(request: Request,
                 payload: dict[str, Any] | None = Body(default=None)) -> dict:
    """上传整月用量。同一份用量文件（sha256 相同）重试不重复累计。"""
    run = web.run_or_404(request)
    body = payload or {}
    name = body.get("usage") or body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BadRequest("文件名不能为空", code="E_BAD_FILE_NAME")
    store = request.app.state.store
    settings = request.app.state.settings
    return service.import_usage(store, run["run_id"], settings, name.strip())
