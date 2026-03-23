import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("omnis.llm")

class OmnisLLMClient:
    """
    轻量级 LLM 客户端，专门用于后台小模型（如 Qwen-1.5B/7B）的意图推演。
    设计为同步阻塞调用，在后台线程池中使用。
    """

    def __init__(self, api_base: str = "", api_key: str = "", model: str = ""):
        self.api_base = api_base.rstrip("/")
        if not self.api_base:
            # 默认使用本地 Ollama 接口
            self.api_base = "http://127.0.0.1:11434/v1"
        self.api_key = api_key or "sk-omnis-local"
        self.model = model or "qwen2.5:7b"
        self.timeout = 15.0
        self.max_retries = 2

    def _request_json(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data_bytes = json.dumps(payload).encode("utf-8")
        for idx in range(self.max_retries + 1):
            req = urllib.request.Request(
                f"{self.api_base}/chat/completions",
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                code = int(getattr(e, "code", 0) or 0)
                if idx < self.max_retries and code in {408, 409, 425, 429, 500, 502, 503, 504}:
                    time.sleep(0.2 * (idx + 1))
                    continue
                logger.error(f"LLM request http_error: {code}")
                return None
            except urllib.error.URLError as e:
                if idx < self.max_retries:
                    time.sleep(0.2 * (idx + 1))
                    continue
                logger.error(f"LLM request network_error: {e}")
                return None
            except Exception as e:
                if idx < self.max_retries:
                    time.sleep(0.2 * (idx + 1))
                    continue
                logger.error(f"LLM request failed: {e}")
                return None
        return None

    def _safe_excerpt(self, text: str, limit: int = 120) -> str:
        s = str(text or "").replace("\n", " ").strip()
        if len(s) <= limit:
            return s
        return s[: limit - 1] + "…"

    def generate_intent(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """
        请求模型生成角色的下一步行动。
        期望模型返回严格的 JSON 格式：{"intent": "...", "location": "..."}
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "response_format": {"type": "json_object"}
        }

        data = self._request_json(payload)
        if not data:
            return None
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return None
        try:
            result = json.loads(content)
            if "intent" in result and "location" in result:
                out: Dict[str, Any] = {
                    "intent": str(result["intent"]).strip()[:50],
                    "location": str(result["location"]).strip()[:20]
                }
                if "narrative" in result:
                    out["narrative"] = str(result.get("narrative", "")).strip()[:500]
                if "thought" in result:
                    out["thought"] = str(result.get("thought", "")).strip()[:200]
                if "time_elapsed_ticks" in result:
                    try:
                        out["time_elapsed_ticks"] = int(result.get("time_elapsed_ticks", 1))
                    except Exception:
                        out["time_elapsed_ticks"] = 1
                return out
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM output as JSON: {self._safe_excerpt(content)}")
            return None
        return None

    def adjudicate_interaction_structured(self, system_prompt: str, scenario: str) -> Optional[Dict[str, Any]]:
        """
        供 DM（主持人）使用的判定接口（结构化输出）。
        传入场景描述，返回 JSON 格式的推演结果（包含简报和关系变动）。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": scenario},
            ],
            "temperature": 0.8,
            "response_format": {"type": "json_object"}
        }
        
        data = self._request_json(payload)
        if not data:
            return None
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse DM structured output as JSON: {self._safe_excerpt(content)}")
            return None

    def test_connection(self) -> tuple[bool, str]:
        """
        测试 LLM API 连接是否正常。
        返回 (是否成功, 错误信息或成功提示)
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
            "max_tokens": 5
        }
        
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST"
        )
        
        try:
            with urllib.request.urlopen(req, timeout=5.0) as response:
                if response.getcode() == 200:
                    return True, "连接成功！"
                else:
                    return False, f"HTTP {response.getcode()}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP 错误: {e.code} - {e.reason}"
        except urllib.error.URLError as e:
            return False, f"网络连接失败: {e.reason}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"
