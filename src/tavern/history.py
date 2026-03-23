
class HistoryManager:
    """
    Manages chat history with token budget awareness (sliding window).
    """
    def __init__(self, max_history_tokens: int = 2000):
        self.history = []
        self.max_history_tokens = max_history_tokens
        
    def add_message(self, role: str, content: str, name: str = ""):
        self.history.append({
            "role": role,
            "content": content,
            "name": name
        })

    def _estimate_tokens(self, text: str) -> int:
        t = str(text or "")
        return max(1, (len(t) + 3) // 4)

    def get_context(self, token_budget: int | None = None) -> list[dict]:
        budget = int(token_budget) if token_budget is not None else int(self.max_history_tokens)
        if budget <= 0:
            return []
        total = 0
        picked: list[dict] = []
        for msg in reversed(self.history):
            content = str(msg.get("content", ""))
            name = str(msg.get("name", ""))
            role = str(msg.get("role", ""))
            cost = self._estimate_tokens(content) + self._estimate_tokens(name) + self._estimate_tokens(role)
            if picked and total + cost > budget:
                break
            if not picked and cost > budget:
                trimmed = content[-max(80, budget * 4):]
                picked.append({"role": role, "content": trimmed, "name": name})
                break
            picked.append({"role": role, "content": content, "name": name})
            total += cost
        picked.reverse()
        return picked

    def to_list(self) -> list[dict]:
        """序列化为可 JSON 存储的列表"""
        return list(self.history)

    def from_list(self, data: list[dict]) -> None:
        """从列表恢复历史"""
        self.history = [msg for msg in data if isinstance(msg, dict)]

    def trim(self, max_messages: int = 40) -> None:
        """保留最近 max_messages 条消息"""
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]
