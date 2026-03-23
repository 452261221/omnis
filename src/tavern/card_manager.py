
import base64
import json
import re
import shlex
import zlib
from typing import Dict, Any

class CardManager:
    def load_card(self, file_path: str) -> Dict[str, Any]:
        lower = str(file_path).lower()
        if lower.endswith(".png"):
            with open(file_path, "rb") as f:
                raw = f.read()
            return self.load_card_bytes(file_name=file_path, raw=raw)
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return self._normalize_card_data(data)

    def load_card_bytes(self, file_name: str, raw: bytes) -> Dict[str, Any]:
        lower = str(file_name or "").lower()
        if lower.endswith(".json"):
            text = raw.decode("utf-8", errors="ignore")
            data = json.loads(text)
            return self._normalize_card_data(data)
        if lower.endswith(".png"):
            data = self._extract_png_card_data(raw)
            return self._normalize_card_data(data)
        raise RuntimeError("仅支持 .json 或 .png 角色卡")

    def _extract_png_card_data(self, raw: bytes) -> Dict[str, Any]:
        sig = b"\x89PNG\r\n\x1a\n"
        if not raw.startswith(sig):
            raise RuntimeError("文件不是有效PNG")
        idx = 8
        texts: list[tuple[str, str]] = []
        while idx + 8 <= len(raw):
            length = int.from_bytes(raw[idx: idx + 4], "big")
            ctype = raw[idx + 4: idx + 8]
            start = idx + 8
            end = start + length
            if end + 4 > len(raw):
                break
            chunk_data = raw[start:end]
            idx = end + 4
            if ctype == b"IEND":
                break
            if ctype == b"tEXt":
                p = chunk_data.find(b"\x00")
                if p > 0:
                    key = chunk_data[:p].decode("latin1", errors="ignore")
                    val = chunk_data[p + 1 :].decode("latin1", errors="ignore")
                    texts.append((key, val))
            elif ctype == b"zTXt":
                p = chunk_data.find(b"\x00")
                if p > 0 and p + 2 <= len(chunk_data):
                    key = chunk_data[:p].decode("latin1", errors="ignore")
                    comp = chunk_data[p + 2 :]
                    try:
                        val = zlib.decompress(comp).decode("utf-8", errors="ignore")
                        texts.append((key, val))
                    except Exception:
                        pass
            elif ctype == b"iTXt":
                parts = chunk_data.split(b"\x00", 5)
                if len(parts) >= 6:
                    key = parts[0].decode("latin1", errors="ignore")
                    comp_flag = parts[1][:1]
                    text_data = parts[5]
                    try:
                        if comp_flag == b"\x01":
                            text_data = zlib.decompress(text_data)
                        val = text_data.decode("utf-8", errors="ignore")
                        texts.append((key, val))
                    except Exception:
                        pass
        candidates: list[str] = []
        for key, val in texts:
            k = str(key or "").strip().lower()
            if k in {"chara", "ccv3", "character", "card"}:
                candidates.insert(0, val)
            else:
                candidates.append(val)
        for text in candidates:
            parsed = self._try_parse_card_payload(text)
            if parsed is not None:
                return parsed
        raise RuntimeError("PNG 中未找到可解析角色卡数据")

    def _try_parse_card_payload(self, text: str) -> Dict[str, Any] | None:
        s = str(text or "").strip()
        if not s:
            return None
        if s.startswith("{"):
            try:
                data = json.loads(s)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        try:
            decoded = base64.b64decode(s.encode("utf-8"), validate=False)
            payload = decoded.decode("utf-8", errors="ignore").strip()
            if payload.startswith("{"):
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data
        except Exception:
            return None
        return None

    def _normalize_card_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        card = data
        if "data" in card and isinstance(card.get("data"), dict) and "name" in card["data"]:
            card = card["data"]
        return {
            "name": card.get("name", "Unknown"),
            "description": card.get("description", ""),
            "personality": card.get("personality", ""),
            "scenario": card.get("scenario", ""),
            "first_message": card.get("first_message", card.get("first_mes", "")),
            "mes_example": card.get("mes_example", ""),
            "creator_notes": card.get("creator_notes", ""),
            "system_prompt": card.get("system_prompt", ""),
            "post_history_instructions": card.get("post_history_instructions", ""),
            "alternate_greetings": card.get("alternate_greetings", []),
            "tags": card.get("tags", []),
            "creator": card.get("creator", ""),
            "character_version": card.get("character_version", ""),
            "extensions": card.get("extensions", {}),
        }

    def parse_instruction_embedding(self, text: str) -> tuple[str, list[dict]]:
        """
        Extracts [ACTION: ...] instructions from the text.
        Returns cleaned text and a list of action dictionaries.
        
        Example:
        Input: "Take this! [ACTION: ATTACK target=John]"
        Output: ("Take this!", [{"type": "ATTACK", "target": "John"}])
        """
        actions = []
        
        # Regex to find [ACTION: TYPE param=val ...]
        # Simple parser for now
        pattern = r"\[ACTION:\s*(\w+)(?:\s+(.*?))?\]"
        
        def replace_callback(match):
            action_type = str(match.group(1) or "").strip().upper()
            params_str = match.group(2) or ""
            
            params = {}
            if params_str:
                try:
                    pairs = shlex.split(params_str)
                except Exception:
                    pairs = params_str.split()
                for pair in pairs:
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k.strip()] = v.strip().strip("'\"")
                    else:
                        params[pair.strip()] = True
            
            actions.append({
                "type": action_type,
                **params
            })
            return "" # Remove the tag from text
            
        cleaned_text = re.sub(pattern, replace_callback, text)
        return cleaned_text.strip(), actions
