#!/usr/bin/env python3
"""
用户画像管理
保留 user_profile.md 供用户手动编辑，对话历史由 SQLite 存储
"""

from pathlib import Path

from pocket_agent.memory import ProfileStore


class LongTermMemory:
    """用户画像管理（对话记忆由 SQLite messages 表承担）"""

    def __init__(self, memory_dir: str):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(exist_ok=True)
        self.profile_file = self.memory_dir / "user_profile.md"
        self._profile_store = ProfileStore(self.profile_file)

    def _init_user_profile(self):
        ProfileStore(self.profile_file)

    def get_user_profile(self) -> str:
        return self._profile_store.read()

    def update_user_profile(self, section: str, content: str):
        self._profile_store.update(section, content)
