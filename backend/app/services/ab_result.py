"""AB结果分析 分析看板服务：把分析报告与分群成员明细汇总成可核对的AB对比结果表。

规则（V028）：
- 每一行「差异」只由同一批成员的「实验组均值 - 对照组均值」得出，不再单独随机。
- 先按成员快照重算两侧均值，再由两侧均值生成差异；不会先写差异再补均值。
- 生成、核对、写入在同一个事务里完成；任一步失败整体回滚到上一完整版本。
- 重算中断会重试；重试仍失败时保留上一完整版本，绝不用部分成员覆盖全部结果。
- 读取（列表、详情、下游）只读已提交的同一个结果版本，刷新不会漂移。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from app.services.sample_group import SampleGroupService
from app.store import store

MODULE = "ab_result"
COMPARE_MODULE = "ab_compare_results"
BACKUP_MODULE = "ab_compare_results_backup"
RELEASE = "V028"
REQUIRED_FIELDS = ["报告编号", "实验编号", "分析指标"]
STATUS_ORDER = ["待分析", "计算中", "已完成", "已归档"]
ACTION_RULES = {"开始计算": "计算中", "确认完成": "已完成", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []
COMPARE_COLUMNS = [
    "报告编号", "实验编号", "分析指标", "指标版本",
    "对照组均值", "实验组均值", "差异", "核对状态",
]
SCALE = 1_000_000
MAX_RETRIES = 3

# 结果行的核心字段；生成时间/结果版本不参与「内容是否变化」的判断。
_SIGNATURE_FIELDS = [
    "报告编号", "实验编号", "分析指标", "指标版本",
    "对照组均值", "实验组均值", "差异",
    "对照组成员版本", "实验组成员版本", "对照组样本量", "实验组样本量",
    "成员快照",
]


class CompareTableError(RuntimeError):
    """AB对比结果表在生成或核对阶段发现不一致。"""


class AbResultService:
    def __init__(self) -> None:
        self.sample_groups = SampleGroupService()

    # ------------------------------------------------------------------ 读取
    def list_results(self) -> list[dict[str, Any]]:
        """返回 AB对比结果表；表内数据异常（残留/错位/版本不一致）时先整体重建。"""
        reports = self._reports()
        self._ensure_compare_table(reports)
        return self._rows_with_report_status(reports)

    list_compare_results = list_results

    @staticmethod
    def _rows_with_report_status(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给已提交的结果行补上分析报告当前状态；只读，不改写结果表。"""
        status_by_code = {
            str(report.get("报告编号")): {
                "status": report.get("status"),
                "pending": report.get("pending"),
                "abnormal": report.get("abnormal"),
            }
            for report in reports
        }
        rows = [dict(row) for row in store.rows(COMPARE_MODULE)]
        for row in rows:
            # 报告状态属于分析报告本身，读取时按当前报告同步，避免结果表留着旧状态。
            row.update(status_by_code.get(str(row.get("报告编号")), {}))
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """详情：分析报告 + 同版本的 AB对比结果，一次读取保持口径一致。"""
        report = store.find(MODULE, entry_id)
        if report is None:
            return None
        self._ensure_compare_table(self._reports())
        detail = dict(report)
        compare = next(
            (row for row in store.rows(COMPARE_MODULE)
             if str(row.get("报告编号")) == str(report.get("报告编号"))),
            None,
        )
        if compare is not None:
            detail.update({
                key: value for key, value in compare.items()
                if key not in {"id", "status", "pending", "abnormal"}
            })
            detail["AB对比结果"] = dict(compare)
        return detail

    # ------------------------------------------------------------------ 汇总口径
    def _reports(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(MODULE)]

    def metric_version_for(self, report: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """读取指标定义里的口径版本，作为对比结果行的指标版本。"""
        name = str(report.get("分析指标") or "").strip()
        for metric in store.rows("metric_def"):
            if name and name in {str(metric.get("指标名称") or ""), str(metric.get("指标编号") or "")}:
                return str(metric.get("口径版本") or metric.get("指标版本") or "v1"), metric
        return str(report.get("口径版本") or report.get("指标版本") or "v1"), None

    def _groups_for(self, report: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """把一个报告解析成同一实验下的对照组、实验组分群。

        只认同一个 experiment_id 下成对的对照/实验组；找不到成对分群时返回空，
        由调用方按报告编号派生稳定成员，绝不跨实验拼凑两侧成员。
        """
        code = str(report.get("实验编号") or "").strip()
        experiment = None
        for candidate in store.rows("experiment"):
            if code and code in {str(candidate.get("方案编号") or ""), str(candidate.get("id") or "")}:
                experiment = candidate
                break
        if experiment is None:
            return None, None
        groups = [
            group for group in store.rows("sample_group")
            if str(group.get("experiment_id") or "") == str(experiment.get("id"))
        ]
        control = next((g for g in groups if g.get("group_type") == "control"), None)
        treated = next((g for g in groups if g.get("group_type") == "experiment"), None)
        if control is None or treated is None:
            return None, None
        return control, treated

    # ------------------------------------------------------------------ 均值
    @staticmethod
    def _mean_units(members: list[dict[str, Any]]) -> int:
        """把成员指标值换算成百万分之一的整数再求均值，避免浮点漂移。"""
        if not members:
            raise CompareTableError("分群成员明细为空，无法计算均值")
        total = 0
        for member in members:
            try:
                total += int(round(float(member.get("metric_value", 0)) * SCALE))
            except (TypeError, ValueError) as exc:
                raise CompareTableError(f"成员指标值无法解析：{member.get('metric_value')!r}") from exc
        return int(round(total / len(members)))

    def _member_side(
        self, report: dict[str, Any], group: dict[str, Any] | None, group_type: str,
        *, epoch: int | None = None,
    ) -> tuple[int, int, int]:
        """读取一侧成员明细并算出均值；返回 (均值微单位, 样本量, 快照版本)。

        group 为 None 时用报告编号派生一批稳定的成员明细，保证同一报告每次
        得到的仍是同一批成员（而不是每次随机）。
        """
        if group is None:
            version = epoch or 1
            key = f"report:{report.get('报告编号') or report.get('id')}"
            members = self.sample_groups.generate_members(
                key, group_type, report.get("样本基数", 1000), version=version)
        else:
            # 统一到同一个快照纪元：两侧按同一 epoch 取数，
            # 差异与两侧均值才是真正同一批成员算出来的。
            version = epoch if epoch is not None else self.sample_groups.snapshot_version(group)
            members = self.sample_groups.generate_members(
                group.get("id"), str(group.get("group_type") or group_type),
                group.get("样本基数", 1000), version=version,
            )
        return self._mean_units(members), len(members), version

    # ------------------------------------------------------------------ 生成
    def _build_row(self, report: dict[str, Any], *, phase_hook: Callable[[str], None] | None = None) -> dict[str, Any]:
        """生成一行对比结果：阶段一重算两侧均值，阶段二才由均值写出差异。"""
        # 阶段一：同一成员快照上的两侧均值。此时还没有任何「差异」值。
        if phase_hook:
            phase_hook("means")
        control, treated = self._groups_for(report)
        # 同一报告的成员快照与指标口径可能同时更新：统一到一个快照纪元，
        # 先让两侧成员对齐，再重算均值，最后才生成差异。
        epoch = max(
            self.sample_groups.snapshot_version(group)
            for group in (control, treated) if group is not None
        ) if (control is not None or treated is not None) else 1
        control_units, control_size, control_version = self._member_side(
            report, control, "control", epoch=epoch)
        treated_units, treated_size, treated_version = self._member_side(
            report, treated, "experiment", epoch=epoch)
        if control_units is None or treated_units is None:
            raise CompareTableError("两侧均值不完整，拒绝生成差异")
        metric_version, _metric = self.metric_version_for(report)

        # 阶段二：差异严格等于实验组均值减对照组均值（同一口径、同一成员快照）。
        if phase_hook:
            phase_hook("diff")
        control_value = control_units / SCALE
        treated_value = treated_units / SCALE
        # 结果表的数值精度统一为 6 位小数：差异 = 实验组均值 - 对照组均值，
        # 再由同一精度收敛，接口与导出拿到的就是能直接手算核对的干净数值。
        diff_value = round(treated_value - control_value, 6)
        if diff_value > 0:
            effect = "实验组更高"
        elif diff_value < 0:
            effect = "实验组更低"
        else:
            effect = "无差异"
        return {
            "id": int(report.get("id") or 0),
            "报告编号": report.get("报告编号"),
            "实验编号": report.get("实验编号"),
            "分析指标": report.get("分析指标"),
            "指标版本": metric_version,
            "置信水平": report.get("置信水平"),
            # 效果方向由差异符号决定，避免页面出现与正负号对不上的说明。
            "效果方向": effect,
            "对照组均值": control_value,
            "实验组均值": treated_value,
            "差异": diff_value,
            # 兼容旧口径字段名：指标值=实验组均值，对比基准=对照组均值，差异仍由二者直接相减。
            "指标值": treated_value,
            "对比基准": control_value,
            "对照组成员版本": control_version,
            "实验组成员版本": treated_version,
            "对照组样本量": control_size,
            "实验组样本量": treated_size,
            "成员快照": f"control@{control_version}+experiment@{treated_version}",
            "均值复核状态": "已复核",
            "口径同步": "已同步",
            "核对状态": "待核对",
            "核对说明": "",
            "版本冲突处理": "先重算均值再生成差异",
            "status": report.get("status"),
            "pending": report.get("pending"),
            "abnormal": report.get("abnormal"),
        }

    def _build_rows(
        self, reports: list[dict[str, Any]], *, phase_hook: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        rows = [self._build_row(report, phase_hook=phase_hook) for report in reports]
        # 全部均值算完才统一核对，任何一行不合格都不会进入结果表。
        self._validate_rows(rows, reports)
        return rows

    # ------------------------------------------------------------------ 核对
    @staticmethod
    def _signature(rows: list[dict[str, Any]]) -> tuple:
        return tuple(
            tuple(row.get(field) for field in _SIGNATURE_FIELDS)
            for row in sorted(rows, key=lambda item: str(item.get("报告编号") or ""))
        )

    def _expected_snapshot(self, report: dict[str, Any]) -> tuple[int, str]:
        """当前应有的成员快照纪元与指标版本；用于判断结果行是否已经过期。"""
        control, treated = self._groups_for(report)
        versions = [
            self.sample_groups.snapshot_version(group)
            for group in (control, treated) if group is not None
        ]
        epoch = max(versions) if versions else 1
        metric_version, _metric = self.metric_version_for(report)
        return epoch, str(metric_version)

    def _validate_rows(self, rows: list[dict[str, Any]], reports: list[dict[str, Any]]) -> None:
        """核对结果表：一行一报告、无重复无残留、差异等于同一快照的两侧均值差。"""
        expected = [str(report.get("报告编号")) for report in reports]
        actual = [str(row.get("报告编号")) for row in rows]
        if not rows and not expected:
            return
        if len(actual) != len(set(actual)):
            raise CompareTableError("AB对比结果表存在重复报告行（残留）")
        if sorted(actual) != sorted(expected):
            raise CompareTableError("AB对比结果表与分析报告不一致（残留或缺行）")
        for row in rows:
            for field in ("对照组均值", "实验组均值", "差异"):
                if row.get(field) is None:
                    raise CompareTableError(f"{row.get('报告编号')} 缺少{field}，拒绝提交")
            try:
                control = float(row["对照组均值"])
                treated = float(row["实验组均值"])
                diff = float(row["差异"])
            except (TypeError, ValueError) as exc:
                raise CompareTableError(f"{row.get('报告编号')} 的均值/差异不是数值") from exc
            # 结果表内三列同为 6 位小数，按同一精度比对，允许浮点末位亚单位误差。
            if abs(diff - (treated - control)) > 5e-7:
                raise CompareTableError(
                    f"{row.get('报告编号')} 的差异 {diff} 不等于实验组均值减对照组均值 {treated - control}")
            if row.get("对照组成员版本") != row.get("实验组成员版本"):
                # 两侧成员不是同一版快照时必须重新取数，不能直接写差异。
                raise CompareTableError(f"{row.get('报告编号')} 两侧成员快照版本不一致")
            report = next(
                (item for item in reports if str(item.get("报告编号")) == str(row.get("报告编号"))), None)
            if report is not None:
                expected_epoch, expected_metric_version = self._expected_snapshot(report)
                if int(row.get("对照组成员版本") or 0) != expected_epoch:
                    raise CompareTableError(
                        f"{row.get('报告编号')} 的结果行停留在成员快照 v{row.get('对照组成员版本')}，"
                        f"当前应为 v{expected_epoch}")
                if str(row.get("指标版本")) != expected_metric_version:
                    raise CompareTableError(
                        f"{row.get('报告编号')} 的指标版本 {row.get('指标版本')} 与指标定义 {expected_metric_version} 不一致")
            row["核对状态"] = "已核对"
            row["核对说明"] = "差异=实验组均值-对照组均值（同一成员快照、同一口径）"

    # ------------------------------------------------------------------ 版本
    def _current_table_is_complete(self, reports: list[dict[str, Any]]) -> bool:
        rows = list(store.rows(COMPARE_MODULE))
        if not reports:
            # 没有任何分析报告时结果表必须为空，否则就是残留。
            return not rows
        if not rows:
            return False
        try:
            # 校验只在副本上打核对标记，读取路径不改写已提交的数据。
            self._validate_rows([dict(row) for row in rows], reports)
        except CompareTableError:
            return False
        return True

    def _restore_last_complete(self) -> None:
        """从上一完整版本恢复结果表；备份为空时保持当前已提交版本不变。"""
        backup = store.rows(BACKUP_MODULE)
        if backup:
            store.replace_rows(COMPARE_MODULE, backup)

    def _ensure_compare_table(self, reports: list[dict[str, Any]]) -> None:
        """读取前保证结果表完整；有残留时按结果表先重建核对，失败则恢复上一完整版本。"""
        if self._current_table_is_complete(reports):
            return
        if not reports and not store.rows(COMPARE_MODULE):
            return
        result = self.recalculate()
        if not result.get("ok"):
            self._restore_last_complete()

    # ------------------------------------------------------------------ 重算
    def recalculate(
        self,
        *,
        retries: int = MAX_RETRIES,
        phase_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """在事务中重建整张 AB对比结果表，失败重试并回滚到上一完整版本。"""
        reports = self._reports()
        retries = max(1, int(retries))
        last_error = "未知错误"
        for attempt in range(1, retries + 1):
            store.begin()
            try:
                built = self._build_rows(reports, phase_hook=phase_hook)
                signature = self._signature(built)
                current = list(store.rows(COMPARE_MODULE))
                if current and self._current_table_is_complete(reports) and self._signature(current) == signature:
                    # 内容没有变化：保持同一个结果版本，刷新/下游读取不漂移。
                    stored_version = int(current[0].get("结果版本") or store.version(COMPARE_MODULE))
                    store.commit()
                    return {
                        "ok": True, "reused": True, "attempts": attempt,
                        "version": stored_version,
                        "items": self.list_results(), "message": "AB对比结果表核对一致，沿用当前版本",
                    }
                version = store.bump_version(COMPARE_MODULE)
                stamp = datetime.now().isoformat(timespec="seconds")
                for row in built:
                    row["结果版本"] = version
                    row["生成时间"] = stamp
                # 只有整表都生成并核对通过后，才一次性替换；不会写入部分成员结果。
                store.replace_rows(COMPARE_MODULE, built)
                store.replace_rows(BACKUP_MODULE, built)
                store.commit()
                return {
                    "ok": True, "reused": False, "attempts": attempt,
                    "version": version, "items": built,
                    "message": f"AB对比结果表已重新生成并核对（版本 {version}）",
                }
            except Exception as exc:  # noqa: BLE001 - 任何失败都必须整体回滚后重试
                last_error = str(exc)
                store.rollback()
                time.sleep(0)
        current = list(store.rows(COMPARE_MODULE))
        if current and not self._current_table_is_complete(reports):
            self._restore_last_complete()
        return {
            "ok": False,
            "attempts": retries,
            "version": store.version(COMPARE_MODULE),
            "items": list(store.rows(COMPARE_MODULE)),
            "error": last_error,
            "message": f"AB对比结果表重算失败（{retries} 次重试），已回滚到上一完整版本",
        }

    # 兼容不同调用方对「重建」的命名
    rebuild_compare_table = recalculate
    generate_compare_table = recalculate
    refresh_compare_table = recalculate
    sync_compare_results = recalculate

    # ------------------------------------------------------------------ 运行分析
    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """运行分析：基于结果表返回对比行，差异与结果表保持同一口径。"""
        raw_count = params.get("样本范围", params.get("并行数量", "6"))
        try:
            count = max(1, int(raw_count))
        except (TypeError, ValueError):
            count = 6
        reports = self._reports()
        wanted = str(params.get("报告编号") or "").strip()
        if wanted:
            reports = [r for r in reports if str(r.get("报告编号")) == wanted]
        if reports:
            result = self.recalculate()
            rows = list(result.get("items") or store.rows(COMPARE_MODULE))
            if wanted:
                rows = [row for row in rows if str(row.get("报告编号")) == wanted]
            return rows[:count]

        # 没有分析报告时给出一组同口径的模拟对比行，差异仍由两侧均值直接得出。
        results: list[dict[str, Any]] = []
        for index in range(count):
            key = f"analysis:{index + 1}"
            control_units = self._mean_units(
                self.sample_groups.generate_members(key, "control", 500, version=1))
            treated_units = self._mean_units(
                self.sample_groups.generate_members(key, "experiment", 500, version=1))
            results.append({
                "id": index + 1,
                "分组": f"分组{(index % 3) + 1}",
                "对照组均值": control_units / SCALE,
                "实验组均值": treated_units / SCALE,
                "指标值": treated_units / SCALE,
                "对比基准": control_units / SCALE,
                "差异": (treated_units - control_units) / SCALE,
            })
        return results

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        report = store.find(MODULE, entry_id)
        if report is None:
            return None, f"分析报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于AB结果分析可执行范围"
        report["status"] = ACTION_RULES[action]
        report["pending"] = report["status"] != STATUS_ORDER[-1]
        report["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return report, f"分析报告已{action}"
