"""
防封策略模块。
提供账号间的随机等待、节点轮换等逻辑，降低批量操作被微博风控的概率。
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime

from . import database

log = logging.getLogger("weibo.antibab")


class AntiBanPolicy:
    """防封策略配置与执行。"""

    def __init__(self, enabled: bool, wait_min: int, wait_max: int,
                 window_hour: int):
        self.enabled = enabled
        self.wait_min = max(0, wait_min)
        self.wait_max = max(self.wait_min, wait_max)
        self.window_hour = window_hour

    @classmethod
    def from_settings(cls) -> "AntiBanPolicy":
        return cls(
            enabled=database.get_setting("anti_ban_enabled", "1") == "1",
            wait_min=int(database.get_setting("anti_ban_wait_min", "120") or 120),
            wait_max=int(database.get_setting("anti_ban_wait_max", "300") or 300),
            window_hour=int(database.get_setting("anti_ban_window_hour", "7") or 7),
        )

    def in_window(self) -> bool:
        """是否处于防封窗口（凌晨 N 点前）。"""
        return datetime.now().hour < self.window_hour

    def should_wait(self) -> bool:
        """是否需要在账号间等待。"""
        return self.enabled and self.in_window()

    def wait_between_accounts(self, account_index: int, total: int) -> float:
        """账号间随机等待，返回实际等待秒数。首个账号也可能等待。"""
        if not self.should_wait():
            return 0.0
        wait = random.uniform(self.wait_min, self.wait_max)
        log.info("⏳ 防封等待 %.1f 秒后执行账号 %d/%d",
                 wait, account_index, total)
        time.sleep(wait)
        return wait

    def describe(self) -> str:
        if not self.enabled:
            return "防封策略：关闭"
        return (f"防封策略：开启（账号间随机等待 {self.wait_min}s~{self.wait_max}s，"
                f"凌晨 {self.window_hour} 点前生效）")


def node_rotation(proxy_count: int, account_index: int) -> int:
    """根据账号序号轮换代理节点索引。"""
    if proxy_count <= 0:
        return 0
    return (account_index - 1) % proxy_count
