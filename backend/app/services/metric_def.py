"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.ab_result import AbResultService
from app.store import store

MODULE = "metric_def"
RELEASE = "V028"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    # ------------------------------------------------------------ V028 跨模块核对
    def verify_ab_compare(self) -> dict[str, Any]:
        """读取AB对比结果表中的指标版本，核对差异等于同一口径下的两侧均值差。

        核对只读已提交的结果表：口径漂移、残留行或差异对不上都如实报出，
        不在核对过程中顺手改写被核对的那张表。结果表尚未生成时先按结果表
        先生成环节补齐一次，再基于补齐后的版本核对。
        """
        service = AbResultService()
        rows = list(store.rows("ab_compare_results"))
        rebuilt: dict[str, Any] = {}
        if not rows:
            rebuilt = service.recalculate()
            rows = list(rebuilt.get("items") or store.rows("ab_compare_results"))
        problems = self._collect_problems(rows)
        ok = bool(rows) and not problems
        return {
            "ok": ok,
            "release": RELEASE,
            "version": rebuilt.get("version"),
            "checked": len(rows),
            "table_ok": bool(rebuilt.get("ok", True)),
            "message": "AB对比结果表核对通过，可转交接收方" if ok
            else "AB对比结果表与指标口径不一致，暂不可转交",
            "problems": problems,
        }

    def _collect_problems(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        problems: list[dict[str, Any]] = []
        for row in rows:
            metric = self._metric_for_row(row)
            if metric is None:
                problems.append({
                    "报告编号": row.get("报告编号"),
                    "问题": f"指标定义中找不到「{row.get('分析指标')}」，无法确认口径版本",
                })
                continue
            expected = str(metric.get("口径版本") or "v1")
            if str(row.get("指标版本")) != expected:
                problems.append({
                    "报告编号": row.get("报告编号"),
                    "问题": f"结果行指标版本 {row.get('指标版本')} 与指标定义 {expected} 不一致",
                })
                continue
            try:
                control = float(row.get("对照组均值"))
                treated = float(row.get("实验组均值"))
                diff = float(row.get("差异"))
            except (TypeError, ValueError):
                problems.append({"报告编号": row.get("报告编号"), "问题": "均值或差异不是数值"})
                continue
            if abs(diff - (treated - control)) > 5e-7:
                problems.append({
                    "报告编号": row.get("报告编号"),
                    "问题": f"差异 {diff} 不等于实验组均值减对照组均值 {treated - control}",
                })
        return problems

    def _metric_for_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        name = str(row.get("分析指标") or "").strip()
        for metric in store.rows(MODULE):
            if name and name in {str(metric.get("指标名称") or ""), str(metric.get("指标编号") or "")}:
                return metric
        return None
