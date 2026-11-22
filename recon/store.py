"""核销状态的持久化。

单一 JSON 状态文件 + 原子替换；重启收件端不改变来源时钟、档案任务或会计回条，
因此本服务必须把游标、归档任务、读数、会计交接状态全部落盘，从同一状态继续。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_state() -> dict[str, Any]:
    return {
        "shift": 0,
        # 静态资料（按已知轮次解释后的最新视图与历史版本）
        "bridges": [],          # 桥接文件的全部版本
        "switch": [],           # 施工换表单的全部版本
        "close": [],            # 分表截账配置
        # 新运营商增量流水
        "feed_after": 0,
        "feed_floor": 0,
        "feed_compacted_days": [],
        "events": [],           # 已入账的原始事件
        # 旧运营商实时日账：operator-day -> 抓取状态
        "legacy_days": {},
        # 归档：token -> 任务；内容缓存另存 content 目录
        "archive_jobs": {},
        "archive_applied": {},  # "operator|day" -> token，保证同参数只申请一次
        # 每个运营商-营业日的有效取证视图
        "days": {},
        # 分表读数：meter|epoch|window -> 历次领取内容
        "readings": {},
        # 会计交接：request id -> 状态记录
        "handoffs": {},
        # 已提交给会计但尚无回条的请求（需要先查回条再决定是否重发）
        "in_flight": {},
        # 待人工确认的调账
        "pending_adjustments": [],
        # 运行轨迹，便于值班员/会计追溯
        "log": [],
    }


class StateStore:
    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "content").mkdir(exist_ok=True)
        self.path = self.directory / "state.json"
        self.state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return default_state()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def append_log(self, kind: str, **detail) -> None:
        entry = {"shift": self.state["shift"], "kind": kind, **detail}
        self.state["log"].append(entry)

    # 归档分片内容缓存（按文件名），附带核对摘要
    def content_path(self, filename: str) -> Path:
        return self.directory / "content" / filename

    def save_content(self, filename: str, raw: bytes) -> Path:
        path = self.content_path(filename)
        if not path.exists():
            tmp = path.with_suffix(".part")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        return path
