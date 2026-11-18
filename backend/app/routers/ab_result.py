"""AB结果分析 分析看板接口：AB对比结果表、结果核对、重算与明细读取。

读取路径只暴露已提交的结果版本：列表、详情、导出和下游取数拿到的是同一批行。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, ActionPayload, EntryPayload
from app.services.ab_result import COMPARE_COLUMNS, RELEASE, AbResultService

router = APIRouter(prefix="/api/ab_result", tags=["AB结果分析"])
service = AbResultService()
LIST_FIELDS = ["报告编号", "实验编号", "分析指标", "对照组均值", "实验组均值", "置信水平", "效果方向", "生成时间"]
STATUSES = ["待分析", "计算中", "已完成", "已归档"]


def csv_cell(value: Any) -> str:
    if isinstance(value, float):
        # 差异与均值按同一精度导出，避免把 0.0651480000000001 这类浮点尾差带给下游。
        text = f"{value:.6f}"
    else:
        text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """汇总卡片全部取自结果表，和列表、详情读同一版业务数据。"""
    rows = service.list_results()
    if not rows:
        return {"release": RELEASE, "metrics": [
            {"label": "对比结果行", "value": 0, "trend": 0},
            {"label": "已核对行", "value": 0, "trend": 0},
            {"label": "结果版本", "value": 0, "trend": 0},
        ]}
    diffs = [float(row.get("差异") or 0) for row in rows]
    checked = sum(1 for row in rows if row.get("核对状态") == "已核对")
    positive = sum(1 for value in diffs if value > 0)
    negative = sum(1 for value in diffs if value < 0)
    return {"release": RELEASE, "metrics": [
        {"label": "对比结果行", "value": len(rows), "trend": len(rows)},
        {"label": "已核对行", "value": checked, "trend": checked},
        {"label": "结果版本", "value": service_version(rows), "trend": positive - negative},
    ]}


def service_version(rows: list[dict[str, Any]]) -> int:
    versions = [int(row.get("结果版本") or 0) for row in rows]
    return max(versions, default=0)


@router.get("")
def list_results() -> dict[str, Any]:
    """AB对比结果表：每行差异=实验组均值-对照组均值，与详情读同一版本。"""
    rows = service.list_results()
    return {
        "items": rows,
        "total": len(rows),
        "release": RELEASE,
        "version": service_version(rows),
        "columns": COMPARE_COLUMNS,
    }


@router.get("/compare")
def list_compare() -> dict[str, Any]:
    """下游取数入口：返回已提交的 AB对比结果表，和页面列表读同一个版本。"""
    return list_results()


@router.post("/compare")
def rebuild_compare() -> dict[str, Any]:
    """接收方触发的重建入口：整表重新生成并核对后一次性替换。"""
    result = service.recalculate()
    return {**result, "release": RELEASE}


@router.get("/compare/{entry_id}")
def get_compare(entry_id: int) -> dict[str, Any]:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"AB对比结果 {entry_id} 不存在")
    return {**entry, "release": RELEASE}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    """运行分析：先按成员快照重算两侧均值，再由均值生成差异。"""
    results = service.run_analysis(payload.values)
    return {"items": results, "release": RELEASE}


@router.post("/recalculate")
def recalculate(payload: EntryPayload | None = None) -> dict[str, Any]:
    """重算 AB对比结果表；失败会重试并回滚到上一完整版本。"""
    result = service.recalculate()
    return {**result, "release": RELEASE}


@router.post("/verify")
def verify() -> dict[str, Any]:
    """核对结果表：差异必须等于同一快照下实验组均值减对照组均值。"""
    result = service.recalculate()
    return {
        "ok": result.get("ok", False),
        "version": result.get("version"),
        "checked": len(result.get("items") or []),
        "message": result.get("message"),
        "release": RELEASE,
    }


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id", *COMPARE_COLUMNS, "结果版本"]
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=ab_result.csv"})


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """详情：报告字段与 AB对比结果来自同一次读取，不会错位。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析报告 {entry_id} 不存在")
    return {**entry, "release": RELEASE}
