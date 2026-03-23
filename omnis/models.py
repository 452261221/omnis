from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ItemState:
    name: str
    owner: str = ""
    location: str = ""


@dataclass
class AgentState:
    name: str
    location: str
    intent: str
    salience: int = 0
    updated_at: float = 0.0
    journal: list[str] = None
    status: str = "alive"
    ai_enabled: bool = True
    summary: str = "暂无简介"
    card_description: str = ""
    card_personality: str = ""
    card_scenario: str = ""
    full_card: dict = None
    chat_history_data: list = None

    def __post_init__(self):
        if self.journal is None:
            self.journal = []
        if self.chat_history_data is None:
            self.chat_history_data = []
