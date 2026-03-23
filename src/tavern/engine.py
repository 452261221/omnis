
import json
import re
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional
from .renderer import Renderer
from .card_manager import CardManager
from .history import HistoryManager

class TavernEngine:
    """
    The new 'Director' logic.
    Combines:
    1. Card loading (CardManager)
    2. Context building (HistoryManager + Renderer)
    3. LLM Request (Roleplay Mode)
    4. Action Parsing (Dual-Pass / Sidecar)
    """
    
    def __init__(self, api_base: str = "", api_key: str = "", model: str = ""):
        self.renderer = Renderer()
        self.card_manager = CardManager()
        self.histories: Dict[str, HistoryManager] = {}
        
        # Use simple HTTP requests directly instead of relying on external planner
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_sec = 60.0
        self.jailbreak_enabled = False
        self.jailbreak_prompt_suffix = ""
        self.hide_safe_blocks = True
        self.hide_thinking_blocks = True
        self.extra_output_filters: List[Dict[str, str]] = []

    def update_config(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model

    def update_jailbreak_config(self, config: Dict[str, Any]) -> None:
        cfg = config if isinstance(config, dict) else {}
        self.jailbreak_enabled = bool(cfg.get("enabled", False))
        self.jailbreak_prompt_suffix = str(cfg.get("prompt_suffix", "") or "").strip()
        self.hide_safe_blocks = bool(cfg.get("hide_safe_blocks", True))
        self.hide_thinking_blocks = bool(cfg.get("hide_thinking_blocks", True))
        rows = cfg.get("extra_output_filters", [])
        cleaned: List[Dict[str, str]] = []
        if isinstance(rows, list):
            for row in rows[:20]:
                if not isinstance(row, dict):
                    continue
                pattern = str(row.get("pattern", "") or "").strip()
                if not pattern:
                    continue
                cleaned.append(
                    {
                        "name": str(row.get("name", "") or "").strip() or "extra",
                        "pattern": pattern,
                        "flags": str(row.get("flags", "") or "").strip().lower(),
                    }
                )
        self.extra_output_filters = cleaned

    def reflect(self, character_name: str, memories: List[str]) -> str:
        """
        触发角色的反思机制，将多条短期记忆总结为一条高价值的长期洞察 (Insight)。
        """
        if not self.api_base or not self.api_key or not self.model:
            return "（无法反思：LLM 未配置）"

        if not memories:
            return ""

        prompt = f"你正在分析角色【{character_name}】的近期经历。\n"
        prompt += "以下是该角色最近的几条记忆片段：\n"
        for i, mem in enumerate(memories):
            prompt += f"{i+1}. {mem}\n"
        prompt += "\n请根据这些记忆，总结出一条关于世界、他人或自身的深刻洞察（Insight）。"
        prompt += "\n要求：用第一人称（我）陈述，简明扼要，揭示潜在的动机、威胁或态度转变。不超过50字。"

        try:
            insight = self._call_llm(prompt)
            return insight.strip()
        except Exception as e:
            print(f"[TavernEngine] Reflection error: {e}")
            return f"（反思失败：{e}）"

    def get_history_manager(self, session_id: str) -> HistoryManager:
        if session_id not in self.histories:
            self.histories[session_id] = HistoryManager()
        return self.histories[session_id]

    def prompt_instruction_injection(self) -> str:
        """
        Returns the system instruction for action embedding.
        """
        return (
            "\n【重要指令】\n"
            "在进行剧情描写时，如果你的动作涉及对其他角色的实质性影响（如攻击、偷窃、给予物品），"
            "请在段落末尾附带隐式指令，格式为：[ACTION: TYPE target=名字 param=value]。\n"
            "支持的 ACTION 类型：\n"
            "- ATTACK (攻击): [ACTION: ATTACK target=李四]\n"
            "- STEAL (偷窃): [ACTION: STEAL target=李四]\n"
            "- GIVE (给予): [ACTION: GIVE target=李四 item=金疮药]\n"
            "- MOVE (移动): [ACTION: MOVE target=后院]\n"
            "例如：\n"
            "张三怒吼一声，挥拳打向李四的脸！[ACTION: ATTACK target=李四]"
        )

    def jailbreak_instruction_injection(self) -> str:
        if not self.jailbreak_enabled:
            return ""
        base = (
            "\n【输出格式要求】\n"
            "你必须在正文结尾额外输出：\n"
            "1) <thinking>用于内部分析；\n"
            "2) <safe>用于英文免责声明，声明文本仅用于虚构创作演示，不可用于现实行为参考。\n"
            "注意：正文内容与标签内容分段输出，不要混写。"
        )
        if self.jailbreak_prompt_suffix:
            return f"{base}\n{self.jailbreak_prompt_suffix}"
        return base

    def _clean_model_output(self, text: str) -> tuple[str, list[dict[str, str]]]:
        cleaned = str(text or "")
        hidden_blocks: list[dict[str, str]] = []
        patterns: list[tuple[str, str, int]] = []
        if self.hide_safe_blocks:
            patterns.append(("safe", r"<safe>[\s\S]*?</safe>", re.IGNORECASE))
        if self.hide_thinking_blocks:
            patterns.append(("thinking", r"<thinking>[\s\S]*?</thinking>", re.IGNORECASE))
        for row in self.extra_output_filters:
            flags = 0
            raw_flags = str(row.get("flags", "") or "")
            if "i" in raw_flags:
                flags |= re.IGNORECASE
            if "m" in raw_flags:
                flags |= re.MULTILINE
            if "s" in raw_flags:
                flags |= re.DOTALL
            patterns.append((str(row.get("name", "extra") or "extra"), str(row.get("pattern", "")), flags))
        for name, pattern, flags in patterns:
            if not pattern:
                continue
            try:
                matches = re.findall(pattern, cleaned, flags=flags)
            except re.error:
                continue
            if matches:
                preview = []
                for m in matches[:10]:
                    t = m if isinstance(m, str) else (m[0] if isinstance(m, tuple) and m else "")
                    t = str(t).strip()
                    if t:
                        preview.append(t[:240])
                if preview:
                    hidden_blocks.append({"name": name, "count": str(len(matches)), "preview": "\n".join(preview[:3])})
                try:
                    cleaned = re.sub(pattern, "", cleaned, flags=flags)
                except re.error:
                    continue
        cleaned = cleaned.strip()
        return cleaned, hidden_blocks

    def step(self,
             character_card: Dict[str, Any],
             user_name: str,
             user_input: str,
             scenario_context: str = "",
             world_info: str = "",
             session_id: str = "default") -> Dict[str, Any]:
        """
        Executes a full turn: Build prompt -> Call LLM -> Parse Response -> Update History
        """
        hm = self.get_history_manager(session_id)

        # 1. Add user message to history
        if user_input:
            hm.add_message("user", user_input, user_name)

        # 2. Build Prompt
        system_prompt = (
            character_card.get("system_prompt", "")
            + self.prompt_instruction_injection()
            + self.jailbreak_instruction_injection()
        )

        budget = max(300, min(2400, 3200 - len(str(scenario_context or "")) // 3))
        full_prompt = self.renderer.render_story_string(
            system_prompt=system_prompt,
            character_name=character_card.get("name", "Unknown"),
            character_desc=character_card.get("description", ""),
            scenario=scenario_context,
            world_info=world_info,
            persona=character_card.get("personality", ""),
            chat_history=hm.get_context(token_budget=budget),
            user_name=user_name,
            user_input=user_input,
            mes_example=character_card.get("mes_example", ""),
            post_history_instructions=character_card.get("post_history_instructions", ""),
        )

        # 3. Call LLM
        raw_output = self._call_llm(full_prompt)

        # 4. Parse & Update
        return self.process_model_response(
            raw_output,
            session_id,
            character_card.get("name", "Unknown"),
            prompt=full_prompt,
        )

    def step_for_agent(self,
                       character_card: Dict[str, Any],
                       user_name: str,
                       user_input: str,
                       scenario_context: str = "",
                       world_info: str = "",
                       session_id: str = "default",
                       llm_caller: Optional[Callable[[str], str]] = None,
                       ) -> Dict[str, Any]:
        """
        与 step() 完全一致的 Prompt 构建 + 历史管理，
        但使用外部 llm_caller 代替内置 _call_llm，
        用于让 NPC 智能体走与玩家完全一致的路径。
        """
        hm = self.get_history_manager(session_id)

        # 1. 添加旁白/世界事件为 user 消息
        if user_input:
            hm.add_message("user", user_input, user_name)

        # 2. 构建与玩家完全一致的 Prompt
        system_prompt = (
            character_card.get("system_prompt", "")
            + self.prompt_instruction_injection()
        )

        budget = max(300, min(2400, 3200 - len(str(scenario_context or "")) // 3))
        full_prompt = self.renderer.render_story_string(
            system_prompt=system_prompt,
            character_name=character_card.get("name", "Unknown"),
            character_desc=character_card.get("description", ""),
            scenario=scenario_context,
            world_info=world_info,
            persona=character_card.get("personality", ""),
            chat_history=hm.get_context(token_budget=budget),
            user_name=user_name,
            user_input=user_input,
            mes_example=character_card.get("mes_example", ""),
            post_history_instructions=character_card.get("post_history_instructions", ""),
        )

        # 3. 使用外部 LLM caller 或回退到内置
        if llm_caller:
            raw_output = llm_caller(full_prompt)
        else:
            raw_output = self._call_llm(full_prompt)

        # 4. 解析与更新历史（与 step 完全一致）
        return self.process_model_response(
            raw_output,
            session_id,
            character_card.get("name", "Unknown"),
            prompt=full_prompt,
        )

    def process_model_response(self, raw_output: str, session_id: str, char_name: str, prompt: str = "") -> Dict[str, Any]:
        """
        Parses the model output, extracts actions, and updates history.
        """
        clean_text, actions = self.card_manager.parse_instruction_embedding(raw_output)
        clean_text, hidden_blocks = self._clean_model_output(clean_text)
        hm = self.get_history_manager(session_id)
        hm.add_message("assistant", clean_text, char_name)
        
        return {
            "text": clean_text,
            "actions": actions,
            "hidden_blocks": hidden_blocks,
            "raw": raw_output,
            "prompt": prompt,
        }

    def _call_llm(self, prompt: str) -> str:
        if not self.api_base or not self.api_key:
            return "（系统提示：LLM API 未配置，无法生成回复。）"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
            "max_tokens": 500,
        }
        try:
            req = urllib.request.Request(
                url=f"{self.api_base}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}"
                }
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            return f"（系统提示：LLM 调用失败 - {e}）"
