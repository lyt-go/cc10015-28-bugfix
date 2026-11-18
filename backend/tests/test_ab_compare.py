"""AB对比结果表回归测试：差异口径、版本一致性、残留恢复、重试与跨模块核对。"""
import sys, os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.ab_result import BACKUP_MODULE, COMPARE_MODULE, AbResultService, CompareTableError
from app.services.experiment import ExperimentService
from app.services.metric_def import MetricDefService
from app.services.sample_group import SampleGroupService
from app.store import store


def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []
    for k in list(store._versions.keys()):
        store._versions[k] = 1


def _row_diff_equals_means(rows):
    for row in rows:
        # 均值与差异都收敛到 6 位小数，重算误差只在浮点末位，远小于 1e-9。
        assert abs(row["差异"] - (row["实验组均值"] - row["对照组均值"])) < 1e-9, row


def _seed_experiment_with_reports():
    _clear_all()
    ExperimentService().create_entry({
        "方案编号": "EXPE-9001", "实验名称": "首页改版", "对照组说明": "旧版",
        "实验组说明": "新版", "样本总量": "1000", "分流比例": "5:5",
    })
    store.rows("ab_result").append({
        "id": 1, "status": "已完成", "pending": False, "abnormal": False,
        "报告编号": "AB_R-9001", "实验编号": "EXPE-9001", "分析指标": "点击率",
    })
    store.rows("metric_def").append({
        "id": 1, "status": "已生效", "pending": False, "abnormal": False,
        "指标编号": "METR-9001", "指标名称": "点击率", "口径版本": "v7",
    })


def test_diff_is_treated_minus_control_and_stable_across_reads():
    _seed_experiment_with_reports()
    service = AbResultService()
    first = service.list_results()
    assert first, "应生成 AB对比结果表"
    _row_diff_equals_means(first)
    second = service.list_results()
    assert first == second, "刷新读取必须是同一版本、同一批成员"
    assert all(row["核对状态"] == "已核对" for row in second)


def test_experiment_groups_share_snapshot_epoch():
    _seed_experiment_with_reports()
    service = AbResultService()
    rows = service.list_results()
    assert all(row["对照组成员版本"] == row["实验组成员版本"] for row in rows)
    # 一侧推进快照后必须统一到同一纪元重算，不能拿旧行凑数。
    experiment_groups = store.rows("sample_group")
    SampleGroupService().refresh_snapshot(experiment_groups[1]["id"])
    refreshed = service.list_results()
    assert refreshed[0]["对照组成员版本"] == refreshed[0]["实验组成员版本"] == 2
    _row_diff_equals_means(refreshed)


def test_detail_and_list_read_same_result_version():
    _seed_experiment_with_reports()
    service = AbResultService()
    listed = service.list_results()[0]
    detail = service.get_entry(1)
    assert detail["差异"] == listed["差异"]
    assert detail["实验组均值"] == listed["实验组均值"]
    assert detail["结果版本"] == listed["结果版本"]


def test_stale_table_with_duplicate_rows_is_rebuilt():
    _seed_experiment_with_reports()
    service = AbResultService()
    service.list_results()
    stale = dict(store.rows(COMPARE_MODULE)[0])
    stale["差异"] = 9.9          # 残留的、无法解释的差异
    stale["核对状态"] = "待核对"
    store.rows(COMPARE_MODULE).append(stale)
    repaired = service.list_results()
    assert len(repaired) == 1, "重复残留行必须被清理"
    _row_diff_equals_means(repaired)
    assert repaired[0]["核对状态"] == "已核对"


def test_interrupted_recalculation_retries_without_partial_overwrite():
    _seed_experiment_with_reports()
    service = AbResultService()
    committed = service.list_results()
    version = store.version(COMPARE_MODULE)

    attempts = {"count": 0}

    def hook(phase):
        if phase == "diff":
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise CompareTableError("模拟重算中断")

    result = service.recalculate(phase_hook=hook)
    assert result["ok"] is True
    assert result["attempts"] == 3, "中断后必须重试"
    assert result["version"] == version, "成员与口径未变化时应沿用同一版本"
    assert result["items"] == committed, "不能用部分成员覆盖完整结果"


def test_failed_recalculation_keeps_last_complete_version():
    _seed_experiment_with_reports()
    service = AbResultService()
    committed = service.list_results()
    version = store.version(COMPARE_MODULE)

    def broken(phase):
        raise CompareTableError("模拟每次都中断")

    result = service.recalculate(retries=2, phase_hook=broken)
    assert result["ok"] is False
    assert result["attempts"] == 2
    assert result["version"] == version
    assert store.rows(COMPARE_MODULE) == committed, "失败必须回滚到上一完整版本"
    assert store.rows(BACKUP_MODULE) == committed


def test_snapshot_or_caliber_change_bumps_result_version():
    _seed_experiment_with_reports()
    service = AbResultService()
    service.list_results()
    version = store.version(COMPARE_MODULE)
    store.rows("metric_def")[0]["口径版本"] = "v8"
    rows = service.list_results()
    assert store.version(COMPARE_MODULE) == version + 1
    assert rows[0]["指标版本"] == "v8"
    _row_diff_equals_means(rows)


def test_metric_def_verifies_ab_compare_table():
    _seed_experiment_with_reports()
    verdict = MetricDefService().verify_ab_compare()
    assert verdict["ok"] is True, verdict
    assert verdict["release"] == "V028"
    assert verdict["checked"] == 1

    # 口径不一致时必须判为不通过，而不是默默放过。
    store.rows("metric_def")[0]["口径版本"] = "v9"
    store.rows(COMPARE_MODULE)[0]["指标版本"] = "v7"
    inconsistent = MetricDefService().verify_ab_compare()
    assert inconsistent["ok"] is False
    assert any("指标版本" in item["问题"] for item in inconsistent["problems"])
