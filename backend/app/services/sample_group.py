"""样本分群业务规则：提供可复现的分群成员快照供AB分析消费。

AB 的差异必须由同一批成员算出，因此成员列表不能每次随机生成：
同一个分群 + 同一个「成员快照版本」必须永远返回同一批用户和同一组指标值。
确认就绪会推进快照版本，等价于重新拉取一次成员明细。
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.store import store

MODULE = "sample_group"
LIST_FIELDS = ["分群编号", "分群名称", "筛选条件", "样本基数", "分群日期", "更新方式", "有效天数", "负责人"]
REQUIRED_FIELDS = ["分群编号", "分群名称", "筛选条件"]
STATUS_ORDER = ["草稿", "计算中", "已就绪", "已过期"]
ACTION_RULES = {"开始计算": "计算中", "确认就绪": "已就绪", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []
MEMBER_CAP = 500
SNAPSHOT_VERSION_FIELD = "成员快照版本"


class SampleGroupService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("分群编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def snapshot_version(self, group: dict[str, Any]) -> int:
        """当前成员快照版本；缺省视为第一版。"""
        try:
            return max(1, int(group.get(SNAPSHOT_VERSION_FIELD) or 1))
        except (TypeError, ValueError):
            return 1

    def generate_members(
        self,
        group_key: str | int,
        group_type: str = "control",
        base: int | str | None = 1000,
        *,
        version: int = 1,
    ) -> list[dict[str, Any]]:
        """按 (分群, 类型, 版本) 确定性生成成员明细。

        相同入参必然得到相同成员和相同指标值，刷新、重试都不会漂移；
        换版本则是一批全新的成员，用于「成员快照已更新」的场景。
        """
        try:
            base_n = int(base) if base is not None else 1000
        except (TypeError, ValueError):
            base_n = 1000
        size = max(1, min(base_n, MEMBER_CAP))
        rng = random.Random(f"{group_key}|{group_type}|{int(version)}")
        mu = 0.62 if group_type == "experiment" else 0.55
        members: list[dict[str, Any]] = []
        for index in range(size):
            value = rng.gauss(mu=mu, sigma=0.08)
            members.append({
                "user_id": f"U{group_key}{index:04d}".replace(":", ""),
                "group_id": group_key,
                "group_type": group_type,
                "metric_value": round(max(0.0, min(1.0, value)), 4),
            })
        return members

    def get_group_members(self, group_id: int) -> list[dict[str, Any]]:
        """读取分群当前版本的成员明细；同一版本内结果稳定可复现。"""
        sg = self.get_entry(group_id)
        if sg is None:
            return []
        return self.generate_members(
            group_id,
            str(sg.get("group_type") or "control"),
            sg.get("样本基数", 1000),
            version=self.snapshot_version(sg),
        )

    def refresh_snapshot(self, group_id: int) -> int | None:
        """推进成员快照版本，下一次读取会得到一批新成员。"""
        sg = self.get_entry(group_id)
        if sg is None:
            return None
        version = self.snapshot_version(sg) + 1
        sg[SNAPSHOT_VERSION_FIELD] = version
        store.bump_version(MODULE)
        return version

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry[SNAPSHOT_VERSION_FIELD] = 1
        entry["created_at"] = time.time()
        store.rows(MODULE).append(entry)
        store.bump_version(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"样本分群 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于样本分群可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action in {"开始计算", "确认就绪"}:
            # 重新计算的动作会产出一批新的成员明细，必须推进快照版本。
            entry[SNAPSHOT_VERSION_FIELD] = self.snapshot_version(entry) + 1
        store.bump_version(MODULE)
        return entry, f"样本分群已{action}"
