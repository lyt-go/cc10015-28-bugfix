"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部派生表：给业务流程存放中间结果，不算业务模块，不出现在概览和健康检查里。
INTERNAL_MODULES = {"ab_compare_results", "ab_compare_results_backup"}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._versions: dict[str, int] = {name: 1 for name in self._tables}
        self._created_at: dict[str, float] = {name: time.time() for name in self._tables}
        self._journal: list[tuple[str, list[dict[str, Any]], int]] | None = None

    # ---- 事务：整表快照，任一步失败整体回到事务开始前的版本 ----
    def begin(self) -> None:
        """记录所有表的完整快照，作为本次事务可回滚到的唯一版本。"""
        self._journal = [
            (name, copy.deepcopy(rows), self._versions.get(name, 1))
            for name, rows in self._tables.items()
        ]

    def commit(self) -> None:
        """提交：截止版本一次性推进，读侧不会看到半成品。"""
        self._journal = None

    def rollback(self) -> list[str]:
        """回滚到事务开始时的完整版本，返回被恢复的模块名。"""
        if self._journal is None:
            return []
        restored: list[str] = []
        for name, rows, version in self._journal:
            self._tables[name] = rows
            self._versions[name] = version
            restored.append(name)
        # 事务期间新建的表也要撤掉，否则会留下「事务产物」这种残留。
        known = {name for name, _rows, _version in self._journal}
        for name in list(self._tables):
            if name not in known:
                del self._tables[name]
                self._versions.pop(name, None)
                restored.append(name)
        self._journal = None
        return restored

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def in_transaction(self) -> bool:
        return self._journal is not None

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def replace_rows(self, module: str, rows: list[dict[str, Any]]) -> None:
        """整表替换：只用于事务内一次性写入完整结果，避免部分写入。"""
        self._tables[module] = [dict(row) for row in rows]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def filter_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def bump_version(self, module: str) -> int:
        self._versions[module] = self._versions.get(module, 0) + 1
        return self._versions[module]

    def version(self, module: str) -> int:
        return self._versions.get(module, 1)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
