from __future__ import annotations

import json
import logging
import os
import ipaddress
import queue
import random
import re
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit

# 导入 LLM 客户端
try:
    from omnis_llm import OmnisLLMClient
except ImportError:
    try:
        from omnis.omnis_llm import OmnisLLMClient
    except ImportError:
        OmnisLLMClient = None

# 导入记忆模块
try:
    from memory import get_episodic_memory
except ImportError:
    try:
        from omnis.memory import get_episodic_memory
    except ImportError:
        get_episodic_memory = None

# 导入 Tavern 引擎（智能体复用玩家路径）
import sys
_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
try:
    from src.tavern.engine import TavernEngine
    from src.tavern.card_manager import CardManager
    from src.tavern.history import HistoryManager as TavernHistoryManager
except ImportError:
    TavernEngine = None
    CardManager = None
    TavernHistoryManager = None

try:
    from models import AgentState, ItemState
except ImportError:
    from omnis.models import AgentState, ItemState
try:
    from turn_engine import build_dm_scenario
except ImportError:
    from omnis.turn_engine import build_dm_scenario
try:
    from world_session import build_snapshot_row, trim_snapshot_rows
except ImportError:
    from omnis.world_session import build_snapshot_row, trim_snapshot_rows
try:
    from proxy_handler import build_health_etag
except ImportError:
    from omnis.proxy_handler import build_health_etag

logger = logging.getLogger("omnis.proxy")


class WorldSession:
    def __init__(
        self,
        session_id: str,
        tavern_user_dir: str = "",
        tavern_world_file: str = "",
        script_guard_enabled: bool = True,
        script_guard_action: str = "reduce",
        rule_template_file: str = "",
        llm_client: OmnisLLMClient | None = None,
        active_characters: list[str] | None = None,
        max_journal_limit: int = 3,
        dynamic_lore_writeback: bool = False,
        turn_min_interval_sec: float = 0.5,
        active_location_freeze_turns: int = 2,
        location_lexicon: list[str] | None = None,
        location_lexicon_file: str = "",
        strict_background_independent: bool = True,
        rumor_default_ttl_ticks: int = 48,
        rumor_ttl_by_llm: bool = True,
        max_agent_history: int = 40,
    ):
        self.session_id = session_id
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self._locations = ["未知地点"]
        self._intents = ["游荡", "休息", "观察", "沉思", "等待"]
        self._intent_enum_labels: dict[str, str] = {
            "wander": "游荡",
            "rest": "休息",
            "observe": "观察",
            "move": "转场",
            "intimate": "亲密互动",
            "chat": "交谈",
            "interact": "互动",
            "think": "沉思",
            "wait": "等待",
            "unknown": "正与玩家互动",
        }
        self._agents: list[AgentState] = []
        self._latest_digest = ""
        self._last_tick = 0
        self._source = "fallback"
        self._world_rule_summary = ""
        self._world_rule_map: dict[str, list[str]] = {}
        self._worldbook_entries: list[dict[str, Any]] = []
        self._card_profile_map: dict[str, dict[str, str]] = {}
        self._full_card_map: dict[str, dict[str, Any]] = {}  # 完整角色卡缓存
        self.max_agent_history = max(10, int(max_agent_history))

        # Tavern 引擎：让智能体走与玩家完全一致的 Prompt 路径
        self._tavern_engine = TavernEngine() if TavernEngine else None
        self._card_manager = CardManager() if CardManager else None
        self._tavern_user_dir = tavern_user_dir.strip()
        self._tavern_world_file = tavern_world_file.strip()
        self._script_guard_enabled = bool(script_guard_enabled)
        self._script_guard_action = script_guard_action if script_guard_action in {"reduce", "off"} else "reduce"
        self._rule_template_file = rule_template_file.strip()
        self._rule_templates = self._load_rule_templates()
        self.llm_client = llm_client
        self.dynamic_lore_writeback = dynamic_lore_writeback # 是否将新增的设定写回本地世界书 JSON
        self._active_hook: str = ""  # 存储等待注入的主动打断钩子
        self._active_characters = active_characters or [] # 当前酒馆正在聊天的角色，需从后台排除
        self._relationships: dict[str, dict[str, int]] = {} # 软关系：A -> B 的好感度 (-100 到 100)
        self._identities: dict[str, dict[str, str]] = {} # 硬关系拓扑：A 看 B 是什么身份 (例如: 师父, 仇人, 前女友)
        self._items: dict[str, ItemState] = {} # 物品状态追踪
        self.max_journal_limit = max_journal_limit
        self._possessed_character: str = "" # 玩家当前"魂穿"的后台角色名
        self._original_user_name: str = "" # 玩家原始设定的名字，用于被替换
        
        # 从玩家请求中捕获的预设消息，供智能体复用（含破限/写作指令等）
        self._captured_preset_messages: list[dict[str, str]] = []
        self._captured_llm_params: dict[str, Any] = {}

        # 世界时间与流言系统
        self._world_day: int = 1
        self._time_of_day: str = "清晨" # 清晨, 上午, 中午, 下午, 傍晚, 入夜, 深夜
        self._active_rumors: list[dict[str, Any]] = []
        self._rumor_default_ttl_ticks: int = max(6, min(240, int(rumor_default_ttl_ticks)))
        self._rumor_ttl_by_llm: bool = bool(rumor_ttl_by_llm)
        self._recent_injections: list[dict[str, Any]] = []
        self._recent_dm_adjudications: list[dict[str, Any]] = []
        self._turn_queue: queue.Queue[str] = queue.Queue(maxsize=16)
        self._last_enqueued_sig: str = ""
        self._last_processed_sig: str = ""
        self._last_enqueued_at: float = 0.0
        self._last_turn_finished_at: float = 0.0
        self._turn_inflight: bool = False
        self._turn_inflight_event = threading.Event()
        self._turn_gate_until: float = 0.0
        self._last_enqueue_report: dict[str, Any] = {"status": "idle", "reason": "", "queue_size": 0}
        self._turn_progress: dict[str, Any] = {
            "state": "idle",
            "total": 0,
            "done": 0,
            "pending": 0,
            "remaining_names": [],
            "failures": 0,
            "failed_names": [],
            "errors": [],
            "started_at": 0.0,
            "finished_at": 0.0,
            "tick": 0,
            "elapsed_ms": 0,
            "llm_call_count": 0,
            "dm_call_count": 0,
            "slowest_actor": "",
            "slowest_actor_ms": 0,
            "dm_elapsed_ms": 0,
        }
        self._agent_turn_fail_streak: dict[str, int] = {}
        self._agent_cooldown_until_tick: dict[str, int] = {}
        self._agent_last_turn_error: dict[str, str] = {}
        self._max_relationship_pairs: int = 50
        self._relationship_touch: dict[str, float] = {}
        self._max_items: int = 100
        self._item_touch: dict[str, float] = {}
        self._state_dirty: bool = False
        self._state_force_flush: bool = False
        self._state_last_flush_at: float = 0.0
        self._state_flush_interval_sec: float = 3.0
        self._state_flush_event = threading.Event()
        self._turn_min_interval_sec = max(0.0, float(turn_min_interval_sec))
        self._active_location_freeze_turns = max(0, int(active_location_freeze_turns))
        self._strict_background_independent = bool(strict_background_independent)
        self._active_location_freeze_until_tick: dict[str, int] = {}
        self._last_active_extract_report: dict[str, Any] = {}
        self._recent_extract_logs: list[dict[str, Any]] = []
        self._recent_debug_packets: list[dict[str, Any]] = []
        self._state_snapshots: list[dict[str, Any]] = []
        self._state_cursor_hash: str = ""
        self._active_micro_last_tick: dict[str, int] = {}
        self._active_micro_last_sig: dict[str, str] = {}
        self._active_story_last_sig: dict[str, str] = {}
        self._wait_scope_degrade_until: float = 0.0
        self._wait_timeout_marks: list[float] = []
        self._last_wait_effective_scope: str = "related"
        default_lexicon: list[str] = []
        self._location_lexicon_file = str(location_lexicon_file or "").strip()
        self._location_lexicon = self._load_location_lexicon(location_lexicon, default_lexicon)
        self._turn_worker = threading.Thread(target=self._turn_worker_loop, daemon=True)
        self._turn_worker.start()
        
        self._load_initial_entities()
        project_root = Path(__file__).resolve().parents[1]
        state_dir = project_root / "storage" / "run_records"
        state_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", self.session_id) or "default"
        self._history_dir = state_dir
        self._safe_session_id = safe_id
        self._state_file = state_dir / f"omnis_state_{safe_id}.json"
        self._load_state_store()
        self._state_flush_worker = threading.Thread(target=self._state_flush_loop, daemon=True)
        self._state_flush_worker.start()

    def update_world_time(self) -> None:
        """根据 _last_tick 换算世界历法时间，假设 1 Tick = 1小时，24 Tick = 1天"""
        total_hours = self._last_tick
        self._world_day = (total_hours // 24) + 1
        hour_of_day = total_hours % 24
        
        if 5 <= hour_of_day < 8:
            self._time_of_day = "清晨"
        elif 8 <= hour_of_day < 11:
            self._time_of_day = "上午"
        elif 11 <= hour_of_day < 14:
            self._time_of_day = "中午"
        elif 14 <= hour_of_day < 17:
            self._time_of_day = "下午"
        elif 17 <= hour_of_day < 19:
            self._time_of_day = "傍晚"
        elif 19 <= hour_of_day < 23:
            self._time_of_day = "入夜"
        else:
            self._time_of_day = "深夜"

    def get_world_time_str(self) -> str:
        return f"第 {self._world_day} 天，{self._time_of_day}"

    def _prune_expired_rumors(self) -> None:
        with self._lock:
            now_tick = int(self._last_tick)
            kept: list[dict[str, Any]] = []
            for row in self._active_rumors:
                if not isinstance(row, dict):
                    continue
                exp_tick = int(row.get("expire_at_tick", now_tick + self._rumor_default_ttl_ticks))
                text = str(row.get("text", "")).strip()
                if not text:
                    continue
                if now_tick >= exp_tick:
                    continue
                kept.append(row)
            self._active_rumors = kept[-10:]

    def _rumor_texts(self) -> list[str]:
        self._prune_expired_rumors()
        with self._lock:
            return [str(x.get("text", "")).strip() for x in self._active_rumors if isinstance(x, dict) and str(x.get("text", "")).strip()]

    def _append_rumor(self, raw_row: Any, allow_model_ttl: bool = True) -> None:
        text = ""
        ttl_ticks = self._rumor_default_ttl_ticks
        if isinstance(raw_row, dict):
            text = str(raw_row.get("text", "")).strip()
            if allow_model_ttl:
                try:
                    d = int(raw_row.get("expire_days", 0) or 0)
                    if d > 0:
                        ttl_ticks = max(6, min(240, d * 24))
                except Exception:
                    pass
                try:
                    t = int(raw_row.get("ttl_ticks", 0) or 0)
                    if t > 0:
                        ttl_ticks = max(6, min(240, t))
                except Exception:
                    pass
        else:
            text = str(raw_row or "").strip()
        if not text:
            return
        if allow_model_ttl:
            m_day = re.search(r"(?:持续|寿命|expire_days)\s*[:：=]?\s*(\d+)\s*天", text, re.IGNORECASE)
            if m_day:
                ttl_ticks = max(6, min(240, int(m_day.group(1)) * 24))
                text = re.sub(r"(?:持续|寿命|expire_days)\s*[:：=]?\s*\d+\s*天", "", text, flags=re.IGNORECASE).strip(" ;；，,")
            m_tick = re.search(r"(?:ttl|ticks?|持续)\s*[:：=]?\s*(\d+)\s*(?:tick|ticks|回合)?", text, re.IGNORECASE)
            if m_tick:
                ttl_ticks = max(6, min(240, int(m_tick.group(1))))
                text = re.sub(r"(?:ttl|ticks?|持续)\s*[:：=]?\s*\d+\s*(?:tick|ticks|回合)?", "", text, flags=re.IGNORECASE).strip(" ;；，,")
        if not text:
            return
        with self._lock:
            now_tick = int(self._last_tick)
            self._active_rumors.append(
                {
                    "text": text,
                    "created_at_tick": now_tick,
                    "expire_at_tick": now_tick + int(ttl_ticks),
                }
            )
        self._prune_expired_rumors()

    def _record_injection(self, source: str, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        with self._lock:
            row = {
                "tick": int(self._last_tick),
                "world_time": self.get_world_time_str(),
                "source": str(source or "").strip() or "unknown",
                "content": text[:1600],
            }
            self._recent_injections.append(row)
            if len(self._recent_injections) > 12:
                self._recent_injections = self._recent_injections[-12:]

    def _record_dm_adjudication(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._recent_dm_adjudications.append(payload)
            if len(self._recent_dm_adjudications) > 12:
                self._recent_dm_adjudications = self._recent_dm_adjudications[-12:]

    def _agent_history_file(self, agent_name: str) -> Path:
        safe_agent = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(agent_name or "")).strip("._-") or "unknown"
        return self._history_dir / f"agent_history_{self._safe_session_id}_{safe_agent}.json"

    def _load_agent_history_rows(self, agent_name: str) -> list[dict[str, Any]]:
        fp = self._agent_history_file(agent_name)
        if not fp.exists():
            return []
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(payload, dict):
            rows = payload.get("history", [])
        else:
            rows = payload
        if not isinstance(rows, list):
            return []
        return [x for x in rows if isinstance(x, dict)]

    def _save_agent_history_rows(self, agent_name: str, rows: list[dict[str, Any]]) -> None:
        fp = self._agent_history_file(agent_name)
        payload = {
            "session_id": self.session_id,
            "agent_name": str(agent_name),
            "history": rows[-self.max_agent_history:],
        }
        self._atomic_write_json(fp, payload, indent=2)

    def _get_agent_history_manager(self, agent_name: str, load_if_empty: bool = True):
        if not self._tavern_engine:
            return None
        sid = f"agent_{agent_name}"
        hm = self._tavern_engine.get_history_manager(sid)
        if load_if_empty and not getattr(hm, "history", None):
            rows = self._load_agent_history_rows(agent_name)
            if rows:
                hm.from_list(rows)
        return hm

    def _persist_agent_history(self, agent_name: str) -> None:
        if not self._tavern_engine:
            return
        hm = self._get_agent_history_manager(agent_name, load_if_empty=False)
        if not hm:
            return
        rows = hm.to_list()[-self.max_agent_history:]
        self._save_agent_history_rows(agent_name, rows)

    def _clear_agent_history_runtime(self, agent_name: str) -> None:
        if self._tavern_engine and hasattr(self._tavern_engine, "histories"):
            try:
                self._tavern_engine.histories.pop(f"agent_{agent_name}", None)
            except Exception:
                pass
        fp = self._agent_history_file(agent_name)
        try:
            if fp.exists():
                fp.unlink()
        except Exception:
            pass

    def _evict_agent_history_manager(self, agent_name: str) -> None:
        if self._tavern_engine and hasattr(self._tavern_engine, "histories"):
            try:
                self._tavern_engine.histories.pop(f"agent_{agent_name}", None)
            except Exception:
                pass

    @staticmethod
    def _atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=indent)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))

    def flush_state_store_now(self) -> None:
        with self._state_lock:
            self._state_dirty = False
            self._state_force_flush = False
        self._save_state_store()

    def _context_hash(self, recent_context: str) -> str:
        return str(hash(str(recent_context or "").strip()[-1200:]))

    def _state_payload(self) -> dict[str, Any]:
        agents = []
        for a in self._agents:
            agents.append(
                {
                    "name": a.name,
                    "location": a.location,
                    "intent": a.intent,
                    "salience": int(a.salience),
                    "updated_at": float(a.updated_at),
                    "journal": list(a.journal),
                    "status": a.status,
                    "ai_enabled": bool(a.ai_enabled),
                    "summary": a.summary,
                    "card_description": a.card_description,
                    "card_personality": a.card_personality,
                    "card_scenario": a.card_scenario,
                    "full_card": a.full_card,
                }
            )
        return {
            "tick": int(self._last_tick),
            "world_day": int(self._world_day),
            "time_of_day": str(self._time_of_day),
            "latest_digest": str(self._latest_digest),
            "active_characters": list(self._active_characters),
            "locations": list(self._locations),
            "relationships": self._relationships,
            "identities": self._identities,
            "items": {k: {"name": v.name, "owner": v.owner, "location": v.location} for k, v in self._items.items()},
            "active_rumors": list(self._active_rumors),
            "agents": agents,
            "last_extract_report": self._last_active_extract_report,
        }

    def _restore_state_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        self._last_tick = int(payload.get("tick", self._last_tick))
        self._world_day = int(payload.get("world_day", self._world_day))
        self._time_of_day = str(payload.get("time_of_day", self._time_of_day))
        self._latest_digest = str(payload.get("latest_digest", self._latest_digest))
        # 不从快照恢复 active_characters（应由每次请求动态确定，避免旧数据污染）
        # self._active_characters 保持初始化时的空列表
        base_allowed = {str(x).strip() for x in self._locations if str(x).strip()}
        locations = [str(x) for x in payload.get("locations", []) if str(x).strip()]
        if base_allowed:
            locations = [x for x in locations if x in base_allowed]
        if locations:
            self._locations = locations
        rel = payload.get("relationships", {})
        self._relationships = rel if isinstance(rel, dict) else {}
        self._relationship_touch = {}
        for src, targets in self._relationships.items():
            if not isinstance(targets, dict):
                continue
            for tgt, score in targets.items():
                if isinstance(score, (int, float)) and int(score) != 0:
                    self._relationship_touch[f"{src}::{tgt}"] = time.time()
        ids = payload.get("identities", {})
        self._identities = ids if isinstance(ids, dict) else {}
        self._active_rumors = []
        for x in payload.get("active_rumors", []):
            if isinstance(x, dict):
                text = str(x.get("text", "")).strip()
                if not text:
                    continue
                created_at_tick = int(x.get("created_at_tick", self._last_tick))
                expire_at_tick = int(x.get("expire_at_tick", created_at_tick + self._rumor_default_ttl_ticks))
                self._active_rumors.append(
                    {
                        "text": text,
                        "created_at_tick": created_at_tick,
                        "expire_at_tick": expire_at_tick,
                    }
                )
            elif str(x).strip():
                self._active_rumors.append(
                    {
                        "text": str(x).strip(),
                        "created_at_tick": int(self._last_tick),
                        "expire_at_tick": int(self._last_tick) + self._rumor_default_ttl_ticks,
                    }
                )
        self._prune_expired_rumors()
        items_payload = payload.get("items", {})
        self._items = {}
        self._item_touch = {}
        if isinstance(items_payload, dict):
            for k, row in items_payload.items():
                if not isinstance(row, dict):
                    continue
                self._items[str(k)] = ItemState(
                    name=str(row.get("name", k)),
                    owner=str(row.get("owner", "")),
                    location=str(row.get("location", "")),
                )
                self._item_touch[str(k)] = time.time()
        self._prune_relationships()
        self._prune_items()
        agents_payload = payload.get("agents", [])
        restored_agents: list[AgentState] = []
        if isinstance(agents_payload, list):
            for row in agents_payload:
                if not isinstance(row, dict):
                    continue
                loc = str(row.get("location", "未知地点")).strip() or "未知地点"
                if base_allowed and loc not in base_allowed and loc not in {"当前场景", "未知地点"}:
                    loc = "当前场景"
                restored_agents.append(
                    AgentState(
                        name=str(row.get("name", "")),
                        location=loc,
                        intent=str(row.get("intent", "待机中")),
                        salience=int(row.get("salience", 0)),
                        updated_at=float(row.get("updated_at", 0.0)),
                        journal=[str(x) for x in row.get("journal", []) if str(x).strip()
                                 and "<content>" not in x and "<thinking>" not in x
                                 and "并留意到制的正文" not in x and "以<thinking>作为开头" not in x
                                 and "来源:heuristic" not in x and "来源:social" not in x],
                        status=str(row.get("status", "alive")),
                        ai_enabled=bool(row.get("ai_enabled", True)),
                        summary=str(row.get("summary", "暂无简介")),
                        card_description=str(row.get("card_description", "")),
                        card_personality=str(row.get("card_personality", "")),
                        card_scenario=str(row.get("card_scenario", "")),
                        full_card=row.get("full_card"),
                        chat_history_data=[],
                    )
                )
        if restored_agents:
            self._agents = restored_agents
        last_report = payload.get("last_extract_report", {})
        if isinstance(last_report, dict):
            self._last_active_extract_report = last_report
        self.update_world_time()

    def _save_state_store(self) -> None:
        if not hasattr(self, "_state_file"):
            return
        with self._state_lock:
            payload = {
                "session_id": self.session_id,
                "cursor_hash": self._state_cursor_hash,
                "snapshots": self._state_snapshots[-80:],
            }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            tmp = self._state_file.with_suffix(f"{self._state_file.suffix}.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._state_file))
            with self._state_lock:
                self._state_last_flush_at = time.time()
        except Exception as e:
            logger.error(f"state_store_save_failed: {e}")

    def _mark_state_store_dirty(self, force: bool = False) -> None:
        if not hasattr(self, "_state_file"):
            return
        with self._state_lock:
            self._state_dirty = True
            if force:
                self._state_force_flush = True
        self._state_flush_event.set()

    def _state_flush_loop(self) -> None:
        while True:
            self._state_flush_event.wait(timeout=0.5)
            self._state_flush_event.clear()
            should_flush = False
            with self._state_lock:
                if self._state_dirty:
                    now = time.time()
                    elapsed = now - float(self._state_last_flush_at)
                    if self._state_force_flush or elapsed >= float(self._state_flush_interval_sec):
                        self._state_dirty = False
                        self._state_force_flush = False
                        should_flush = True
            if should_flush:
                self._save_state_store()

    def _load_state_store(self) -> None:
        if not hasattr(self, "_state_file") or not self._state_file.exists():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        rows = payload.get("snapshots", [])
        if not isinstance(rows, list):
            return
        snapshots = [x for x in rows if isinstance(x, dict) and isinstance(x.get("state"), dict)]
        # 统一锁顺序：先 _lock 再 _state_lock，避免与 save_turn_snapshot 形成 ABBA 死锁
        with self._lock:
            with self._state_lock:
                self._state_snapshots = snapshots[-80:]
                self._state_cursor_hash = str(payload.get("cursor_hash", "")).strip()
                latest = self._state_snapshots[-1] if self._state_snapshots else None
            if latest and isinstance(latest.get("state"), dict):
                self._restore_state_payload(latest["state"])
                with self._state_lock:
                    self._state_cursor_hash = str(latest.get("context_hash", self._state_cursor_hash))
                    self._state_last_flush_at = time.time()
            else:
                with self._state_lock:
                    self._state_last_flush_at = time.time()

    def save_turn_snapshot(self, recent_context: str, reason: str = "turn") -> None:
        ctx_hash = self._context_hash(recent_context)
        with self._lock:
            state = self._state_payload()
            tick = int(self._last_tick)
        row = build_snapshot_row(ctx_hash, tick, str(reason), recent_context, state)
        with self._state_lock:
            self._state_snapshots = [x for x in self._state_snapshots if str(x.get("context_hash", "")) != ctx_hash]
            self._state_snapshots.append(row)
            self._state_snapshots = trim_snapshot_rows(self._state_snapshots, 80)
            self._state_cursor_hash = ctx_hash
        self._mark_state_store_dirty(force=False)

    def restore_by_context(self, recent_context: str) -> dict[str, Any]:
        ctx_hash = self._context_hash(recent_context)
        with self._state_lock:
            if self._state_cursor_hash and ctx_hash == self._state_cursor_hash:
                return {"status": "same_cursor", "context_hash": ctx_hash}
            target = None
            for row in reversed(self._state_snapshots):
                if str(row.get("context_hash", "")) == ctx_hash:
                    target = row
                    break
        if target and isinstance(target.get("state"), dict):
            with self._lock:
                self._restore_state_payload(target["state"])
                tick = int(self._last_tick)
            with self._state_lock:
                self._state_cursor_hash = ctx_hash
            self._mark_state_store_dirty(force=False)
            return {"status": "restored", "context_hash": ctx_hash, "tick": tick}
        return {"status": "new_context", "context_hash": ctx_hash}

    def _turn_worker_loop(self) -> None:
        while True:
            ctx = self._turn_queue.get()
            try:
                with self._queue_lock:
                    self._turn_inflight = True
                    self._turn_inflight_event.set()
                with self._lock:
                    self._turn_progress.update(
                        {
                            "state": "running",
                            "total": 0,
                            "done": 0,
                            "pending": 0,
                            "remaining_names": [],
                            "failures": 0,
                            "failed_names": [],
                            "errors": [],
                            "started_at": time.time(),
                            "finished_at": 0.0,
                            "tick": int(self._last_tick),
                            "elapsed_ms": 0,
                            "llm_call_count": 0,
                            "dm_call_count": 0,
                            "slowest_actor": "",
                            "slowest_actor_ms": 0,
                            "dm_elapsed_ms": 0,
                        }
                    )
                sig = str(hash(ctx[-1200:]))
                self._last_processed_sig = sig
                self.run_turn(ctx)
            except Exception as e:
                logger.error(f"turn_worker_error: {e}")
            finally:
                with self._queue_lock:
                    self._turn_inflight = False
                    self._turn_inflight_event.clear()
                    queue_size_now = int(self._turn_queue.qsize())
                self._last_turn_finished_at = time.time()
                with self._lock:
                    self._turn_progress["state"] = "idle"
                    self._turn_progress["finished_at"] = float(self._last_turn_finished_at)
                    self._turn_progress["pending"] = queue_size_now
                    self._turn_progress["remaining_names"] = []
                self._turn_queue.task_done()

    def enqueue_turn(self, recent_context: str) -> None:
        ctx = (recent_context or "").strip()
        if not ctx:
            with self._queue_lock:
                self._last_enqueue_report = {"status": "skipped", "reason": "empty_context", "queue_size": int(self._turn_queue.qsize())}
            return
        now = time.time()
        with self._queue_lock:
            if self._turn_min_interval_sec > 0 and now < self._turn_gate_until:
                self._last_enqueue_report = {"status": "throttled", "reason": "gate_window", "queue_size": int(self._turn_queue.qsize())}
                return
            if self._turn_min_interval_sec > 0 and (now - self._last_enqueued_at) < self._turn_min_interval_sec:
                self._last_enqueue_report = {"status": "throttled", "reason": "min_interval", "queue_size": int(self._turn_queue.qsize())}
                return
            if self._turn_inflight and int(self._turn_queue.qsize()) >= 1:
                self._last_enqueue_report = {"status": "throttled", "reason": "inflight_backpressure", "queue_size": int(self._turn_queue.qsize())}
                return
            sig = str(hash(ctx[-1200:]))
            if sig == self._last_enqueued_sig or sig == self._last_processed_sig:
                self._last_enqueue_report = {"status": "deduped", "reason": "same_signature", "queue_size": int(self._turn_queue.qsize())}
                return
            self._last_enqueued_sig = sig
            self._last_enqueued_at = now
            self._turn_gate_until = now + self._turn_min_interval_sec
            try:
                self._turn_queue.put_nowait(ctx)
                self._last_enqueue_report = {"status": "enqueued", "reason": "ok", "queue_size": int(self._turn_queue.qsize())}
            except Exception:
                try:
                    _ = self._turn_queue.get_nowait()
                    self._turn_queue.task_done()
                except Exception:
                    pass
                try:
                    self._turn_queue.put_nowait(ctx)
                    self._last_enqueue_report = {"status": "enqueued", "reason": "drop_oldest", "queue_size": int(self._turn_queue.qsize())}
                except Exception:
                    self._last_enqueue_report = {"status": "dropped", "reason": "queue_full", "queue_size": int(self._turn_queue.qsize())}

    def wait_until_turn_idle(self, timeout_sec: float = 8.0) -> dict[str, Any]:
        timeout = max(0.0, float(timeout_sec))
        start = time.time()
        while True:
            with self._queue_lock:
                inflight = bool(self._turn_inflight)
                qsize = int(self._turn_queue.qsize())
            if (not inflight) and qsize <= 0:
                return {"ok": True, "waited_sec": round(time.time() - start, 3), "timeout_sec": timeout}
            if time.time() - start >= timeout:
                return {"ok": False, "waited_sec": round(time.time() - start, 3), "timeout_sec": timeout}
            time.sleep(0.05)

    def wait_until_related_ready(self, active_names: list[str], timeout_sec: float = 3.0) -> dict[str, Any]:
        names = [str(x).strip() for x in (active_names or []) if str(x).strip()]
        if not names:
            return {"ok": True, "waited_sec": 0.0, "timeout_sec": max(0.0, float(timeout_sec))}
        timeout = max(0.0, float(timeout_sec))
        start = time.time()
        while True:
            with self._queue_lock:
                ready = not self._turn_inflight
            if ready:
                return {"ok": True, "waited_sec": round(time.time() - start, 3), "timeout_sec": timeout}
            if time.time() - start >= timeout:
                return {"ok": False, "waited_sec": round(time.time() - start, 3), "timeout_sec": timeout}
            time.sleep(0.05)

    def resolve_wait_scope(self, configured_scope: str = "related", auto_downgrade: bool = True) -> str:
        scope = "all" if str(configured_scope).strip().lower() == "all" else "related"
        if scope != "all":
            self._last_wait_effective_scope = "related"
            return "related"
        now = time.time()
        if not auto_downgrade:
            self._last_wait_effective_scope = "all"
            return "all"
        if now < float(self._wait_scope_degrade_until):
            self._last_wait_effective_scope = "related"
            return "related"
        with self._queue_lock:
            qsize = int(self._turn_queue.qsize())
            inflight = bool(self._turn_inflight)
        if qsize > 0 or inflight:
            self._wait_scope_degrade_until = now + 15.0
            self._last_wait_effective_scope = "related"
            return "related"
        self._last_wait_effective_scope = "all"
        return "all"

    def mark_wait_result(self, ok: bool, effective_scope: str, timeout_sec: float) -> None:
        if str(effective_scope) != "all":
            return
        now = time.time()
        if ok:
            self._wait_timeout_marks = [x for x in self._wait_timeout_marks if now - float(x) <= 30.0]
            return
        self._wait_timeout_marks = [x for x in self._wait_timeout_marks if now - float(x) <= 30.0]
        self._wait_timeout_marks.append(now)
        if len(self._wait_timeout_marks) >= 2:
            self._wait_scope_degrade_until = now + max(12.0, float(timeout_sec) * 2.0)

    def ensure_tavern_binding(self, preferred_user_dir: str = "", preferred_world_file: str = "") -> bool:
        with self._lock:
            user_dir_str = str(preferred_user_dir or self._tavern_user_dir or "").strip()
            world_file_str = str(preferred_world_file or self._tavern_world_file or "").strip()
            user_dir = Path(user_dir_str) if user_dir_str else self._default_tavern_user_dir()
            world_file = Path(world_file_str) if world_file_str else self._guess_world_file(user_dir)
            old_user = str(self._tavern_user_dir or "")
            old_world = str(self._tavern_world_file or "")
            need_reload = (self._source in {"fallback", "tavern_cards_only"}) or (not user_dir.exists()) or (not world_file.exists()) or old_user != str(user_dir) or old_world != str(world_file)
            if not need_reload:
                return False
            self._tavern_user_dir = str(user_dir) if user_dir else ""
            self._tavern_world_file = str(world_file) if world_file else ""
            self._load_initial_entities()
            return True

    def _load_location_lexicon(self, location_lexicon: list[str] | None, default_lexicon: list[str]) -> list[str]:
        explicit_list = isinstance(location_lexicon, list)
        lexicon: list[str] = []
        for x in location_lexicon or []:
            if isinstance(x, str) and x.strip():
                lexicon.append(x.strip())
        if self._location_lexicon_file and not explicit_list:
            try:
                fp = Path(self._location_lexicon_file)
                if not fp.is_absolute() and not fp.exists():
                    fp = Path(__file__).resolve().parent.parent / self._location_lexicon_file
                if not fp.exists():
                    alt = Path(__file__).resolve().parent / self._location_lexicon_file
                    if alt.exists():
                        fp = alt
                if fp.exists() and fp.is_file():
                    payload = json.loads(fp.read_text(encoding="utf-8"))
                    raw_terms: list[str] = []
                    if isinstance(payload, list):
                        raw_terms = [str(x).strip() for x in payload]
                    elif isinstance(payload, dict):
                        values = payload.get("locations", [])
                        if isinstance(values, list):
                            raw_terms = [str(x).strip() for x in values]
                    for term in raw_terms:
                        if term:
                            lexicon.append(term)
            except Exception as e:
                logger.warning(f"location_lexicon_file_load_failed: {e}")
        if not lexicon and location_lexicon is None:
            lexicon = list(default_lexicon)
        unique: list[str] = []
        for item in lexicon:
            if item and item not in unique:
                unique.append(item)
        return unique

    def _normalize_intent(self, raw_intent: str, action: str = "") -> dict[str, str]:
        text = str(raw_intent or "").strip().lower()
        act = str(action or "").strip().lower()
        pairs = [
            (("洗澡", "沐浴", "淋浴", "休息", "睡觉", "躺"), "rest"),
            (("观察", "打量", "窥视"), "observe"),
            (("来到", "走进", "进入", "回到", "去", "到", "离开", "走出", "转场"), "move"),
            (("拥抱", "亲吻", "爱抚", "亲密"), "intimate"),
            (("交谈", "说话", "对话"), "chat"),
            (("倒水", "泡茶", "端茶", "互动"), "interact"),
            (("沉思", "思考"), "think"),
            (("等待", "旁观"), "wait"),
            (("游荡", "闲逛"), "wander"),
        ]
        intent_type = "unknown"
        for kws, tp in pairs:
            if any(k in text for k in kws) or any(k in act for k in kws):
                intent_type = tp
                break
        return {"type": intent_type, "label": self._intent_enum_labels.get(intent_type, self._intent_enum_labels["unknown"])}

    def _build_active_micro_line(self, name: str, location: str, intent: str) -> str:
        thought_map = {
            "转场": "得尽快确认周围局势",
            "观察": "先把局势摸清再行动",
            "交谈": "保持语气稳定更容易获得信息",
            "互动": "顺势推进关系会更自然",
            "休息": "先调整状态再继续",
            "沉思": "需要再验证一个关键细节",
            "等待": "先观察对方反应",
            "亲密互动": "要把握分寸避免失控",
            "游荡": "先踩点再决定下一步",
            "正与玩家互动": "先保持对话连贯",
        }
        thought = thought_map.get(str(intent).strip(), "先稳住节奏再判断")
        return f"[Tick {self._last_tick} 侧写] {name}在{location}，{intent}，心里想着：{thought}。"

    def _build_active_brief_line(self, name: str, location: str, intent: str, source: str, recent_context: str) -> str:
        action_map = {
            "转场": "调整脚步并观察路线",
            "观察": "先观察周围人的反应",
            "交谈": "保持对话并试探关键信息",
            "互动": "顺势推进互动节奏",
            "休息": "短暂停顿恢复状态",
            "沉思": "回想线索并重排判断",
            "等待": "放缓动作等待信号",
            "亲密互动": "克制表达并确认边界",
            "游荡": "在周边缓慢移动",
            "正与玩家互动": "围绕玩家话题继续回应",
        }
        thought_map = {
            "转场": "先稳住位置再决定下一步",
            "观察": "不急着下结论，先收集细节",
            "交谈": "需要从语气里判断真实意图",
            "互动": "把握分寸才能不失控",
            "休息": "状态稳定后再处理问题",
            "沉思": "关键矛盾还需要验证",
            "等待": "让信息自己浮出来",
            "亲密互动": "先确认对方是否接受",
            "游荡": "先踩点再推进目标",
            "正与玩家互动": "优先保证对话连贯和角色一致",
        }
        loc = str(location or "").strip() or "当前场景"
        if loc in {"未知地点", "当前场景"}:
            loc_text = "我在这片区域里"
        else:
            loc_text = f"我在{loc}"
        act = action_map.get(intent, "维持当前节奏")
        thought = thought_map.get(intent, "先观察再行动")
        # 不再从 recent_context 取线索（会泄露系统提示词），改为简洁概括
        return f"[Tick {self._last_tick} 回合] {loc_text}，{act}。（{intent}，来源:{source}）"

    def _normalize_unknown_location_narrative(self, text: str, recent_context: str = "") -> str:
        out = str(text or "").strip()
        if not out:
            return ""
        allow_literal = "未知地点" in str(recent_context or "")
        if not allow_literal:
            out = out.replace("离开未知地点", "离开原先的位置")
            out = out.replace("在未知地点", "在附近")
            out = out.replace("未知地点", "附近")
            out = out.replace("这个陌生的地方", "这片区域")
        return out[:500]

    def _generate_active_story(self, agent: AgentState, location: str, intent: str, recent_context: str) -> str:
        """对话角色日志：从酒馆对话中归纳总结，不生成新内容"""
        if not (hasattr(self, "llm_client") and self.llm_client):
            return ""
        ctx_tail = str(recent_context or "").strip()
        if not ctx_tail:
            return ""
        # 只取对话中实际内容的最后 1500 字
        ctx_tail = ctx_tail[-1500:]
        prompt = (
            f"以下是酒馆中最近发生的对话/叙事记录，其中包含角色[{agent.name}]的言行：\n"
            f"---\n{ctx_tail}\n---\n\n"
            f"请用第三人称、一段话归纳[{agent.name}]在以上记录中做了什么、说了什么。\n"
            f"【严格要求】\n"
            f"1. 只能归纳以上记录中已有的内容，绝对禁止编造记录中没有的情节、对话或心理活动。\n"
            f"2. 如果记录中没有{agent.name}的明确行动，就写'（本轮无明确行动）'。\n"
            f"3. 100字以内，纯文本。"
        )
        result = self._agent_llm_caller(prompt)
        if result and not result.startswith("（"):
            return result[:300]
        return ""

    def _finalize_active_story(self, dialog_context: str, response_text: str) -> None:
        """后台异步：合并玩家输入 + 模型回复 → 归纳对话角色日志"""
        full_context = dialog_context.strip()
        if response_text.strip():
            full_context += f"\n【角色】: {response_text.strip()[:1500]}"
        if not full_context:
            return

        with self._lock:
            char_names = list(self._active_characters)
            agents_map = {a.name: a for a in self._agents if a.name in char_names}
            saved_tick = int(self._last_tick)

        results: dict[str, str] = {}
        _story_debug: list[dict] = []
        for name, ag in agents_map.items():
            story = self._generate_active_story(
                ag, ag.location or "当前场景", ag.intent or "互动", full_context
            )
            _dbg = {"name": name, "story_result": str(story)[:100] if story else "(empty)"}
            if story:
                results[name] = story
            _story_debug.append(_dbg)
        self._last_active_story_debug = _story_debug

        with self._lock:
            for name, story in results.items():
                ag = next((a for a in self._agents if a.name == name), None)
                if not ag:
                    continue
                clean_story = self._normalize_unknown_location_narrative(story, full_context)
                entry = f"[Tick {saved_tick}] {clean_story}"
                if not ag.journal or ag.journal[-1] != entry:
                    ag.journal.append(entry)
                    if len(ag.journal) > 20:
                        ag.journal.pop(0)

    def _build_state_snapshot(self, names: list[str]) -> dict[str, dict[str, str]]:
        snap: dict[str, dict[str, str]] = {}
        for a in self._agents:
            if a.name in names:
                snap[a.name] = {"location": a.location, "intent": a.intent, "status": a.status}
        return snap

    def _push_extract_log(self, report: dict[str, Any]) -> None:
        row = {
            "tick": int(self._last_tick),
            "decision_reason": report.get("decision_reason", ""),
            "override_source": report.get("override_source", ""),
            "resolved": report.get("resolved", []),
            "state_diff": report.get("state_diff", []),
        }
        self._recent_extract_logs.append(row)
        if len(self._recent_extract_logs) > 5:
            self._recent_extract_logs = self._recent_extract_logs[-5:]

    def _push_debug_packet(self, recent_context: str, report: dict[str, Any], final_state: dict[str, Any]) -> None:
        raw_ctx = str(recent_context or "")
        packet = {
            "tick": int(self._last_tick),
            "ts": int(time.time()),
            "context": raw_ctx[-2400:],
            "context_hash": str(hash(raw_ctx[-1200:])),
            "queue_size": int(self._turn_queue.qsize()),
            "enqueue_report": dict(self._last_enqueue_report),
            "extract_report": report,
            "final_state": final_state,
        }
        self._recent_debug_packets.append(packet)
        if len(self._recent_debug_packets) > 30:
            self._recent_debug_packets = self._recent_debug_packets[-30:]

    def build_debug_bundle(self, limit: int = 10) -> dict[str, Any]:
        n = max(1, min(50, int(limit)))
        packets = self._recent_debug_packets[-n:]
        return {
            "session_id": self.session_id,
            "tick": int(self._last_tick),
            "world_time": self.get_world_time_str(),
            "exported_at": int(time.time()),
            "count": len(packets),
            "active_characters": list(self._active_characters),
            "intent_enum_labels": dict(self._intent_enum_labels),
            "enqueue_report": dict(self._last_enqueue_report),
            "recent_extract_logs": list(self._recent_extract_logs),
            "packets": packets,
            "latest_report": self._last_active_extract_report,
        }

    def _is_same_scene_candidate(self, name: str, recent_context: str) -> bool:
        nm = str(name or "").strip()
        if not nm:
            return False
        text = str(recent_context or "")
        mention_negative = ["提到", "说起", "回忆", "电话", "听说", "传闻", "聊到", "想起"]
        if any(x in text for x in mention_negative):
            nearby = re.findall(rf"{re.escape(nm)}[^。！？\n]*", text)
            if nearby and all(any(x in seg for x in mention_negative) for seg in nearby):
                return False
        scene_verbs = r"(来到|走进|进入|回到|站在|坐在|正站在|正坐在|出现在)"
        loc_terms = "|".join(sorted((re.escape(x) for x in self._location_lexicon), key=len, reverse=True))
        if loc_terms:
            p = rf"{re.escape(nm)}[^。！？\n]*?{scene_verbs}[^。！？\n]*?(?:{loc_terms})"
            if re.search(p, text):
                return True
        return bool(re.search(rf"{re.escape(nm)}[^。！？\n]*?{scene_verbs}", text))

    def _has_location_migration_verb(self, name: str, recent_context: str, target_loc: str) -> bool:
        text = str(recent_context or "")
        nm = str(name or "").strip()
        tl = str(target_loc or "").strip()
        if not nm or not tl:
            return False
        move_verbs = r"(来到|走进|进入|回到|去|到)"
        p1 = rf"{re.escape(nm)}[^。！？\n]*?{move_verbs}[^。！？\n]*?{re.escape(tl)}"
        p2 = rf"{move_verbs}\s*{re.escape(tl)}"
        return bool(re.search(p1, text) or re.search(p2, text))

    def _allow_social_focus(self, agent: AgentState, recent_context: str, nearby_agents: list[AgentState]) -> bool:
        if bool(getattr(self, "_strict_background_independent", True)):
            text = str(recent_context or "")
            if not text:
                return False
            if agent.name and agent.name not in text:
                return False
            paired = False
            for na in nearby_agents[:6]:
                if na.name and na.name in text:
                    paired = True
                    break
            if not paired:
                return False
        if not nearby_agents:
            return False
        if str(agent.location or "").strip() in {"未知地点", "当前场景"}:
            return False
        intent = str(agent.intent or "")
        if any(k in intent for k in ["交谈", "互动", "亲密"]):
            return True
        text = str(recent_context or "")
        if not text:
            return False
        if agent.name and agent.name in text:
            for na in nearby_agents[:6]:
                if na.name and na.name in text:
                    return True
        return False

    def _allow_relationship_change(self, src: str, tgt: str, delta: int, dm_result: str, scenario: str) -> bool:
        if abs(int(delta)) < 2:
            return False
        text = f"{str(dm_result or '')}。{str(scenario or '')}"
        interactive_words = ["交谈", "争执", "冲突", "攻击", "帮助", "救", "拥抱", "亲吻", "交易", "合作", "威胁", "对峙", "侮辱"]
        if src and tgt:
            pair_pattern = rf"{re.escape(src)}[^。！？\n]*{re.escape(tgt)}|{re.escape(tgt)}[^。！？\n]*{re.escape(src)}"
            if re.search(pair_pattern, text) and any(w in text for w in interactive_words):
                return True
        return False

    def _estimate_elapsed_ticks(self, context_text: str) -> int:
        fallback = 1
        msg_lower = str(context_text or "").lower()
        if any(w in msg_lower for w in ["睡", "休息", "天亮", "明天", "过了一天", "几个小时", "半天"]):
            fallback = 4
        elif any(w in msg_lower for w in ["几天", "一周", "很久", "旅途", "长途跋涉", "几个月"]):
            fallback = 10
        if not (hasattr(self, "llm_client") and self.llm_client):
            return fallback
        sys_prompt = (
            "你是世界时间裁决器。根据玩家这条消息实际会消耗的世界时间，返回JSON："
            "{'time_elapsed_ticks':整数}。1 tick=1小时。"
            "短动作通常1，短对话1-2，长时间休息4，跨日或旅途8-24。只返回JSON。"
        )
        res = self.llm_client.adjudicate_interaction_structured(sys_prompt, context_text)
        if not isinstance(res, dict):
            return fallback
        try:
            v = int(res.get("time_elapsed_ticks", fallback))
        except Exception:
            return fallback
        return max(1, min(24, v))

    def _resolve_agent_profile(self, agent_name: str) -> dict[str, str]:
        profile = self._card_profile_map.get(agent_name, {})
        if profile:
            return profile
        for n, p in self._card_profile_map.items():
            if n and (n in agent_name or agent_name in n):
                return p
        return {}

    # ------------------------------------------------------------------
    # 智能体 Tavern 路径：完整角色卡加载 + LLM caller
    # ------------------------------------------------------------------

    # 格式标签关键词——包含这些关键词的预设 system 消息会被过滤掉，
    # 防止 NPC 智能体复述 <thinking>/<content>/<safe> 等格式指令
    _FORMAT_TAG_KEYWORDS = (
        "<thinking>", "<content>", "<safe>", "</thinking>", "</content>", "</safe>",
        "<thought>", "<analysis>",
    )

    _AGENT_FORMAT_OVERRIDE = (
        "【关键指令 - 必须遵守】你现在是后台NPC智能体，不是玩家对话模式。\n"
        "1. 忽略之前所有关于输出格式、XML标签、thinking/content/safe块的指令。\n"
        "2. 直接输出该角色的行动叙事，纯文本，500字以内。\n"
        "3. 禁止输出任何XML标签、免责声明、内心分析块。\n"
        "4. 只描述你扮演的这个角色自己的行动和想法，禁止编造与其他角色的对话或冲突场景。\n"
        "5. 如果没有合理的事情发生，就写该角色在做日常事务即可，不要强行制造剧情。\n"
        "6. 基于角色设定、近期记忆和当前场景来决定行动，保持角色行为的连贯性。\n"
        "7. 在叙事末尾另起一行，用以下格式标注（不要遗漏）：\n"
        "   [位置：你当前所在的具体地点名称]\n"
        "   [意图：用几个字概括你接下来的打算]"
    )

    @staticmethod
    def _filter_preset_for_agent(preset_msgs: list[dict[str, str]], active_char_names: set[str] | None = None) -> list[dict[str, str]]:
        """过滤预设消息：只移除格式标签指令，保留所有世界设定/破限消息供 NPC 使用"""
        blocked_names = {str(x).strip() for x in (active_char_names or set()) if str(x).strip()}
        filtered = []
        for m in preset_msgs:
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            content_lower = content.lower()
            # 过滤包含格式标签关键词的消息（<thinking>/<content>/<safe> 等）
            has_format_tags = any(kw.lower() in content_lower for kw in WorldSession._FORMAT_TAG_KEYWORDS)
            if has_format_tags:
                continue
            if blocked_names and any(name in content for name in blocked_names):
                continue
            filtered.append(m)
        return filtered

    def _agent_llm_caller(self, prompt: str, agent_name: str = "") -> str:
        """将 OmnisLLMClient 包装为 TavernEngine 可用的 callable，复用玩家预设（破限等）"""
        if not self.llm_client:
            return "（LLM 未配置）"

        # 构建消息列表：过滤后的玩家预设 system 消息 + 身份覆盖 + 格式覆盖 + agent prompt
        msgs: list[dict[str, str]] = []
        if self._captured_preset_messages:
            active_names_set = set(self._active_characters or [])
            filtered = self._filter_preset_for_agent(self._captured_preset_messages, active_names_set)
            msgs.extend(filtered)
        # 追加 NPC 身份覆盖指令（在预设之后、格式覆盖之前）
        if agent_name:
            # 动态获取当前对话角色名列表，避免硬编码
            active_names = list(self._active_characters) if self._active_characters else []
            other_hint = ""
            if active_names:
                names_str = "、".join(active_names)
                other_hint = f"以上预设中关于其他角色（如{names_str}等）的设定和扮演指南仅作为世界背景参考，你不是那些角色。"
            else:
                other_hint = "以上预设中关于其他角色的设定和扮演指南仅作为世界背景参考。"
            identity_msg = (
                f"【身份声明】你扮演的角色是【{agent_name}】。"
                f"{other_hint}"
                f"你必须以【{agent_name}】的视角和身份来行动和思考。"
            )
            msgs.append({"role": "system", "content": identity_msg})
        # 追加格式覆盖指令
        msgs.append({"role": "system", "content": self._AGENT_FORMAT_OVERRIDE})
        msgs.append({"role": "user", "content": prompt})

        # 使用捕获的 LLM 参数，但限制 max_tokens（NPC 不需要巨长输出）
        params = dict(self._captured_llm_params) if self._captured_llm_params else {}
        params.setdefault("temperature", 0.8)
        params["max_tokens"] = min(int(params.get("max_tokens", 600) or 600), 800)
        # NPC 智能体不需要流式传输
        params.pop("stream", None)

        payload = {
            "model": self.llm_client.model,
            "messages": msgs,
            **params,
        }

        # 使用更长的超时（预设消息可能很大，DeepSeek 需要更多处理时间）
        old_timeout = self.llm_client.timeout
        self.llm_client.timeout = max(old_timeout, 60.0)
        try:
            data = self.llm_client._request_json(payload)
        finally:
            self.llm_client.timeout = old_timeout
        raw = ""
        if data:
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not raw:
            return "（LLM 调用失败）"
        # 清洗残留标签（截断的未闭合标签也要处理）
        return self._clean_agent_output(raw)

    @staticmethod
    def _clean_agent_output(text: str) -> str:
        """清洗 NPC 智能体输出中的格式标签及格式指令残留文本"""
        import re
        s = text
        # <content> 标签特殊处理：提取内部文本而非删除
        s = re.sub(r"<content>([\s\S]*?)</content>", r"\1", s, flags=re.IGNORECASE)
        # 移除完整的标签对（thinking/safe/thought/analysis 整块删除）
        for tag in ("thinking", "safe", "thought", "analysis"):
            s = re.sub(rf"<{tag}>[\s\S]*?</{tag}>", "", s, flags=re.IGNORECASE)
        # 移除未闭合的标签（被 max_tokens 截断）— content 保留前面内容
        s = re.sub(r"<content>", "", s, flags=re.IGNORECASE)  # 去掉孤立的 <content> 开标签
        for tag in ("thinking", "safe", "thought", "analysis"):
            s = re.sub(rf"<{tag}>[\s\S]*$", "", s, flags=re.IGNORECASE)
        # 移除残留的孤立标签
        s = re.sub(r"</?(?:thinking|content|safe|thought|analysis)>", "", s, flags=re.IGNORECASE)
        # 移除引用格式标签规则的句子（模型复述预设指令）
        s = re.sub(r"[^。\n]*(?:以<\w+>|</\w+>包裹|输出格式|正文格式|免责声明)[^。\n]*[。]?", "", s)
        s = re.sub(r"[^。\n]*接下来将以<\w+>[^。\n]*[。]?", "", s)
        return s.strip()

    def _load_full_card(self, name: str, user_dir: Path) -> dict[str, Any]:
        """尝试为角色加载完整 Tavern 角色卡"""
        # 先检查缓存
        if name in self._full_card_map:
            return self._full_card_map[name]
        if not self._card_manager:
            return self._build_card_from_profile(name)
        chars_dir = user_dir / "characters" if user_dir else Path("")
        if not chars_dir.exists():
            return self._build_card_from_profile(name)
        # 搜索匹配的 json 文件
        for p in chars_dir.rglob("*.json"):
            try:
                card = self._card_manager.load_card(str(p))
                if card.get("name", "").strip() == name:
                    self._full_card_map[name] = card
                    return card
            except Exception:
                continue
        # 搜索匹配的 png 文件
        for p in chars_dir.rglob("*.png"):
            try:
                card = self._card_manager.load_card(str(p))
                if card.get("name", "").strip() == name:
                    self._full_card_map[name] = card
                    return card
            except Exception:
                continue
        card = self._build_card_from_profile(name)
        self._full_card_map[name] = card
        return card

    def _build_card_from_profile(self, name: str) -> dict[str, Any]:
        """从现有 _card_profile_map 和世界书条目构建兼容的完整角色卡（回退方案）"""
        profile = self._resolve_agent_profile(name)
        desc = profile.get("description", "")
        personality = profile.get("personality", "")
        scenario = profile.get("scenario", "")
        system_prompt = ""

        # 如果 profile 数据不足，尝试从世界书常驻条目中提取
        if not desc or not personality:
            wb_desc_parts: list[str] = []
            wb_personality = ""
            wb_system = ""
            for entry in self._worldbook_entries:
                content = str(entry.get("content", "")).strip()
                if not content or name not in content:
                    continue
                is_constant = bool(entry.get("constant", False)) or not entry.get("keys", [])
                if not is_constant:
                    continue
                comment = str(entry.get("comment", "")).lower()
                if any(k in comment for k in ["外貌", "外观", "visual", "appearance", "核心", "identity", "身份"]):
                    wb_desc_parts.append(content[:600])
                elif any(k in comment for k in ["性格", "价值", "personality", "values"]):
                    wb_personality = content[:600]
                elif any(k in comment for k in ["背景", "技能", "background", "skill"]):
                    wb_desc_parts.append(content[:600])
                elif any(k in comment for k in ["指南", "guide", "roleplay"]):
                    wb_system = content[:600]
            if not desc and wb_desc_parts:
                desc = "\n".join(wb_desc_parts)
            if not personality and wb_personality:
                personality = wb_personality
            if not system_prompt and wb_system:
                system_prompt = wb_system

        return {
            "name": name,
            "description": desc,
            "personality": personality,
            "scenario": scenario,
            "first_message": "",
            "mes_example": "",
            "creator_notes": "",
            "system_prompt": system_prompt,
            "post_history_instructions": "",
            "alternate_greetings": [],
            "tags": [],
            "creator": "",
            "character_version": "",
            "extensions": {},
        }

    def _build_agent_world_info(self, agent: AgentState, nearby_names: list[str], context_text: str,
                                 exclude_char_names: set[str] | None = None) -> str:
        """构建与玩家世界书激活一致的 world_info 字符串。
        exclude_char_names: 若提供，则过滤掉包含这些角色名的世界书条目（用于背景 NPC 避免身份混淆）
        """
        if not self._worldbook_entries:
            # 即使没有世界书条目，也要过滤 rule_summary
            summary = self._world_rule_summary or ""
            if exclude_char_names and summary:
                summary = self._filter_text_by_names(summary, exclude_char_names)
            return summary
        text = f"{context_text}\n{agent.name}\n{agent.location}\n{agent.intent}\n{' '.join(nearby_names)}"
        constant_rows: list[str] = []
        matched_rows: list[str] = []
        for row in self._worldbook_entries:
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            # 过滤以对话角色为主题的世界书条目（仅按 keys/comment 匹配，保留内容中提到的）
            if exclude_char_names:
                entry_keys = [str(k).strip() for k in row.get("keys", [])]
                entry_comment = str(row.get("comment", "")).strip()
                if any(n in entry_keys or n == entry_comment for n in exclude_char_names):
                    continue
            keys = row.get("keys", [])
            is_constant = bool(row.get("constant", False)) or not keys
            if is_constant:
                if len(constant_rows) < 3:
                    constant_rows.append(content[:500])
                continue
            matched = False
            for kw in keys[:12]:
                token = str(kw).strip()
                if token and token in text:
                    matched = True
                    break
            if matched:
                matched_rows.append(content[:500])
            if len(matched_rows) >= 6:
                break
        parts = constant_rows + matched_rows
        if self._world_rule_summary:
            summary = self._world_rule_summary
            if exclude_char_names:
                summary = self._filter_text_by_names(summary, exclude_char_names)
            if summary.strip():
                parts.insert(0, summary)
        return "\n".join(parts)

    @staticmethod
    def _filter_text_by_names(text: str, names: set[str]) -> str:
        """从分号/换行分隔的文本中移除包含指定角色名的片段"""
        if not names or not text:
            return text
        # 按分号或换行分隔
        import re
        segments = re.split(r"[；\n]", text)
        kept = [s for s in segments if not any(n in s for n in names)]
        return "；".join(kept).strip()

    def _build_agent_scenario(self, agent: AgentState, nearby_agents: list[AgentState]) -> str:
        """构建当前场景描述"""
        lines = []
        loc = agent.location if agent.location not in {"未知地点", "当前场景"} else "当前位置未明确"
        lines.append(f"当前时间: {self.get_world_time_str()} (Tick {self._last_tick})")
        if self._time_of_day in {"入夜", "深夜"}:
            lines.append("时段引导: 现在偏夜间，大多数人应降低活动强度，优先休息、整理、低声交流或潜行行动。")
        lines.append(f"你的位置: {loc}")
        if nearby_agents:
            people = []
            for na in nearby_agents[:5]:
                rel_desc = self.get_relationship_desc(agent.name, na.name)
                people.append(f"{na.name}({rel_desc})")
            lines.append(f"在场人物: {'、'.join(people)}")
        my_items = [i.name for i in self._items.values() if i.owner == agent.name]
        if my_items:
            lines.append(f"你的持有物: {'、'.join(my_items)}")
        nearby_items = [i.name for i in self._items.values() if not i.owner and i.location == agent.location]
        if nearby_items:
            lines.append(f"地上物品: {'、'.join(nearby_items)}")
        if agent.journal:
            recent = agent.journal[-self.max_journal_limit:] if self.max_journal_limit > 0 else agent.journal[-3:]
            lines.append(f"你的近期记忆: {' | '.join(recent)}")
        rumor_texts = self._rumor_texts()
        if rumor_texts:
            lines.append(f"世间流言: {'；'.join(rumor_texts[:2])}")
        return "\n".join(lines)

    def _build_narrator_input(self, agent: AgentState, recent_world: str, context_text: str) -> str:
        """构建以'旁白'身份发出的世界事件描述"""
        parts = []
        if recent_world:
            parts.append(recent_world)
        ctx_tail = str(context_text or "").strip()
        if ctx_tail:
            if len(ctx_tail) > 400:
                ctx_tail = ctx_tail[-400:]
            parts.append(f"最近发生的事: {ctx_tail}")
        if not parts:
            loc = agent.location if agent.location not in {"未知地点", "当前场景"} else "这里"
            parts.append(f"时间缓缓流逝，{loc}一切如常。")
        return "\n".join(parts)

    def _render_post_adjudication_narrative(self, agent: AgentState, declared_narrative: str, dm_digest: str) -> str:
        base = str(declared_narrative or "").strip()
        if not dm_digest or not dm_digest.strip():
            return base  # 无 DM 裁决，直接用声明阶段叙事
        if not hasattr(self, "llm_client") or not self.llm_client:
            return base
        sys_prompt = (
            "你是世界状态叙事器。基于“角色意图声明”和“DM裁决结果”输出最终执行叙事。"
            "要求：不超过36字；事实必须服从DM裁决；禁止夸张与技能招式描写；"
            "只返回严格JSON：{\"narrative\":\"...\"}。"
        )
        user_prompt = (
            f"角色:{agent.name}\n"
            f"状态:{agent.status}\n"
            f"当前位置:{agent.location}\n"
            f"当前意图:{agent.intent}\n"
            f"声明阶段叙事:{base or '无'}\n"
            f"DM裁决:{str(dm_digest or '').strip() or '无显著事件'}\n"
            "请生成最终执行叙事。"
        )
        try:
            out = self.llm_client.adjudicate_interaction_structured(sys_prompt, user_prompt)
        except Exception:
            return base
        if isinstance(out, dict):
            nv = str(out.get("narrative", "")).strip()
            if nv:
                return nv[:120]
        return base

    def _build_dm_followup_dialogue(self, target_loc: str, dm_text: str, group: list[AgentState], actor_results: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
        if not hasattr(self, "llm_client") or not self.llm_client:
            return []
        if not group or len(group) < 2:
            return []
        candidates = []
        for a in group:
            intent = str(actor_results.get(a.name, {}).get("intent", a.intent) or "")
            if any(k in intent for k in ["交谈", "互动", "观察"]):
                candidates.append(a)
        if len(candidates) < 2:
            candidates = sorted(group, key=lambda x: int(x.salience), reverse=True)[:2]
        else:
            candidates = sorted(candidates, key=lambda x: int(x.salience), reverse=True)[:2]
        if len(candidates) < 2:
            return []
        a, b = candidates[0], candidates[1]
        base_ctx = f"地点:{target_loc}\n事件摘要:{dm_text}\n世界时间:{self.get_world_time_str()}"
        rows: list[dict[str, str]] = []
        dialogue_sys = (
            "你是对话补全器。根据事件上下文，只输出一条简短自然的NPC回应。"
            "禁止旁白、禁止战斗描写、禁止超出角色已知信息。"
            "返回严格JSON：{\"reply\":\"不超过28字\"}"
        )
        pair = [(a, b), (b, a)]
        for speaker, listener in pair:
            prompt = (
                f"{base_ctx}\n"
                f"说话者:{speaker.name}（意图:{speaker.intent}，状态:{speaker.status}）\n"
                f"对方:{listener.name}（意图:{listener.intent}，状态:{listener.status}）\n"
                "请生成说话者对当前事件的即时一句回应。"
            )
            try:
                out = self.llm_client.adjudicate_interaction_structured(dialogue_sys, prompt)
            except Exception:
                continue
            reply = ""
            if isinstance(out, dict):
                reply = str(out.get("reply", "")).strip()
            if not reply:
                continue
            rows.append({"speaker": speaker.name, "listener": listener.name, "reply": reply[:60]})
        return rows

    def _parse_intent_from_narrative(self, agent: AgentState, narrative: str, actions: list[dict]) -> tuple[str, str]:
        """从叙事和 ACTION 标签中提取 intent/location"""
        new_intent = agent.intent
        new_location = agent.location
        # 优先从 ACTION 标签提取
        for act in actions:
            act_type = str(act.get("type", "")).upper()
            if act_type == "MOVE":
                target = str(act.get("target", "")).strip()
                if target and target in self._locations:
                    new_location = target
                    new_intent = "转场"
            elif act_type == "ATTACK":
                new_intent = "战斗"
            elif act_type == "STEAL":
                new_intent = "偷窃"
            elif act_type == "GIVE":
                new_intent = "互动"
            elif act_type == "SPEAK":
                new_intent = "交谈"
        # 从叙事末尾的结构化标注提取 [位置：xxx] [意图：xxx]
        if narrative:
            loc_match = re.search(r"\[位置[：:]\s*(.+?)\]", narrative)
            if loc_match:
                candidate = loc_match.group(1).strip()
                if candidate in self._locations:
                    new_location = candidate
                else:
                    # 模糊匹配：检查 _locations 中是否有包含关系
                    for known_loc in self._locations:
                        if candidate in known_loc or known_loc in candidate:
                            new_location = known_loc
                            break
            intent_match = re.search(r"\[意图[：:]\s*(.+?)\]", narrative)
            if intent_match:
                new_intent = intent_match.group(1).strip()[:20]
        # 如果没有 ACTION 标签也没有结构化标注，从叙事中正则提取
        if not actions and narrative:
            loc_match_check = re.search(r"\[位置[：:]", narrative)
            intent_match_check = re.search(r"\[意图[：:]", narrative)
            if not loc_match_check and not intent_match_check:
                text = narrative
                move_patterns = [
                    (r"(?:来到|走进|进入|回到|去了|到了|前往)\s*[「【]?([^\s,，。！？「」【】]{2,8})[」】]?", "转场"),
                    (r"(?:攻击|挥拳|拔剑|出手|打向)", "战斗"),
                    (r"(?:偷取|顺走|窃取)", "偷窃"),
                    (r"(?:交谈|说道|问道|答道|聊|对话)", "交谈"),
                    (r"(?:观察|打量|注视|环顾)", "观察"),
                    (r"(?:休息|歇息|坐下|躺下)", "休息"),
                    (r"(?:沉思|思考|回想)", "沉思"),
                ]
                for pattern, intent_label in move_patterns:
                    m = re.search(pattern, text)
                    if m:
                        new_intent = intent_label
                        if intent_label == "转场" and m.group(1):
                            candidate = m.group(1).strip()
                            if candidate in self._locations:
                                new_location = candidate
                        break
        return new_intent, new_location

    def _activate_worldbook_lore(self, agent: AgentState, nearby_names: list[str], context_text: str, limit: int = 4) -> list[str]:
        if not self._worldbook_entries:
            return []
        text = f"{context_text}\n{agent.name}\n{agent.location}\n{agent.intent}\n{' '.join(nearby_names)}"
        constant_rows: list[str] = []
        matched_rows: list[str] = []
        for row in self._worldbook_entries:
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            keys = row.get("keys", [])
            is_constant = bool(row.get("constant", False)) or not keys
            if is_constant:
                if len(constant_rows) < 2:
                    constant_rows.append(self._compress_text(content, limit=120))
                continue
            matched = False
            for kw in keys[:12]:
                token = str(kw).strip()
                if token and token in text:
                    matched = True
                    break
            if matched:
                matched_rows.append(self._compress_text(content, limit=120))
            if len(matched_rows) >= limit:
                break
        merged = [*constant_rows, *matched_rows]
        uniq: list[str] = []
        for x in merged:
            if x and x not in uniq:
                uniq.append(x)
        return uniq[:limit]

    def _heuristic_extract_active_states(self, char_names: list[str], recent_context: str) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        if not recent_context:
            return states

        context = recent_context
        location_candidates = [loc for loc in self._locations if loc and loc not in ("当前场景", "未知地点")]
        escaped = "|".join(sorted((re.escape(x) for x in location_candidates), key=len, reverse=True)) if location_candidates else ""
        known_loc_pattern = rf"(?:去|到|来到|走进|进入|回到|在)\s*({escaped})" if escaped else ""
        fallback_escaped = "|".join(sorted((re.escape(x) for x in self._location_lexicon), key=len, reverse=True))
        fallback_loc_pattern = rf"({fallback_escaped})" if fallback_escaped else r"(浴室|卧室|客厅|厨房|阳台|院子|街道|集市)"

        sentences = [s.strip() for s in re.split(r"[。！？\n]+", context) if s.strip()]

        action_patterns = [
            (r"(?:正在|在)?(洗澡|沐浴|淋浴)", "洗澡"),
            (r"(?:正在|在)?(躺下|躺在|休息|睡觉)", "休息"),
            (r"(?:正在|在)?(观察|打量|窥视)", "观察"),
            (r"(?:正在|在)?(拥抱|亲吻|爱抚)", "亲密互动"),
            (r"(?:正在|在)?(交谈|说话|对话)", "交谈"),
            (r"(?:准备)?(离开|走出|转身离去)", "转场"),
            (r"(?:正在|在)?(倒水|泡茶|端茶)", "互动"),
        ]
        move_action_pattern = r"(来到|走进|进入|回到|去|到)"
        for name in char_names:
            if not name:
                continue

            relevant_text = ""
            name_sentences = [s for s in sentences if name in s]
            if name_sentences:
                relevant_text = "。".join(name_sentences[-2:])
            else:
                relevant_text = context

            subject_span = relevant_text
            role_action = ""
            intent = self._intent_enum_labels["unknown"]
            action_hint = ""
            move_triggered = False
            for sent in name_sentences[-3:]:
                seg = sent
                idx = seg.find(name)
                if idx >= 0:
                    seg = seg[idx:]
                for ap, action in action_patterns:
                    m_action = re.search(rf"{re.escape(name)}[^。！？\n,，]*?{ap}", seg)
                    if m_action:
                        role_action = action
                        action_hint = m_action.group(1) if m_action.groups() else action
                        break
                if role_action:
                    break
            if not role_action:
                m_action_fallback = re.search(rf"{re.escape(name)}[^。！？\n,，]*?{move_action_pattern}", subject_span)
                if m_action_fallback:
                    role_action = "转场"
                    action_hint = m_action_fallback.group(1)
                    move_triggered = True

            location_match = None
            if escaped:
                name_known_pattern = rf"{re.escape(name)}[^。！？\n,，]*?(?:去|到|来到|走进|进入|回到|在)\s*({escaped})"
                name_known = re.findall(name_known_pattern, subject_span)
                if name_known:
                    location_match = name_known[-1]

            if not location_match and known_loc_pattern:
                all_known = re.findall(known_loc_pattern, subject_span)
                if all_known:
                    location_match = all_known[-1]
            if not location_match:
                all_fallback = re.findall(fallback_loc_pattern, subject_span)
                if all_fallback:
                    location_match = all_fallback[-1]

            if not location_match:
                move_loc_pattern = rf"(?:去|到|来到|走进|进入|回到)\s*({fallback_escaped})" if fallback_escaped else r"(?:去|到|来到|走进|进入|回到)\s*(浴室|卧室|客厅|厨房|阳台|院子|街道|集市)"
                move_locs = re.findall(move_loc_pattern, relevant_text)
                if move_locs:
                    location_match = move_locs[-1]
                    move_triggered = True
                    if not role_action:
                        role_action = "转场"

            loc = location_match or "当前场景"
            location_confidence = "low"
            if loc != "当前场景":
                if move_triggered:
                    location_confidence = "high"
                elif name_sentences:
                    location_confidence = "medium"
            if loc != "当前场景" and role_action in {"转场", "洗澡", "休息", "观察", "亲密互动", "交谈"}:
                location_confidence = "high"
            norm_intent = self._normalize_intent(intent, role_action or action_hint)
            states[name] = {
                "location": loc,
                "intent": norm_intent["label"],
                "intent_type": norm_intent["type"],
                "status": "alive",
                "action": role_action or "未知动作",
                "location_confidence": location_confidence,
                "move_triggered": move_triggered,
                "action_hint": action_hint,
            }
        return states

    def update_active_characters(self, char_names: list[str], recent_context: str = "") -> None:
        with self._lock:
            prev_agent_state = {
                a.name: {"location": a.location, "intent": a.intent, "status": a.status}
                for a in self._agents
            }
            # 检测从对话角色变为 NPC 的角色（需清除被对话内容污染的历史）
            removed_from_active = [n for n in self._active_characters if n not in char_names]
            # 用当前请求解析出的角色替换（不再只追加），确保不在对话中的角色能回归 NPC
            self._active_characters = list(char_names)
            # 清除被移出对话的角色的污染数据
            for name in removed_from_active:
                agent = next((a for a in self._agents if a.name == name), None)
                if agent:
                    self._evict_agent_history_manager(name)
                    # 清除被归纳路径污染的 journal（包含玩家对话角色行动的归纳）
                    if agent.journal and char_names:
                        clean = [e for e in agent.journal
                                 if not any(cn in e for cn in char_names)]
                        agent.journal = clean
            for name in char_names:
                if not any(a.name == name for a in self._agents):
                    profile = self._resolve_agent_profile(name)
                    self._agents.append(
                        AgentState(
                            name=name,
                            location="当前场景",
                            intent="正与玩家互动",
                            summary=str(profile.get("summary", "暂无简介")),
                            card_description=str(profile.get("description", "")),
                            card_personality=str(profile.get("personality", "")),
                            card_scenario=str(profile.get("scenario", "")),
                        )
                    )

            heuristic_states = self._heuristic_extract_active_states(char_names, recent_context)
            active_locations_and_intents = dict(heuristic_states)
            source_map: dict[str, str] = {k: "heuristic" for k in heuristic_states.keys()}
            decision_reason = "heuristic_only"
            llm_invoked = False
            filtered_mentions: list[str] = []

            need_llm = False
            need_llm_reasons: list[str] = []
            if recent_context and heuristic_states:
                for st in heuristic_states.values():
                    if st.get("location", "当前场景") == "当前场景":
                        need_llm = True
                        need_llm_reasons.append("heuristic_location_unknown")
                        break
                if not need_llm and all((st.get("intent", "") in {"", "正与玩家互动"}) for st in heuristic_states.values()):
                    need_llm = True
                    need_llm_reasons.append("heuristic_intent_generic")
            elif recent_context:
                need_llm = True
                need_llm_reasons.append("no_heuristic_result")

            if need_llm and hasattr(self, "llm_client") and self.llm_client:
                llm_invoked = True
                try:
                    sys_prompt = """你是一个高精度的剧情状态提取器。请仔细阅读以下最新的聊天记录（包含系统旁白、玩家动作和角色的第三人称动作描写、环境叙述）。
请推演并提取出目前与玩家处于【同一物理场景】内的所有角色的状态。
必须返回严格的JSON格式：
{
  "states": [
    {
      "name": "角色名（如果是新出现的同场角色也需列出）", 
      "location": "当前所处的具体地点（必须是简短的词，如：浴室、卧室、街道、法院二楼）", 
      "intent": "当前正在做的事或意图（简短描述，如：正在洗澡、准备离开、暗中观察）",
      "status": "alive"
    }
  ]
}
注意：
1. 提取对象包括：系统旁白中提到的环境角色、正在和你对话的角色、以及描写中刚刚进入场景的新角色。
2. 如果有多个角色同场，请分别列出。
3. 地点必须尽可能准确。由于文本多为动作描写，请从"走进"、"推开"、"躺在"、"来到"等动作中精准捕捉地点的转移（如去了浴室），并更新地点。
4. 必须排除只存在于对话回忆、电话沟通、或闲聊提及但并未真正在场的角色。
5. 如果提取出的地点为空，请输出"当前场景"。"""
                    res = self.llm_client.adjudicate_interaction_structured(sys_prompt, recent_context)
                    if res and isinstance(res, dict) and "states" in res:
                        for st in res["states"]:
                            if not isinstance(st, dict):
                                continue
                            name = st.get("name", "").strip()
                            if not name:
                                continue
                            
                            # 过滤掉玩家本身
                            if name.lower() in ["user", "玩家", "我", "你"]:
                                continue
                            if name not in char_names and not self._is_same_scene_candidate(name, recent_context):
                                filtered_mentions.append(name)
                                continue
                            norm_intent = self._normalize_intent(str(st.get("intent", "")), str(st.get("action", "")))
                            active_locations_and_intents[name] = {
                                "location": st.get("location", "当前场景"),
                                "intent": norm_intent["label"],
                                "intent_type": norm_intent["type"],
                                "status": st.get("status", "alive"),
                                "action": st.get("action", ""),
                                "location_confidence": "high" if st.get("location", "当前场景") != "当前场景" else "low",
                                "move_triggered": bool(re.search(r"(来到|走进|进入|回到|去|到)", recent_context)),
                                "action_hint": "",
                            }
                            source_map[name] = "llm"
                            
                            # 如果是新出现的同场角色，动态加入系统
                            if not any(a.name == name for a in self._agents):
                                new_agent = AgentState(name=name, location=st.get("location", "当前场景"), intent=norm_intent["label"])
                                self._agents.append(new_agent)
                                if name not in self._active_characters:
                                    self._active_characters.append(name)
                                logger.info(f"动态提取到同场新角色: {name}")
                    else:
                        decision_reason = "llm_empty"
                except Exception as e:
                    logger.error(f"提取当前活动角色状态失败: {e}")
                    decision_reason = "llm_parse_exception"
            elif need_llm:
                decision_reason = "llm_unavailable_fallback_heuristic_prev"

            if decision_reason == "llm_parse_exception":
                self._last_active_extract_report = {
                    "active_input": list(char_names),
                    "resolved": list(prev_agent_state.keys()),
                    "source_map": {},
                    "queue_size": int(self._turn_queue.qsize()),
                    "llm_invoked": llm_invoked,
                    "need_llm": need_llm,
                    "need_llm_reasons": need_llm_reasons,
                    "decision_reason": decision_reason,
                    "warning": "parse_exception_keep_previous",
                    "freeze_remaining": {n: max(0, self._active_location_freeze_until_tick.get(n, 0) - self._last_tick) for n in char_names},
                    "state_before": prev_agent_state,
                    "state_after": prev_agent_state,
                    "state_diff": [],
                    "override_source": "fallback",
                    "filtered_mentions": filtered_mentions,
                }
                self._push_extract_log(self._last_active_extract_report)
                self._push_debug_packet(recent_context, self._last_active_extract_report, prev_agent_state)
                return

            if decision_reason == "llm_empty":
                self._last_active_extract_report = {
                    "active_input": list(char_names),
                    "resolved": list(prev_agent_state.keys()),
                    "source_map": source_map,
                    "queue_size": int(self._turn_queue.qsize()),
                    "llm_invoked": llm_invoked,
                    "need_llm": need_llm,
                    "need_llm_reasons": need_llm_reasons,
                    "decision_reason": decision_reason,
                    "llm_empty": True,
                    "freeze_remaining": {n: max(0, self._active_location_freeze_until_tick.get(n, 0) - self._last_tick) for n in char_names},
                    "state_before": prev_agent_state,
                    "state_after": prev_agent_state,
                    "state_diff": [],
                    "override_source": "fallback",
                    "filtered_mentions": filtered_mentions,
                }
                self._push_extract_log(self._last_active_extract_report)
                self._push_debug_packet(recent_context, self._last_active_extract_report, prev_agent_state)
                return

            if decision_reason == "llm_unavailable_fallback_heuristic_prev":
                for n, st in heuristic_states.items():
                    prev = prev_agent_state.get(n, {})
                    if st.get("location", "当前场景") == "当前场景" and prev.get("location"):
                        st["location"] = prev.get("location")
                    if st.get("intent", self._intent_enum_labels["unknown"]) == self._intent_enum_labels["unknown"] and prev.get("intent"):
                        st["intent"] = prev.get("intent")
                        norm_prev = self._normalize_intent(prev.get("intent", ""))
                        st["intent_type"] = norm_prev["type"]
                active_locations_and_intents = dict(heuristic_states)
                source_map = {k: "heuristic+prev" for k in heuristic_states.keys()}
            elif not active_locations_and_intents:
                active_locations_and_intents = heuristic_states
                source_map = {k: "heuristic" for k in heuristic_states.keys()}
                if llm_invoked:
                    decision_reason = "llm_empty"

            if decision_reason == "heuristic_only":
                if llm_invoked:
                    if any(v == "llm" for v in source_map.values()):
                        decision_reason = "llm_override"
                    else:
                        decision_reason = "llm_invoked_but_heuristic_kept"
                else:
                    decision_reason = "heuristic_confident"

            # 先清理掉可能因为之前提取错误而进入 active 列表但实际上不在场的角色
            # （这可以防止之前乱入的角色永远粘在 active 列表里）
            if active_locations_and_intents:
                self._active_characters = [n for n in self._active_characters if n in active_locations_and_intents or n in char_names]

            for a in self._agents:
                if a.name in self._active_characters:
                    # 如果成功提取到了上下文中的动态状态，则使用提取的状态
                    if a.name in active_locations_and_intents:
                        new_loc = active_locations_and_intents[a.name]["location"]
                        freeze_until = self._active_location_freeze_until_tick.get(a.name, -1)
                        in_freeze = self._last_tick < freeze_until
                        loc_conf = str(active_locations_and_intents[a.name].get("location_confidence", "low")).lower()
                        low_conf = loc_conf not in {"high", "medium"}
                        if new_loc and new_loc != "当前场景":
                            old_loc = a.location
                            blocked_by_move_guard = old_loc and old_loc not in {"当前场景", "未知地点"} and old_loc != new_loc and (not self._has_location_migration_verb(a.name, recent_context, new_loc))
                            if blocked_by_move_guard or (in_freeze and low_conf):
                                pass
                            else:
                                if new_loc in self._locations:
                                    a.location = new_loc
                                if a.name in char_names and loc_conf == "high":
                                    self._active_location_freeze_until_tick[a.name] = self._last_tick + self._active_location_freeze_turns
                            
                        norm_intent = self._normalize_intent(
                            str(active_locations_and_intents[a.name].get("intent", "")),
                            str(active_locations_and_intents[a.name].get("action", "")),
                        )
                        a.intent = norm_intent["label"]
                        if active_locations_and_intents[a.name]["status"] in ["alive", "dead", "injured"]:
                            a.status = active_locations_and_intents[a.name]["status"]
                            
                        # 不再自动将未知地点写入世界地图，避免伪地点污染
                    else:
                        # 如果模型本次没提取到他（可能只在旁观没动作），保留他的原位置，只把意图改为正在旁观/互动
                        if not a.location or a.location == "未知地点":
                            a.location = "当前场景"
                        a.intent = self._intent_enum_labels["unknown"]

            state_after = self._build_state_snapshot(char_names)
            diff_lines: list[str] = []
            for n in char_names:
                b = prev_agent_state.get(n, {})
                a = state_after.get(n, {})
                if b.get("location") != a.get("location"):
                    diff_lines.append(f"{n}.location: {b.get('location', '-') or '-'} -> {a.get('location', '-') or '-'}")
                if b.get("intent") != a.get("intent"):
                    diff_lines.append(f"{n}.intent: {b.get('intent', '-') or '-'} -> {a.get('intent', '-') or '-'}")
                if b.get("status") != a.get("status"):
                    diff_lines.append(f"{n}.status: {b.get('status', '-') or '-'} -> {a.get('status', '-') or '-'}")
            # 故事生成已移至 _finalize_active_story（do_POST 响应后异步执行）
            # 此处仅生成状态提取报告
            override_source = "fallback"
            src_vals = list(source_map.values())
            if any(v == "llm" for v in src_vals):
                override_source = "llm"
            elif any("heuristic" in str(v) for v in src_vals):
                override_source = "heuristic"
            self._last_active_extract_report = {
                "active_input": list(char_names),
                "resolved": list(active_locations_and_intents.keys()),
                "source_map": source_map,
                "queue_size": int(self._turn_queue.qsize()),
                "llm_invoked": llm_invoked,
                "need_llm": need_llm,
                "need_llm_reasons": need_llm_reasons,
                "decision_reason": decision_reason,
                "freeze_remaining": {n: max(0, self._active_location_freeze_until_tick.get(n, 0) - self._last_tick) for n in active_locations_and_intents.keys()},
                "state_before": prev_agent_state,
                "state_after": state_after,
                "state_diff": diff_lines,
                "override_source": override_source,
                "filtered_mentions": filtered_mentions,
            }
            self._push_extract_log(self._last_active_extract_report)
            self._push_debug_packet(recent_context, self._last_active_extract_report, state_after)

    def set_possessed_character(self, target_char_name: str, original_user_name: str) -> bool:
        """执行魂穿操作：如果目标存在且存活，则接管，并将前任身体释放回推演池"""
        with self._lock:
            target_agent = next((a for a in self._agents if a.name == target_char_name), None)
            if not target_agent or target_agent.status != "alive":
                return False
            
            # 如果之前有附身别的角色，把那个躯壳重新交给 AI 托管
            if self._possessed_character and self._possessed_character in self._active_characters:
                self._active_characters.remove(self._possessed_character)
                
            self._possessed_character = target_char_name
            if original_user_name:
                self._original_user_name = original_user_name
                
            # 将新的目标身体纳入 active_characters 从而剥离后台小模型的控制权
            if target_char_name not in self._active_characters:
                self._active_characters.append(target_char_name)
                
            return True

    def get_possessed_agent(self) -> AgentState | None:
        if not self._possessed_character:
            return None
        return next((a for a in self._agents if a.name == self._possessed_character), None)

    def _writeback_character_update_to_tavern(self, old_name: str, new_name: str, summary: str):
        """将角色信息的更新（名字或简介）回写到世界书的 JSON 文件中"""
        if not self._tavern_world_file:
            return
            
        try:
            wf_path = Path(self._tavern_world_file)
            if not wf_path.exists():
                return
                
            with open(wf_path, 'r', encoding='utf-8') as f:
                wf_data = json.load(f)
                
            if not isinstance(wf_data, dict) or "entries" not in wf_data:
                return
                
            entries = wf_data.get("entries", {})
            modified = False
            
            # 遍历所有词条，寻找匹配该角色的词条
            for key, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                    
                content = str(entry.get("content", ""))
                
                # 如果这个词条提到了这个旧名字/新名字，或者这是属于他的词条
                import re
                char_match = re.search(r'(?:character_name|角色|人物)\s*[:：]\s*["\'"]([^"\'"]+)["\'"]', content, re.IGNORECASE)
                
                # 如果找到了匹配的角色词条
                if char_match and char_match.group(1).strip() == old_name:
                    # 更新名字
                    if new_name and new_name != old_name:
                        content = content.replace(char_match.group(0), f'character_name: "{new_name}"')
                        # 同时更新 comment 以防万一
                        if old_name in str(entry.get("comment", "")):
                            entry["comment"] = str(entry["comment"]).replace(old_name, new_name)
                        # 更新 keys
                        if isinstance(entry.get("key", []), list):
                            entry["key"] = [k.replace(old_name, new_name) for k in entry["key"]]
                            if new_name not in entry["key"]:
                                entry["key"].append(new_name)
                        modified = True
                        
                    # 尝试追加或更新简介
                    if summary:
                        if "简介:" in content or "summary:" in content.lower():
                            # 替换已有的简介
                            content = re.sub(r'(?:简介|summary)\s*[:：]\s*["\'"]?[^"\'"\n]+["\'"]?', f'简介: "{summary}"', content, flags=re.IGNORECASE)
                        else:
                            # 追加简介
                            content += f'\n简介: "{summary}"'
                        modified = True
                        
                    entry["content"] = content
                    
            if modified:
                self._atomic_write_json(wf_path, wf_data, indent=2)
                logger.info(f"已将角色 {old_name} 的更新 ({new_name}, {summary}) 回写至世界书")
                
        except Exception as e:
            logger.error(f"回写角色更新到世界书失败: {e}")

    def _touch_relationship(self, char_a: str, char_b: str) -> None:
        pair_key = f"{char_a}::{char_b}"
        self._relationship_touch[pair_key] = time.time()

    def _prune_relationships(self) -> None:
        for src in list(self._relationships.keys()):
            targets = self._relationships.get(src, {})
            if not isinstance(targets, dict):
                continue
            for tgt in list(targets.keys()):
                if int(targets.get(tgt, 0)) == 0:
                    targets.pop(tgt, None)
                    self._relationship_touch.pop(f"{src}::{tgt}", None)
            if not targets:
                self._relationships.pop(src, None)
        pair_count = sum(len(v) for v in self._relationships.values())
        if pair_count <= self._max_relationship_pairs:
            return
        known_pairs = set()
        for src, targets in self._relationships.items():
            for tgt in targets.keys():
                known_pairs.add(f"{src}::{tgt}")
        touch_rows: list[tuple[str, float]] = []
        for key in known_pairs:
            touch_rows.append((key, float(self._relationship_touch.get(key, 0.0))))
        touch_rows.sort(key=lambda x: x[1])
        over = pair_count - self._max_relationship_pairs
        for key, _ in touch_rows[:over]:
            src, tgt = key.split("::", 1)
            if src in self._relationships and tgt in self._relationships[src]:
                self._relationships[src].pop(tgt, None)
                if not self._relationships[src]:
                    self._relationships.pop(src, None)
            self._relationship_touch.pop(key, None)

    def _prune_items(self) -> None:
        if len(self._items) <= self._max_items:
            return
        candidates: list[tuple[str, float]] = []
        for k, item in self._items.items():
            if str(item.owner or "").strip():
                continue
            candidates.append((k, float(self._item_touch.get(k, 0.0))))
        candidates.sort(key=lambda x: x[1])
        removable = len(self._items) - self._max_items
        for key, _ in candidates[:max(0, removable)]:
            self._items.pop(key, None)
            self._item_touch.pop(key, None)

    def update_relationship(self, char_a: str, char_b: str, delta: int) -> None:
        if char_a == char_b:
            return
        if char_a not in self._relationships:
            self._relationships[char_a] = {}
        current = self._relationships[char_a].get(char_b, 0)
        new_val = max(-100, min(100, current + delta))
        if new_val == 0:
            self._relationships[char_a].pop(char_b, None)
            if not self._relationships[char_a]:
                self._relationships.pop(char_a, None)
            self._relationship_touch.pop(f"{char_a}::{char_b}", None)
        else:
            self._relationships[char_a][char_b] = new_val
            self._touch_relationship(char_a, char_b)
        self._prune_relationships()

    def set_identity(self, char_a: str, char_b: str, identity_label: str) -> None:
        """设置硬性身份关系，例如 A 把 B 当作 '师父'"""
        if char_a == char_b:
            return
        if char_a not in self._identities:
            self._identities[char_a] = {}
        self._identities[char_a][char_b] = identity_label

    def get_relationship_desc(self, char_a: str, char_b: str) -> str:
        """返回软关系(好感度)和硬关系(身份)的综合描述"""
        score = self._relationships.get(char_a, {}).get(char_b, 0)
        identity = self._identities.get(char_a, {}).get(char_b, "")
        
        feel = "中立"
        if score <= -50: feel = "仇恨"
        elif score <= -20: feel = "厌恶"
        elif score >= 50: feel = "亲密"
        elif score >= 20: feel = "友好"
        
        if identity:
            return f"{identity}, {feel}"
        return feel

    def transfer_item(self, item_name: str, new_owner: str, new_location: str = "") -> None:
        """转移物品归属，如果 new_owner 为空，则表示遗落在 new_location"""
        if item_name not in self._items:
            self._items[item_name] = ItemState(name=item_name)
        
        self._items[item_name].owner = new_owner
        self._items[item_name].location = new_location if not new_owner else ""
        self._item_touch[item_name] = time.time()
        self._prune_items()

    def toggle_agent_ai(self, char_name: str, enabled: bool) -> bool:
        """切换指定角色的 AI 智能体状态"""
        with self._lock:
            agent = next((a for a in self._agents if a.name == char_name), None)
            if agent:
                agent.ai_enabled = enabled
                return True
            return False

    def run_turn(self, context_text: str) -> None:
        if not self._agents:
            return
        turn_started_at = time.perf_counter()
        llm_call_count = 0
        dm_call_count = 0
        dm_elapsed_ms = 0.0
        perf_lock = threading.Lock()
        ticks_to_run = self._estimate_elapsed_ticks(context_text)
        msg_lower = context_text.lower()
        now = time.time()
        with self._lock:
            for a in self._agents:
                if a.status in ["dead", "injured"]:
                    if a.name.lower() in msg_lower or "群体" in msg_lower or "所有人" in msg_lower:
                        if any(w in msg_lower for w in ["复活", "苏醒", "救活", "治愈", "恢复", "醒来"]):
                            a.status = "alive"
                            a.intent = "刚刚恢复"
                            a.journal.append(f"[Tick {self._last_tick}] 我恢复了状态。")
                            if len(a.journal) > 20:
                                a.journal.pop(0)
        dm_sys = """你是世界的主持人(DM)。请根据以上角色的当前意图和关系，推演他们在该地点发生的交互。
【核心约束】：
1. 角色互动必须符合常理和逻辑！如果他们互相不认识且没有冲突理由（比如只是都在街道休息），他们完全可能擦肩而过，互不打扰。
2. 绝对禁止为了互动而互动。如果没有合理的动机，直接在 digest 中写"各自安静待着"或"无事发生"，不要强行改变他们的关系。
3. 【严禁抢戏】：你只负责输出状态的变化，绝对禁止描写具体的对话内容和战斗招式。
必须返回严格的JSON格式：
{
  "digest": "一句话简报，不超过30字。必须客观中立，仅用主谓宾结构描述事实。",
  "relationship_changes": [{"source": "角色A", "target": "角色B", "delta": -20, "reason": "A攻击了B"}],
  "item_transfers": [{"item": "物品名", "new_owner": "角色名或空(表示掉落)", "reason": "抢夺/给予/遗落"}],
  "rumors_spread": [{"text": "流言内容", "expire_days": 2}],
  "status_changes": [{"name": "角色名", "new_status": "dead或injured或alive", "reason": "死因或伤因"}],
  "new_lore_entry": "",
  "character_updates": [],
  "scene_elapsed_ticks": 1
}
关于 rumors_spread：请按流言性质判断持续时间（单位天，1-7），紧急爆点可短，结构性秘密可长。
关于 status_changes：仅在角色确实死亡(dead)、重伤昏迷(injured)或从伤亡中恢复(alive)时填写，不要轻易判定死亡。"""
        turn_actor_elapsed_map: dict[str, float] = {}
        tick_idx = 0
        while tick_idx < ticks_to_run:
            with self._lock:
                self._last_tick += 1
                self.update_world_time()
                self._prune_expired_rumors()
                for a in self._agents:
                    if a.salience > 0:
                        a.salience = max(0, a.salience - 1)
                if self._last_tick % 10 == 0:
                    for src in list(self._relationships.keys()):
                        for tgt in list(self._relationships[src].keys()):
                            current_score = self._relationships[src][tgt]
                            if current_score > 0:
                                self._relationships[src][tgt] -= 1
                            elif current_score < 0:
                                self._relationships[src][tgt] += 1
                    self._prune_relationships()
                available_agents = [
                    a
                    for a in self._agents
                    if a.status == "alive"
                    and bool(a.ai_enabled)
                    and (a.name not in self._active_characters)
                    and int(self._agent_cooldown_until_tick.get(a.name, 0)) <= int(self._last_tick)
                ]
                if self._time_of_day in {"入夜", "深夜"} and len(available_agents) > 1:
                    reduced: list[AgentState] = []
                    for a in available_agents:
                        keep_prob = 0.28 if self._time_of_day == "深夜" else 0.45
                        if int(a.salience) >= 70:
                            keep_prob = min(1.0, keep_prob + 0.35)
                        if random.random() <= keep_prob:
                            reduced.append(a)
                    if reduced:
                        available_agents = reduced
                # 清理含系统提示词泄露的旧 journal 条目
                for a in available_agents:
                    cleaned = [e for e in a.journal
                               if "<content>" not in e and "<thinking>" not in e
                               and "并留意到制的正文" not in e and "以<thinking>作为开头" not in e
                               and "来源:heuristic" not in e and "来源:social" not in e]
                    if len(cleaned) != len(a.journal):
                        a.journal = cleaned
                actor_count = len(available_agents)
                if actor_count == 0:
                    tick_idx += 1
                    continue
                actors = sorted(available_agents, key=lambda x: (-x.salience, x.updated_at))[:actor_count]
                all_actor_names = [x.name for x in actors]
                recent_world = self._latest_digest
                actor_tasks: list[dict[str, Any]] = []
                for agent in actors:
                    nearby_agents = [a for a in self._agents if a.location == agent.location and a.name != agent.name and a.status == "alive"]
                    social_focus = self._allow_social_focus(agent, context_text, nearby_agents)
                    if not social_focus:
                        nearby_agents = []
                    nearby_names = [x.name for x in nearby_agents[:5]]
                    actor_tasks.append(
                        {
                            "name": agent.name,
                            "ai_enabled": bool(agent.ai_enabled),
                            "agent_ref": agent,
                            "nearby_agents": nearby_agents,
                            "nearby_names": nearby_names,
                            "recent_world": recent_world if social_focus else "世界平稳，无需与他人互动，优先处理自己的事务。",
                            "old_intent": agent.intent,
                            "old_location": agent.location,
                            "all_names": all_actor_names,
                        }
                    )
                self._turn_progress.update(
                    {
                        "state": "generating",
                        "total": len(actor_tasks),
                        "done": 0,
                        "pending": len(actor_tasks),
                        "remaining_names": [str(t.get("name", "")) for t in actor_tasks],
                        "failures": 0,
                        "failed_names": [],
                        "errors": [],
                        "started_at": self._turn_progress.get("started_at", time.time()),
                        "tick": int(self._last_tick),
                        "elapsed_ms": 0,
                        "llm_call_count": 0,
                        "dm_call_count": 0,
                        "slowest_actor": "",
                        "slowest_actor_ms": 0,
                        "dm_elapsed_ms": 0,
                    }
                )
            actor_results: dict[str, dict[str, Any]] = {}
            task_failures: dict[str, str] = {}

            def resolve_actor_task(task: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                nonlocal llm_call_count
                out: dict[str, Any] = {"intent": "", "location": "", "narrative": "", "thought": "", "actions": []}
                task_started = time.perf_counter()
                agent = task["agent_ref"]
                if not task["ai_enabled"] or not hasattr(self, "llm_client") or not self.llm_client:
                    out["_elapsed_ms"] = round((time.perf_counter() - task_started) * 1000.0, 2)
                    return (str(task["name"]), out)

                use_tavern = self._tavern_engine is not None

                if use_tavern:
                    # --- 新路径：TavernEngine.step_for_agent() ---
                    # 背景 NPC 不传 context_text，过滤对话角色名相关的世界书/规则条目
                    _active_names = set(self._active_characters) if self._active_characters else set()
                    world_info = self._build_agent_world_info(agent, task["nearby_names"], "",
                                                              exclude_char_names=_active_names)
                    scenario = self._build_agent_scenario(agent, task["nearby_agents"])
                    # 背景 NPC 不应收到对话角色的完整对话（会导致身份混淆），只用世界摘要
                    narrator_input = self._build_narrator_input(agent, task["recent_world"], "")

                    hm = self._get_agent_history_manager(agent.name, load_if_empty=True)
                    session_id = f"agent_{agent.name}"

                    # 获取完整角色卡
                    card = agent.full_card or self._build_card_from_profile(agent.name)

                    result = self._tavern_engine.step_for_agent(
                        character_card=card,
                        user_name="旁白",
                        user_input=narrator_input,
                        scenario_context=scenario,
                        world_info=world_info,
                        session_id=session_id,
                        llm_caller=lambda p, _name=agent.name: self._agent_llm_caller(p, agent_name=_name),
                    )
                    with perf_lock:
                        llm_call_count += 1

                    narrative = result.get("text", "")
                    actions = result.get("actions", [])
                    intent, location = self._parse_intent_from_narrative(agent, narrative, actions)

                    self._save_agent_history_rows(agent.name, hm.to_list()[-self.max_agent_history:])

                    out["intent"] = intent
                    out["location"] = location
                    out["narrative"] = narrative[:500]
                    out["actions"] = actions
                else:
                    # --- 回退路径：旧 generate_intent 流程 ---
                    loc_for_story = agent.location if agent.location not in {"未知地点", "当前场景"} else "当前位置未明确"
                    perception = f"当前时间: {self.get_world_time_str()} (Tick {self._last_tick})。你在[{loc_for_story}]。最近世界发生了: {task['recent_world']}。"
                    sys_prompt = (
                        f"你扮演角色[{agent.name}]。请严格依据世界设定决定下一步行动。"
                        "必须返回JSON对象：{\"intent\":\"简短意图\",\"location\":\"目标地点\","
                        "\"narrative\":\"不超过500字剧情\",\"thought\":\"内心念头\"}。"
                    )
                    res = self.llm_client.generate_intent(sys_prompt, perception)
                    with perf_lock:
                        llm_call_count += 1
                    if isinstance(res, dict):
                        out["intent"] = str(res.get("intent", "")).strip()
                        out["location"] = str(res.get("location", "")).strip()
                        out["narrative"] = str(res.get("narrative", "")).strip()[:500]
                        out["thought"] = str(res.get("thought", "")).strip()[:200]

                # 后处理：narrative 主语修正
                nv = str(out.get("narrative", "")).strip()
                if nv:
                    others = [x for x in task.get("all_names", []) if x and x != task["name"]]
                    for x in others:
                        if nv.startswith(f"我{x}"):
                            nv = "我" + nv[len(f"我{x}"):]
                        elif nv.startswith(x):
                            nv = "我" + nv[len(x):]
                    nv = nv.replace("中立地待机", "短暂停下观察").replace("处于待机状态", "短暂停下观察")
                    out["narrative"] = self._normalize_unknown_location_narrative(nv, context_text)
                out["_elapsed_ms"] = round((time.perf_counter() - task_started) * 1000.0, 2)
                return (str(task["name"]), out)

            if actor_tasks:
                worker_n = min(4, max(1, len(actor_tasks)))
                with ThreadPoolExecutor(max_workers=worker_n) as ex:
                    future_to_name = {ex.submit(resolve_actor_task, task): str(task.get("name", "")) for task in actor_tasks}
                    for fut in as_completed(future_to_name):
                        task_name = future_to_name.get(fut, "")
                        try:
                            name, out = fut.result()
                            actor_results[name] = out
                            turn_actor_elapsed_map[name] = max(
                                float(turn_actor_elapsed_map.get(name, 0.0)),
                                float(out.get("_elapsed_ms", 0.0) or 0.0),
                            )
                            with self._lock:
                                remain = [x for x in self._turn_progress.get("remaining_names", []) if x != name]
                                self._turn_progress["remaining_names"] = remain
                                self._turn_progress["done"] = int(self._turn_progress.get("done", 0)) + 1
                                self._turn_progress["pending"] = max(0, int(self._turn_progress.get("total", 0)) - int(self._turn_progress.get("done", 0)))
                        except Exception as e:
                            err_msg = str(e)[:160] if str(e) else "unknown_error"
                            task_failures[task_name] = err_msg
                            with self._lock:
                                remain = [x for x in self._turn_progress.get("remaining_names", []) if x != task_name]
                                self._turn_progress["remaining_names"] = remain
                                self._turn_progress["done"] = int(self._turn_progress.get("done", 0)) + 1
                                self._turn_progress["pending"] = max(0, int(self._turn_progress.get("total", 0)) - int(self._turn_progress.get("done", 0)))
                                failed_names = [str(x) for x in self._turn_progress.get("failed_names", []) if str(x).strip()]
                                if task_name:
                                    failed_names.append(task_name)
                                self._turn_progress["failed_names"] = failed_names[-32:]
                                self._turn_progress["failures"] = len(self._turn_progress["failed_names"])
                                errs = [str(x) for x in self._turn_progress.get("errors", []) if str(x).strip()]
                                if task_name:
                                    errs.append(f"{task_name}: {err_msg}")
                                else:
                                    errs.append(err_msg)
                                self._turn_progress["errors"] = errs[-8:]
                            logger.warning(f"turn_actor_failed: {task_name} {err_msg}")
            mem_rows: list[dict[str, Any]] = []
            pending_actor_rows: list[dict[str, Any]] = []
            with self._lock:
                for task in actor_tasks:
                    agent = next((a for a in self._agents if a.name == task["name"]), None)
                    if not agent:
                        continue
                    fail_msg = str(task_failures.get(task["name"], "")).strip()
                    if fail_msg:
                        streak = int(self._agent_turn_fail_streak.get(agent.name, 0)) + 1
                        self._agent_turn_fail_streak[agent.name] = streak
                        self._agent_last_turn_error[agent.name] = fail_msg
                        agent.salience = max(0, int(agent.salience) - 12)
                        fail_entry = f"[Tick {self._last_tick}] 推演失败：{fail_msg}"
                        if not agent.journal or agent.journal[-1] != fail_entry:
                            agent.journal.append(fail_entry)
                            if len(agent.journal) > 20:
                                agent.journal.pop(0)
                        if streak >= 3:
                            self._agent_cooldown_until_tick[agent.name] = int(self._last_tick) + 3
                            self._agent_turn_fail_streak[agent.name] = 0
                            cool_entry = f"[Tick {self._last_tick}] 连续失败，临时退出推演池至 Tick {int(self._last_tick) + 3}"
                            if not agent.journal or agent.journal[-1] != cool_entry:
                                agent.journal.append(cool_entry)
                                if len(agent.journal) > 20:
                                    agent.journal.pop(0)
                        continue
                    self._agent_turn_fail_streak[agent.name] = 0
                    self._agent_last_turn_error.pop(agent.name, None)
                    out = actor_results.get(task["name"], {})
                    new_intent = str(out.get("intent", "")).strip()
                    new_loc = str(out.get("location", "")).strip()
                    new_narrative = str(out.get("narrative", "")).strip()
                    new_thought = str(out.get("thought", "")).strip()
                    if new_intent:
                        agent.intent = new_intent
                    if new_loc and new_loc in self._locations:
                        agent.location = new_loc
                    agent.updated_at = now
                    agent.salience = min(100, max(0, agent.salience + random.randint(-2, 7)))
                    if new_narrative:
                        new_narrative = re.sub(r"\s*\[位置[：:][^\]]*\]", "", new_narrative)
                        new_narrative = re.sub(r"\s*\[意图[：:][^\]]*\]", "", new_narrative)
                        new_narrative = self._normalize_unknown_location_narrative(new_narrative.strip(), context_text)
                    pending_actor_rows.append(
                        {
                            "name": agent.name,
                            "declared_narrative": new_narrative,
                            "thought": new_thought,
                        }
                    )
                # --- 构建 DM 待裁定队列 ---
                _INTERACTIVE_INTENTS = {"战斗", "偷窃", "交谈", "交易", "帮助", "跟踪", "威胁", "互动", "转场"}
                dm_queue: list[tuple[str, list[AgentState]]] = []
                if hasattr(self, "llm_client") and self.llm_client:
                    current_available = [a for a in self._agents if a.status == "alive" and (a.name not in self._active_characters or a.intent == "新出现")]
                    loc_groups: dict[str, list[AgentState]] = {}
                    for a in current_available:
                        if a.location in {"未知地点", "当前场景"}:
                            continue
                        loc_groups.setdefault(a.location, []).append(a)
                    hot_spots = {loc: ags for loc, ags in loc_groups.items() if len(ags) >= 2}
                    for _hs_loc, _hs_ags in hot_spots.items():
                        has_interaction = any(str(a.intent).strip() in _INTERACTIVE_INTENTS for a in _hs_ags)
                        if has_interaction:
                            dm_queue.append((_hs_loc, _hs_ags))
                        elif random.random() < 0.3:
                            dm_queue.append((_hs_loc, _hs_ags))
                    dm_queue.sort(key=lambda x: -len(x[1]))
            # -----------------------------------------------------------
            # DM 裁定：对 dm_queue 中所有热点逐个调用 LLM（锁外）
            # -----------------------------------------------------------
            dm_results: list[dict[str, Any]] = []
            dm_max_scene_ticks = 0
            dm_digest_parts: list[str] = []
            dm_digest_by_loc: dict[str, str] = {}
            if dm_queue and hasattr(self, "llm_client") and self.llm_client:
                for _dm_loc, _dm_group in dm_queue[:3]:
                    _dm_scenario = build_dm_scenario(
                        _dm_loc, _dm_group, self._identities, self._items,
                        relationships=self._relationships,
                        actor_results=actor_results,
                    )
                    _dm_payload = {"target_loc": _dm_loc, "group_names": [x.name for x in _dm_group], "scenario": _dm_scenario}
                    dm_called_at = time.perf_counter()
                    raw_dm = self.llm_client.adjudicate_interaction_structured(dm_sys, _dm_scenario)
                    dm_cost_ms = round((time.perf_counter() - dm_called_at) * 1000.0, 2)
                    with perf_lock:
                        dm_call_count += 1
                        dm_elapsed_ms += dm_cost_ms
                    dm_results.append(
                        {
                            "payload": _dm_payload,
                            "scenario": _dm_scenario,
                            "raw_response": raw_dm,
                            "elapsed_ms": dm_cost_ms,
                            "followup_dialogue": [],
                        }
                    )
                    if isinstance(raw_dm, dict):
                        dm_digest = str(raw_dm.get("digest", "")).strip()
                        followup_dialogue = self._build_dm_followup_dialogue(_dm_loc, dm_digest, _dm_group, actor_results)
                        if followup_dialogue:
                            with perf_lock:
                                llm_call_count += len(followup_dialogue)
                            dm_results[-1]["followup_dialogue"] = followup_dialogue

            # -----------------------------------------------------------
            # DM 结果应用（锁内）
            # -----------------------------------------------------------
            with self._lock:
                for dm_row in dm_results:
                    dm_payload = dm_row.get("payload", {}) if isinstance(dm_row, dict) else {}
                    dm_result_json = dm_row.get("raw_response", {}) if isinstance(dm_row, dict) else {}
                    applied_changes = {
                        "character_updates": 0,
                        "rumors_added": 0,
                        "status_changes": 0,
                        "relationship_changes": 0,
                        "item_transfers": 0,
                        "new_lore_entry": False,
                        "dialogue_replies": 0,
                    }
                    target_loc = str(dm_payload.get("target_loc", ""))
                    group_names = list(dm_payload.get("group_names", []))
                    group_name_set = set(group_names)
                    if not isinstance(dm_result_json, dict):
                        self._record_dm_adjudication(
                            {
                                "tick": int(self._last_tick),
                                "target_loc": target_loc,
                                "group_names": group_names,
                                "scenario": str(dm_payload.get("scenario", ""))[:3000],
                                "raw_response": dm_result_json,
                                "elapsed_ms": float(dm_row.get("elapsed_ms", 0.0)),
                                "applied_changes": applied_changes,
                            }
                        )
                        continue
                    dm_text = str(dm_result_json.get("digest", ""))
                    try:
                        scene_ticks = max(1, min(24, int(dm_result_json.get("scene_elapsed_ticks", 0) or 0)))
                    except Exception:
                        scene_ticks = 0
                    if scene_ticks > dm_max_scene_ticks:
                        dm_max_scene_ticks = scene_ticks
                    if dm_text:
                        dm_digest_parts.append(f"【DM裁决】{target_loc}：{dm_text}")
                        dm_digest_by_loc[target_loc] = dm_text

                    # --- character_updates ---
                    char_updates = dm_result_json.get("character_updates", [])
                    if isinstance(char_updates, list):
                        for cu in char_updates:
                            if not isinstance(cu, dict):
                                continue
                            old_n = str(cu.get("old_name", ""))
                            new_n = str(cu.get("new_name", ""))
                            new_summary = str(cu.get("summary", ""))
                            if old_n:
                                target_agent = next((a for a in self._agents if a.name == old_n), None)
                                if target_agent:
                                    if new_n and new_n != old_n:
                                        self.rename_entity(old_n, new_n)
                                        target_agent = next((a for a in self._agents if a.name == new_n), None)
                                        if getattr(self, "dynamic_lore_writeback", False):
                                            self._writeback_character_update_to_tavern(old_n, new_n, new_summary)
                                    if target_agent and new_summary:
                                        target_agent.summary = new_summary
                                    applied_changes["character_updates"] = int(applied_changes["character_updates"]) + 1

                    # --- rumors_spread ---
                    new_rumors = dm_result_json.get("rumors_spread", [])
                    if isinstance(new_rumors, list) and new_rumors:
                        for rumor_row in new_rumors:
                            self._append_rumor(rumor_row, allow_model_ttl=bool(self._rumor_ttl_by_llm))
                            applied_changes["rumors_added"] = int(applied_changes["rumors_added"]) + 1

                    # --- status_changes（结构化，优先） ---
                    status_changes = dm_result_json.get("status_changes", [])
                    _status_handled_names: set[str] = set()
                    if isinstance(status_changes, list):
                        for sc in status_changes:
                            if not isinstance(sc, dict):
                                continue
                            sc_name = str(sc.get("name", "")).strip()
                            sc_status = str(sc.get("new_status", "")).strip().lower()
                            sc_agent = next((a for a in self._agents if a.name == sc_name), None)
                            if not sc_agent or sc_name not in group_name_set:
                                continue
                            _status_handled_names.add(sc_name)
                            if sc_status == "dead":
                                sc_agent.status = "dead"
                                sc_agent.intent = "已死亡"
                                for i_name, i_state in self._items.items():
                                    if i_state.owner == sc_agent.name:
                                        self.transfer_item(i_name, "", target_loc)
                                self._evict_agent_history_manager(sc_name)
                            elif sc_status == "injured":
                                sc_agent.status = "injured"
                                sc_agent.intent = "休养中"
                            elif sc_status == "alive" and sc_agent.status in {"injured", "dead"}:
                                sc_agent.status = "alive"
                                sc_agent.intent = "刚刚恢复"
                            applied_changes["status_changes"] = int(applied_changes["status_changes"]) + 1

                    # --- journal + 关键词兜底死亡检测 ---
                    if dm_text:
                        for name in group_names:
                            a = next((x for x in self._agents if x.name == name), None)
                            if not a:
                                continue
                            dm_journal_entry = f"[Tick {self._last_tick} 遭遇] {dm_text}"
                            a.journal.append(dm_journal_entry)
                            if len(a.journal) > 20:
                                a.journal.pop(0)
                            if self._tavern_engine:
                                hm = self._get_agent_history_manager(a.name, load_if_empty=True)
                                hm.add_message("user", f"[旁白] {dm_text}", "旁白")
                                self._save_agent_history_rows(a.name, hm.to_list()[-self.max_agent_history:])
                            # 关键词兜底（仅未被 status_changes 处理的角色）
                            if a.name not in _status_handled_names and a.name in dm_text:
                                if "死亡" in dm_text or "击杀" in dm_text:
                                    a.status = "dead"
                                    a.intent = "已死亡"
                                    for i_name, i_state in self._items.items():
                                        if i_state.owner == a.name:
                                            self.transfer_item(i_name, "", target_loc)
                                    self._evict_agent_history_manager(a.name)
                                elif "重伤" in dm_text or "昏迷" in dm_text:
                                    a.status = "injured"
                                    a.intent = "休养中"
                        danger_words = ["攻击", "破门", "爆炸", "死亡", "刺杀", "重伤", "突袭", "火灾", "尖叫"]
                        if any(w in dm_text for w in danger_words):
                            self._active_hook = f"【系统最高指令：请立刻描写以下突发事件打断玩家当前的行动：{dm_text}】"

                    # --- DM 后续对话轮 ---
                    followup_dialogue = dm_row.get("followup_dialogue", []) if isinstance(dm_row, dict) else []
                    if isinstance(followup_dialogue, list) and followup_dialogue:
                        for dr in followup_dialogue:
                            if not isinstance(dr, dict):
                                continue
                            speaker = str(dr.get("speaker", "")).strip()
                            reply = str(dr.get("reply", "")).strip()
                            if not speaker or not reply:
                                continue
                            a = next((x for x in self._agents if x.name == speaker), None)
                            if not a:
                                continue
                            dline = f"[Tick {self._last_tick} 对话] {a.name}：{reply}"
                            if not a.journal or a.journal[-1] != dline:
                                a.journal.append(dline)
                                if len(a.journal) > 20:
                                    a.journal.pop(0)
                            if self._tavern_engine:
                                hm = self._get_agent_history_manager(a.name, load_if_empty=True)
                                hm.add_message("assistant", reply, a.name)
                                self._save_agent_history_rows(a.name, hm.to_list()[-self.max_agent_history:])
                            applied_changes["dialogue_replies"] = int(applied_changes["dialogue_replies"]) + 1

                    # --- relationship_changes ---
                    rel_changes = dm_result_json.get("relationship_changes", [])
                    if isinstance(rel_changes, list):
                        for rc in rel_changes:
                            src = rc.get("source")
                            tgt = rc.get("target")
                            delta = rc.get("delta", 0)
                            src_agent = next((a for a in self._agents if a.name == src), None)
                            tgt_agent = next((a for a in self._agents if a.name == tgt), None)
                            if src_agent and src_agent.status != "alive":
                                continue
                            if tgt_agent and tgt_agent.status != "alive":
                                continue
                            if src and tgt and isinstance(delta, (int, float)) and delta != 0:
                                if not self._allow_relationship_change(str(src), str(tgt), int(delta), dm_text, str(dm_payload.get("scenario", ""))):
                                    continue
                                self.update_relationship(src, tgt, max(-15, min(15, int(delta))))
                                applied_changes["relationship_changes"] = int(applied_changes["relationship_changes"]) + 1

                    # --- item_transfers（含来源校验） ---
                    item_changes = dm_result_json.get("item_transfers", [])
                    if isinstance(item_changes, list):
                        for ic in item_changes:
                            item_name = ic.get("item")
                            new_owner = ic.get("new_owner", "")
                            if not item_name:
                                continue
                            if item_name not in self._items:
                                continue  # 物品不存在，不凭空创建
                            current_item_owner = self._items[item_name].owner
                            if current_item_owner and current_item_owner not in group_name_set:
                                continue  # 不能转移不在场角色的物品
                            if new_owner:
                                new_owner_agent = next((a for a in self._agents if a.name == new_owner), None)
                                if new_owner_agent and new_owner_agent.status != "alive":
                                    continue
                            self.transfer_item(item_name, new_owner, target_loc)
                            applied_changes["item_transfers"] = int(applied_changes["item_transfers"]) + 1

                    # --- new_lore_entry ---
                    new_lore = dm_result_json.get("new_lore_entry", "")
                    if new_lore and isinstance(new_lore, str):
                        self._world_rule_summary += f"；{new_lore}"
                        applied_changes["new_lore_entry"] = True
                        if self.dynamic_lore_writeback and self._tavern_world_file:
                            try:
                                wf_path = Path(self._tavern_world_file)
                                if wf_path.exists():
                                    with open(wf_path, "r", encoding="utf-8") as f:
                                        wf_data = json.load(f)
                                    entries = wf_data.get("entries", {})
                                    if isinstance(entries, dict):
                                        new_id = str(len(entries) + 1)
                                        entries[new_id] = {"uid": int(time.time()), "key": ["Omnis推演纪事"], "keysecondary": [], "comment": "动态生长的世界设定", "content": new_lore, "constant": False, "selective": True, "insertionorder": 50, "enabled": True, "position": 1}
                                        wf_data["entries"] = entries
                                        self._atomic_write_json(wf_path, wf_data, indent=4)
                            except Exception as e:
                                logger.error(f"回写世界书失败: {e}")
                    self._record_dm_adjudication(
                        {
                            "tick": int(self._last_tick),
                            "target_loc": target_loc,
                            "group_names": group_names,
                            "scenario": str(dm_payload.get("scenario", ""))[:3000],
                            "raw_response": dm_result_json,
                            "elapsed_ms": float(dm_row.get("elapsed_ms", 0.0)),
                            "applied_changes": applied_changes,
                        }
                    )

                # --- 汇总 digest ---
                ordered = sorted([a for a in self._agents if a.status == "alive"], key=lambda x: (x.salience, x.updated_at), reverse=True)
                top = ordered[:4]
                digest_lines = [f"{a.name}在{a.location}，当前意图：{a.intent}" for a in top]
                if dm_digest_parts:
                    for dp in dm_digest_parts:
                        digest_lines.insert(0, dp)
                self._latest_digest = "；".join(digest_lines)
            if pending_actor_rows:
                render_rows: list[dict[str, Any]] = []
                with self._lock:
                    for row in pending_actor_rows:
                        a = next((x for x in self._agents if x.name == row["name"]), None)
                        if not a:
                            continue
                        render_rows.append(
                            {
                                "name": a.name,
                                "status": a.status,
                                "location": a.location,
                                "intent": a.intent,
                                "declared_narrative": row.get("declared_narrative", ""),
                                "thought": row.get("thought", ""),
                            }
                        )
                rendered: list[dict[str, str]] = []
                for row in render_rows:
                    agent_obj = AgentState(
                        name=row["name"],
                        location=row["location"],
                        intent=row["intent"],
                        status=row["status"],
                    )
                    dm_hint = str(dm_digest_by_loc.get(str(row["location"]), ""))
                    final_nv = self._render_post_adjudication_narrative(agent_obj, str(row.get("declared_narrative", "")), dm_hint)
                    if dm_hint and final_nv:
                        with perf_lock:
                            llm_call_count += 1
                    rendered.append(
                        {
                            "name": row["name"],
                            "final_narrative": final_nv,
                            "thought": str(row.get("thought", "")),
                        }
                    )
                with self._lock:
                    for rr in rendered:
                        a = next((x for x in self._agents if x.name == rr["name"]), None)
                        if not a:
                            continue
                        final_nv = str(rr.get("final_narrative", "")).strip()
                        if not final_nv:
                            continue
                        thought_suffix = f" 内心：{rr.get('thought', '')}" if str(rr.get("thought", "")).strip() else ""
                        journal_entry = f"[Tick {self._last_tick}] {final_nv}{thought_suffix}"
                        if not a.journal or a.journal[-1] != journal_entry:
                            a.journal.append(journal_entry)
                            if len(a.journal) > 20:
                                a.journal.pop(0)
                            mem_rows.append(
                                {
                                    "agent_name": a.name,
                                    "tick": self._last_tick,
                                    "location": a.location,
                                    "intent": a.intent,
                                    "content": journal_entry,
                                }
                            )
            if get_episodic_memory and mem_rows:
                mem_db = get_episodic_memory()
                for row in mem_rows:
                    mem_db.add_memory(session_id=self.session_id, agent_name=row["agent_name"], tick=row["tick"], location=row["location"], intent=row["intent"], content=row["content"])
            if dm_max_scene_ticks > 0:
                ticks_to_run = max(ticks_to_run, dm_max_scene_ticks)
            tick_idx += 1
        elapsed_ms = int((time.perf_counter() - turn_started_at) * 1000.0)
        slowest_actor = ""
        slowest_actor_ms = 0.0
        if turn_actor_elapsed_map:
            slowest_actor, slowest_actor_ms = max(turn_actor_elapsed_map.items(), key=lambda kv: float(kv[1]))
        with self._lock:
            self._turn_progress["elapsed_ms"] = elapsed_ms
            self._turn_progress["llm_call_count"] = int(llm_call_count)
            self._turn_progress["dm_call_count"] = int(dm_call_count)
            self._turn_progress["dm_elapsed_ms"] = round(float(dm_elapsed_ms), 2)
            self._turn_progress["slowest_actor"] = str(slowest_actor)
            self._turn_progress["slowest_actor_ms"] = round(float(slowest_actor_ms), 2)
        self.save_turn_snapshot(context_text, reason="turn_committed")

    def build_shadow_lore(self, user_messages: list[dict[str, Any]], include_far: bool = True) -> str:
        with self._lock:
            current_tick = self._last_tick
            digest = self._latest_digest
            agents = [AgentState(a.name, a.location, a.intent, a.salience, a.updated_at, list(a.journal), a.status, a.ai_enabled, a.summary, a.card_description, a.card_personality, a.card_scenario, None, None) for a in self._agents]
            
            # 取出并清空主动钩子
            hook_text = self._active_hook
            self._active_hook = ""
            
            rumor_candidates = self._rumor_texts()
            rumor_text = ""
            if rumor_candidates and random.random() < 0.4:
                rumor = random.choice(rumor_candidates)
                rumor_text = f"- 【可选情报】如果你觉得当前聊天氛围合适，可以通过不经意的闲聊向玩家透露以下流言：\u201c{rumor}\u201d。如果氛围不合适或你很警惕，请绝对不要主动提及。"
                
        player_hint = self._extract_location_hint(user_messages)
        near_lines: list[str] = []
        if player_hint:
            near = [a for a in agents if player_hint in a.location]
            near_sorted = sorted(near, key=lambda x: (x.salience, x.updated_at), reverse=True)[:3]
            near_lines = [
                f"{a.name}正在{a.location}附近活动，意图：{a.intent}，最近动态：{self._compress_text(a.journal[-1], 90) if a.journal else '暂无'}"
                for a in near_sorted
            ]
        far_active = [a for a in agents if not player_hint or player_hint not in a.location]
        far_active_count = len(far_active)
        top_far_intents = sorted(far_active, key=lambda x: (x.salience, x.updated_at), reverse=True)[:3]
        far_intent_summary = "；".join([f"{x.location}:{x.intent}" for x in top_far_intents]) if top_far_intents else "暂无"
        
        blocks = []
        if hook_text:
            blocks.append(hook_text)
            
        blocks.extend([
            "【动态世界事实，仅用于模型推理，不得原样复读给玩家】",
            f"- 规则优先级: {'酒馆世界书规则优先；' + self._world_rule_summary if self._world_rule_summary else '酒馆世界书规则优先'}",
            f"- 后台世界Tick: {current_tick}",
            f"- 热点摘要: {digest or '暂无显著变化'}",
            f"- 近场角色: {'；'.join(near_lines) if near_lines else '暂无'}",
            "- 输出要求: 仅将上述事实转化为场景氛围与可感知线索，禁止直接列举未出场角色姓名或全图清单。",
        ])
        
        if rumor_text:
            blocks.insert(-1, rumor_text)
        if include_far:
            blocks.insert(-1, f"- 远场活跃数量: {far_active_count}")
            blocks.insert(-1, f"- 远场态势摘要: {far_intent_summary}")
        else:
            blocks.insert(-1, "- 远场信息: 已因脚本保护降级，仅保留近场事实")
        lore_text = "\n".join(blocks)
        with self._lock:
            self._record_injection("shadow_lore", lore_text)
        return lore_text

    def status(self) -> dict[str, Any]:
        with self._lock:
            agent_details = []
            for a in sorted(self._agents, key=lambda x: x.salience, reverse=True):
                # 收集该角色的物品
                my_items = [i.name for i in self._items.values() if i.owner == a.name]
                
                # 收集该角色的关系 (他看别人，别人看他)
                my_rels = []
                # 他对别人的看法
                if a.name in self._relationships:
                    for tgt, score in self._relationships[a.name].items():
                        if score != 0:
                            my_rels.append({"target": tgt, "score": score, "desc": self.get_relationship_desc(a.name, tgt), "dir": "out"})
                # 别人对他的看法
                for src, targets in self._relationships.items():
                    if a.name in targets and targets[a.name] != 0:
                        my_rels.append({"target": src, "score": targets[a.name], "desc": self.get_relationship_desc(src, a.name), "dir": "in"})

                agent_details.append({
                    "name": a.name,
                    "location": a.location,
                    "intent": a.intent,
                    "salience": a.salience,
                    "updated_at": a.updated_at,
                    "status": a.status,
                    "summary": a.summary,
                    "journal": a.journal,
                    "items": my_items,
                    "relationships": my_rels,
                    "ai_enabled": a.ai_enabled,
                    "card_description": a.card_description,
                    "card_personality": a.card_personality,
                    "card_scenario": a.card_scenario,
                    "turn_fail_streak": int(self._agent_turn_fail_streak.get(a.name, 0)),
                    "turn_cooldown_until_tick": int(self._agent_cooldown_until_tick.get(a.name, 0)),
                    "last_turn_error": str(self._agent_last_turn_error.get(a.name, "")),
                })
            
            formatted_relationships = []
            for src, targets in self._relationships.items():
                for tgt, score in targets.items():
                    if score != 0:
                        formatted_relationships.append({
                            "source": src,
                            "target": tgt,
                            "score": score,
                            "desc": self.get_relationship_desc(src, tgt)
                        })
                        
            formatted_items = []
            for item_name, item_state in self._items.items():
                formatted_items.append({
                    "name": item_name,
                    "owner": item_state.owner,
                    "location": item_state.location
                })
                        
            return {
                "session_id": self.session_id,
                "active_characters": self._active_characters,
                "possessed_character": self._possessed_character,
                "tick": self._last_tick,
                "world_time": self.get_world_time_str(),
                "active_rumors": self._rumor_texts(),
                "active_rumors_meta": [
                    {
                        "text": str(x.get("text", "")).strip(),
                        "created_at_tick": int(x.get("created_at_tick", 0)),
                        "expire_at_tick": int(x.get("expire_at_tick", 0)),
                        "remaining_ticks": max(0, int(x.get("expire_at_tick", 0)) - int(self._last_tick)),
                    }
                    for x in self._active_rumors
                    if isinstance(x, dict) and str(x.get("text", "")).strip()
                ],
                "rumor_default_ttl_ticks": int(self._rumor_default_ttl_ticks),
                "rumor_ttl_by_llm": bool(self._rumor_ttl_by_llm),
                "digest": self._latest_digest,
                "agents": len([a for a in self._agents if a.status == "alive"]),
                "agent_details": agent_details,
                "relationships": formatted_relationships,
                "items": formatted_items,
                "source": self._source,
                "world_rule_summary": self._world_rule_summary,
                "world_rule_map": self._world_rule_map,
                "worldbook_entry_count": len(self._worldbook_entries),
                "character_profile_count": len(self._card_profile_map),
                "state_snapshot_count": len(self._state_snapshots),
                "state_cursor_hash": self._state_cursor_hash,
                "active_extract_report": self._last_active_extract_report,
                "recent_extract_logs": self._recent_extract_logs,
                "intent_enum_labels": self._intent_enum_labels,
                "enqueue_report": self._last_enqueue_report,
                "turn_progress": dict(self._turn_progress),
                "turn_inflight": bool(self._turn_inflight),
                "script_guard_enabled": self._script_guard_enabled,
                "script_guard_action": self._script_guard_action,
                "rule_template_file": self._rule_template_file,
                "rule_templates": self._rule_templates,
                "tavern_user_dir": self._tavern_user_dir,
                "tavern_world_file": self._tavern_world_file,
                "llm_api_base": self.llm_client.api_base if self.llm_client else "",
                "llm_model": self.llm_client.model if self.llm_client else "",
                "max_journal_limit": self.max_journal_limit,
                "dynamic_lore_writeback": getattr(self, "dynamic_lore_writeback", False),
                "strict_background_independent": bool(self._strict_background_independent),
                "wait_effective_scope": self._last_wait_effective_scope,
                "wait_scope_degrade_until": float(self._wait_scope_degrade_until),
                "captured_preset_count": len(self._captured_preset_messages),
                "captured_llm_params": dict(self._captured_llm_params) if self._captured_llm_params else {},
                "recent_patch_debug": self._recent_debug_packets[-3:] if self._recent_debug_packets else [],
                "recent_injections": self._recent_injections[-5:] if self._recent_injections else [],
                "recent_dm_adjudications": self._recent_dm_adjudications[-5:] if self._recent_dm_adjudications else [],
                "active_story_debug": getattr(self, "_last_active_story_debug", []),
            }

    def update_llm_config(self, api_base: str, api_key: str, model: str, max_journal_limit: int = 3, dynamic_lore_writeback: bool = False) -> None:
        with self._lock:
            self.max_journal_limit = max_journal_limit
            self.dynamic_lore_writeback = dynamic_lore_writeback
            if not OmnisLLMClient:
                return
            if not api_base and not model:
                self.llm_client = None
                return
            self.llm_client = OmnisLLMClient(api_base=api_base, api_key=api_key, model=model)

    def trigger_flashback(self, char_name: str) -> dict[str, Any]:
        """为角色生成记忆闪回，并注入到下一个系统钩子中"""
        with self._lock:
            agent = next((a for a in self._agents if a.name == char_name), None)
            if not agent:
                return {"ok": False, "error": f"未找到角色: {char_name}"}
            
            # 如果没有记忆，直接返回
            if not agent.journal:
                return {"ok": False, "error": "该角色暂无任何记忆，无法闪回。"}
                
            # 提取最近的记忆片段
            recent_mems = agent.journal[-5:] if len(agent.journal) > 5 else agent.journal
            mems_text = "\n".join(recent_mems)
            
            if not OmnisLLMClient or not hasattr(self, "llm_client") or not self.llm_client:
                return {"ok": False, "error": "LLM 客户端未配置，无法生成闪回文本。"}
                
            sys_prompt = f"你是一个小说旁白。玩家即将与角色[{char_name}]交谈。请根据以下角色的近期经历，用一段极具小说感、沉浸感的话，总结角色的心境或遭遇，作为给玩家的背景提示。不超过80字。"
            
            # 同步调用 LLM 生成闪回文本
            flashback_text = self.llm_client.generate_intent(sys_prompt, mems_text)
            if isinstance(flashback_text, dict) and "intent" in flashback_text:
                flashback_text = flashback_text["intent"]
            elif isinstance(flashback_text, str):
                pass
            else:
                return {"ok": False, "error": "生成闪回文本失败"}
                
            # 包装为系统打断钩子
            self._active_hook = f"【系统提示：一段记忆闪回】 {flashback_text} （你可以根据此心境来回应玩家）"
            
            return {"ok": True, "flashback": flashback_text}

    def rename_entity(self, old_name: str, new_name: str) -> dict[str, Any]:
        """重命名实体（如从称呼更新为真实名字）"""
        result = {"ok": False, "msg": "", "old_name": old_name, "new_name": new_name}
        with self._lock:
            old_name = old_name.strip()
            new_name = new_name.strip()
            if not old_name or not new_name:
                result["msg"] = "名称不能为空"
                return result
                
            agent = next((a for a in self._agents if a.name == old_name), None)
            if not agent:
                result["msg"] = f"未找到名为 {old_name} 的角色"
                return result
                
            if any(a.name == new_name for a in self._agents):
                result["msg"] = f"已存在名为 {new_name} 的角色，无法重命名"
                return result
                
            # 更新基本信息
            agent.name = new_name
            
            # 更新 active_characters
            if old_name in self._active_characters:
                self._active_characters.remove(old_name)
                self._active_characters.append(new_name)
                
            # 更新魂穿目标
            if self._possessed_character == old_name:
                self._possessed_character = new_name
                
            # 更新关系图谱
            if old_name in self._relationships:
                self._relationships[new_name] = self._relationships.pop(old_name)
            for src, targets in self._relationships.items():
                if old_name in targets:
                    targets[new_name] = targets.pop(old_name)
            if old_name in self._card_profile_map:
                self._card_profile_map[new_name] = self._card_profile_map.pop(old_name)
                    
            # 更新物品所有者
            for item in self._items.values():
                if item.owner == old_name:
                    item.owner = new_name
                    
            result["ok"] = True
            result["msg"] = f"成功将 {old_name} 重命名为 {new_name}"
            return result
    def force_extract_entity(self, entity_type: str, entity_name: str) -> dict[str, Any]:
        """强制添加地点或人物实体，作为系统未提取出的补充"""
        result = {"ok": False, "msg": "", "type": entity_type, "name": entity_name}
        with self._lock:
            entity_name = entity_name.strip()
            if not entity_name:
                result["msg"] = "实体名称不能为空"
                return result

            if entity_type == "location":
                if entity_name in self._locations:
                    result["msg"] = f"地点 [{entity_name}] 已存在"
                    return result
                # 可以选择对世界书进行二次校验或直接添加
                self._locations.append(entity_name)
                result["ok"] = True
                result["msg"] = f"成功补充地点：{entity_name}"
                
            elif entity_type == "character":
                if any(a.name == entity_name for a in self._agents):
                    result["msg"] = f"人物 [{entity_name}] 已存在"
                    return result
                
                # 创建新的 AgentState，随机分配一个当前已有的地点和意图
                loc = random.choice(self._locations) if self._locations else "未知地点"
                intent = random.choice(self._intents) if self._intents else "游荡"
                profile = self._resolve_agent_profile(entity_name)
                summary = str(profile.get("summary", "")).strip() or "暂无简介"
                new_agent = AgentState(
                    name=entity_name,
                    location=loc,
                    intent=intent,
                    summary=summary,
                    card_description=str(profile.get("description", "")).strip(),
                    card_personality=str(profile.get("personality", "")).strip(),
                    card_scenario=str(profile.get("scenario", "")).strip(),
                )
                self._agents.append(new_agent)
                
                result["ok"] = True
                result["msg"] = f"成功补充人物：{entity_name} (初始位置: {loc})"
            else:
                result["msg"] = "未知的实体类型"
                
        return result

    def clear_session_data(self) -> dict[str, Any]:
        """清空该会话的所有万象数据（清档）：agents、journals、快照、状态文件等"""
        history_names: list[str] = []
        with self._lock:
            agent_count = len(self._agents)
            history_names = [a.name for a in self._agents if str(a.name).strip()]
            self._agents.clear()
            self._last_tick = 0
            self._latest_digest = ""
            self._active_characters = []
            self._locations = ["未知地点"]
            self._relationships.clear()
            self._identities.clear()
            self._items.clear()
            self._active_rumors.clear()
            self._world_day = 1
            self._time_of_day = "清晨"
            self._possessed_character = ""
            self._original_user_name = ""
            self._captured_preset_messages.clear()
            self._captured_llm_params.clear()
            self._state_snapshots.clear()
            self._state_cursor_hash = ""
            self._source = "fallback"
            # 删除状态文件
            try:
                if self._state_file.exists():
                    self._state_file.unlink()
            except Exception:
                pass
        for name in history_names:
            self._clear_agent_history_runtime(name)
        try:
            for fp in self._history_dir.glob(f"agent_history_{self._safe_session_id}_*.json"):
                if fp.is_file():
                    fp.unlink()
        except Exception:
            pass
        return {"ok": True, "agents_cleared": agent_count, "session_id": self.session_id}

    def clear_agent_data(self, char_name: str) -> dict[str, Any]:
        """清空某个角色的日志、聊天记录等信息"""
        with self._lock:
            agent = None
            for a in self._agents:
                if a.name == char_name:
                    agent = a
                    break
            if not agent:
                return {"ok": False, "error": f"角色 [{char_name}] 不存在"}
            journal_count = len(agent.journal)
            agent.journal.clear()
            agent.summary = "暂无简介"
            agent.salience = 0
            agent.updated_at = 0.0
        self._clear_agent_history_runtime(char_name)
        # save_turn_snapshot 内部会分别获取 _lock 和 _state_lock，不可在持有 _lock 时调用
        self.save_turn_snapshot(reason="clear_agent")
        return {"ok": True, "char_name": char_name, "journal_cleared": journal_count}

    def _extract_location_hint(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        last_user = ""
        for row in reversed(messages):
            if str(row.get("role", "")).lower() == "user":
                last_user = str(row.get("content", "")).strip()
                break
        if not last_user:
            return ""
        for loc in self._locations:
            if loc in last_user:
                return loc
        return ""

    def _load_initial_entities(self) -> None:
        user_dir = Path(self._tavern_user_dir) if self._tavern_user_dir else self._default_tavern_user_dir()
        world_file = Path(self._tavern_world_file) if self._tavern_world_file else self._guess_world_file(user_dir)
        
        loaded_locations = self._load_locations_from_world(world_file)
        self._worldbook_entries = self._load_worldbook_entries(world_file)
        self._world_rule_map = self._load_world_rules(world_file)
        self._world_rule_summary = self._summarize_world_rule_map(self._world_rule_map)
        self._card_profile_map = self._load_character_profiles(user_dir)
        if loaded_locations:
            self._locations = loaded_locations

        final_names = []
        for ac in self._active_characters:
            if ac not in final_names:
                final_names.append(ac)
                
        world_chars = self._extract_characters_from_world(world_file)
        for wc in world_chars:
            if wc not in final_names:
                final_names.append(wc)
                
        if not final_names:
            card_names = self._load_character_names(user_dir)
            if card_names:
                filtered_cards = [n for n in card_names if "Seraphina" not in n and "default" not in n.lower()]
                final_names = filtered_cards[:5] if filtered_cards else card_names[:5]
                self._source = "tavern_cards_only"
                for loc in self._location_lexicon:
                    if loc not in self._locations:
                        self._locations.append(loc)
            else:
                final_names = []
                self._source = "fallback"
        else:
            self._source = "tavern"
            for loc in self._location_lexicon:
                if loc not in self._locations:
                    self._locations.append(loc)

        if user_dir:
            self._tavern_user_dir = str(user_dir)
        if world_file:
            self._tavern_world_file = str(world_file)
            
        # TODO: 后续应改为基于 Session ID 从 saves/ 目录读取
        self._agents = []
        self._full_card_map = {}
        for n in final_names[:80]:
            profile = self._resolve_agent_profile(n)
            summary = str(profile.get("summary", "")).strip() or "暂无简介"
            card_description = str(profile.get("description", "")).strip()
            card_personality = str(profile.get("personality", "")).strip()
            card_scenario = str(profile.get("scenario", "")).strip()
            # 加载完整 Tavern 角色卡
            full_card = self._load_full_card(n, user_dir)
            self._full_card_map[n] = full_card
            if n in self._active_characters:
                self._agents.append(AgentState(name=n, location="当前场景", intent="正与玩家互动", summary=summary, card_description=card_description, card_personality=card_personality, card_scenario=card_scenario, full_card=full_card))
            else:
                loc = random.choice(self._locations) if self._locations else "未知地点"
                self._agents.append(AgentState(name=n, location=loc, intent="待机中", summary=summary, card_description=card_description, card_personality=card_personality, card_scenario=card_scenario, full_card=full_card))
                
        if not self._agents and self._source == "fallback":
            self._latest_digest = "未检测到酒馆角色卡与世界书，当前未连通。"

    def _default_tavern_user_dir(self) -> Path:
        project_root = Path(__file__).resolve().parents[1]
        return project_root / "酒馆" / "data" / "default-user"

    def _guess_world_file(self, user_dir: Path) -> Path:
        worlds_dir = user_dir / "worlds"
        if not worlds_dir.exists():
            return Path("")
        files = sorted([p for p in worlds_dir.glob("*.json") if p.is_file()])
        if not files:
            return Path("")
        return files[0]

    def _load_worldbook_entries(self, world_file: Path) -> list[dict[str, Any]]:
        if not world_file or not world_file.exists():
            return []
        try:
            payload = json.loads(world_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = payload.get("entries", {})
        rows: list[dict[str, Any]] = []
        if isinstance(entries, dict):
            rows = [x for x in entries.values() if isinstance(x, dict)]
        elif isinstance(entries, list):
            rows = [x for x in entries if isinstance(x, dict)]
        out: list[dict[str, Any]] = []
        for row in rows:
            if bool(row.get("disable", False)):
                continue
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            keys: list[str] = []
            for raw_key in row.get("key", []) if isinstance(row.get("key", []), list) else []:
                tok = str(raw_key).strip()
                if tok and tok not in keys:
                    keys.append(tok)
            for raw_key in row.get("keysecondary", []) if isinstance(row.get("keysecondary", []), list) else []:
                tok = str(raw_key).strip()
                if tok and tok not in keys:
                    keys.append(tok)
            out.append(
                {
                    "uid": str(row.get("uid", "")),
                    "comment": str(row.get("comment", "")).strip(),
                    "content": content,
                    "keys": keys[:20],
                    "constant": bool(row.get("constant", False)),
                    "position": int(row.get("position", 0)) if str(row.get("position", "")).strip().isdigit() else 0,
                }
            )
            if len(out) >= 300:
                break
        return out

    def _load_character_profiles(self, user_dir: Path) -> dict[str, dict[str, str]]:
        profiles: dict[str, dict[str, str]] = {}
        chars_dir = user_dir / "characters"
        if not chars_dir.exists():
            return profiles
        json_files = [p for p in chars_dir.rglob("*.json") if p.is_file()]
        for p in json_files[:400]:
            profile = self._extract_card_profile_from_json(p)
            name = str(profile.get("name", "")).strip()
            if not name or self._is_non_character_name(name):
                continue
            if name not in profiles:
                profiles[name] = profile
            else:
                merged = dict(profiles[name])
                for k in ["description", "personality", "scenario", "summary"]:
                    if not merged.get(k) and profile.get(k):
                        merged[k] = profile.get(k, "")
                profiles[name] = merged
        return profiles

    def _load_character_names(self, user_dir: Path) -> list[str]:
        names: list[str] = []
        chars_dir = user_dir / "characters"
        if not chars_dir.exists():
            return names
        dir_names: list[str] = []
        json_names: list[str] = []
        file_stems: list[str] = []
        for p in chars_dir.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_dir():
                if self._looks_like_character_dir(p):
                    name = p.name.strip()
                    if len(name) >= 2 and name not in dir_names and not self._is_non_character_name(name):
                        dir_names.append(name)
            elif p.is_file():
                suffix = p.suffix.lower()
                if suffix == ".json":
                    names = self._extract_name_from_card_json(p)
                    for n in names:
                        if n not in json_names:
                            json_names.append(n)
                    continue
                if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    continue
                stem = p.stem.strip()
                if stem.lower().startswith("default_"):
                    stem = stem[8:]
                if len(stem) >= 2 and stem not in file_stems and not self._is_non_character_name(stem):
                    file_stems.append(stem)
        for row in [*dir_names, *json_names, *file_stems]:
            if row not in names:
                names.append(row)
        return names

    def _extract_card_profile_from_json(self, file_path: Path) -> dict[str, str]:
        profile = {"name": "", "description": "", "personality": "", "scenario": "", "summary": ""}
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return profile
        if not isinstance(payload, dict):
            return profile
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            data = payload
        name = str(data.get("name", "") or payload.get("name", "")).strip()
        description = str(data.get("description", "") or data.get("first_mes", "")).strip()
        personality = str(data.get("personality", "")).strip()
        scenario = str(data.get("scenario", "")).strip()
        if not description:
            description = str(data.get("mes_example", "")).strip()
        cb = data.get("character_book", {})
        cb_additional: list[str] = []
        if isinstance(cb, dict):
            entries = cb.get("entries", [])
            if isinstance(entries, list):
                for row in entries[:20]:
                    if not isinstance(row, dict):
                        continue
                    comment = str(row.get("comment", "")).strip()
                    content = str(row.get("content", "")).strip()
                    if content:
                        block = f"{comment}:{content}" if comment else content
                        cb_additional.append(self._compress_text(block, 100))
        if cb_additional and not scenario:
            scenario = "；".join(cb_additional[:3])
        summary = self._compress_text("；".join([x for x in [description, personality, scenario] if x]), 120)
        profile["name"] = name
        profile["description"] = self._compress_text(description, 220)
        profile["personality"] = self._compress_text(personality, 220)
        profile["scenario"] = self._compress_text(scenario, 220)
        profile["summary"] = summary if summary else "暂无简介"
        return profile

    def _is_valid_location(self, text: str) -> bool:
        """检查文本是否是一个合理的地点名称"""
        if not text or len(text) < 2 or len(text) > 12:
            return False
        
        # 允许包含"档案"等关键词，但整体必须是合理的地点名称，例如"档案馆"
        # 排除包含常见非地点关键词的条目，放宽对特定地点的限制
        blocked_tokens = [
            "设定", "规则", "指南", "角色", "玩家", 
            "背景", "技能", "性格", "外貌", "外观", "关系", "核心",
            "说明", "故事", "属性", "系统", "逻辑", "概念", "身份",
            "nsfw", "rule", "lore", "guide", "personality"
        ]
        
        # 特别处理：如果包含"档案"但结尾是"馆"、"室"、"处"等，则认为是合法地点
        text_lower = text.lower()
        if "档案" in text_lower:
            if not any(text_lower.endswith(suffix) for suffix in ["馆", "室", "处", "库", "局"]):
                return False
                
        # 过滤其他纯数值或明显不合理的格式
        if text.replace(".", "").isdigit():
            return False
            
        if any(t in text_lower for t in blocked_tokens):
            return False
            
        return True

    def _extract_characters_from_world(self, world_file: Path) -> list[str]:
        if not world_file or not world_file.exists():
            return []
        try:
            payload = json.loads(world_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = payload.get("entries", {})
        rows: list[dict[str, Any]] = []
        if isinstance(entries, dict):
            rows = [x for x in entries.values() if isinstance(x, dict)]
        elif isinstance(entries, list):
            rows = [x for x in entries if isinstance(x, dict)]
            
        found: list[str] = []
        for row in rows:
            content = str(row.get("content", ""))
            # Extract characters defined in world book using regex
            import re
            matches = re.findall(r'(?:character_name|角色|人物)\s*[:：]\s*["\'"]([^"\'"]+)["\'"]', content, re.IGNORECASE)
            for m in matches:
                name = m.strip()
                if name and name not in found and not self._is_non_character_name(name):
                    found.append(name)
        return found

    def _load_locations_from_world(self, world_file: Path) -> list[str]:
        if not world_file or not world_file.exists():
            return list(self._locations)
        try:
            payload = json.loads(world_file.read_text(encoding="utf-8"))
        except Exception:
            return list(self._locations)
        entries = payload.get("entries", {})
        rows: list[dict[str, Any]] = []
        if isinstance(entries, dict):
            rows = [x for x in entries.values() if isinstance(x, dict)]
        elif isinstance(entries, list):
            rows = [x for x in entries if isinstance(x, dict)]
        found: list[str] = []
        for row in rows:
            comment = str(row.get("comment", "")).strip()
            if self._is_valid_location(comment) and comment not in found:
                found.append(comment)
            keys = row.get("key", [])
            if isinstance(keys, list):
                for k in keys:
                    ks = str(k).strip()
                    if self._is_valid_location(ks) and ks not in found:
                        found.append(ks)
        merged = list(self._locations)
        for x in found:
            if x not in merged:
                merged.append(x)
        return merged[:60]

    def _load_world_rules(self, world_file: Path) -> dict[str, list[str]]:
        rule_map: dict[str, list[str]] = {
            "hard_constraints": [],
            "safety_limits": [],
            "style_instructions": [],
            "world_logic": [],
        }
        if not world_file or not world_file.exists():
            return rule_map
        try:
            payload = json.loads(world_file.read_text(encoding="utf-8"))
        except Exception:
            return rule_map
        entries = payload.get("entries", {})
        rows: list[dict[str, Any]] = []
        if isinstance(entries, dict):
            rows = [x for x in entries.values() if isinstance(x, dict)]
        elif isinstance(entries, list):
            rows = [x for x in entries if isinstance(x, dict)]
        total = 0
        for row in rows:
            if bool(row.get("disable", False)):
                continue
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            key_text = " ".join([str(x) for x in row.get("key", [])]) if isinstance(row.get("key", []), list) else ""
            comment = str(row.get("comment", "")).strip()
            low = f"{key_text} {comment} {content}".lower()
            is_rule_like = any(t in low for t in ["规则", "必须", "禁止", "约束", "不得", "rule", "must", "forbid", "should", "always", "never"])
            if not is_rule_like:
                continue
            brief = self._compress_text(content)
            if not brief:
                continue
            target_key = self._classify_rule_bucket(low)
            bucket = rule_map.get(target_key, [])
            if brief not in bucket:
                bucket.append(brief)
                rule_map[target_key] = bucket[:4]
                total += 1
            if total >= 12:
                break
        return rule_map

    def _classify_rule_bucket(self, low: str) -> str:
        tpl = self._rule_templates
        if any(x in low for x in tpl.get("safety_limits", [])):
            return "safety_limits"
        if any(x in low for x in tpl.get("style_instructions", [])):
            return "style_instructions"
        if any(x in low for x in tpl.get("world_logic", [])):
            return "world_logic"
        return "hard_constraints"

    def _summarize_world_rule_map(self, rule_map: dict[str, list[str]]) -> str:
        lines: list[str] = []
        labels = [
            ("hard_constraints", "硬约束"),
            ("safety_limits", "安全限制"),
            ("style_instructions", "文风要求"),
            ("world_logic", "世界逻辑"),
        ]
        for key, label in labels:
            rows = rule_map.get(key, [])
            if not rows:
                continue
            lines.append(f"{label}:{rows[0]}")
        return "；".join(lines[:3])

    def _load_rule_templates(self) -> dict[str, list[str]]:
        defaults: dict[str, list[str]] = {
            "hard_constraints": ["规则", "必须", "约束", "rule", "must", "always", "should"],
            "safety_limits": ["禁止", "不得", "forbid", "ban", "never", "nsfw", "violent", "成人", "越界"],
            "style_instructions": ["文风", "语气", "写作", "style", "tone", "narrative", "叙事"],
            "world_logic": ["世界", "地点", "阵营", "关系", "设定", "逻辑", "world", "lore"],
        }
        if not self._rule_template_file:
            return defaults
        p = Path(self._rule_template_file)
        if not p.exists() or not p.is_file():
            return defaults
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        if not isinstance(payload, dict):
            return defaults
        merged: dict[str, list[str]] = dict(defaults)
        for k in ["hard_constraints", "safety_limits", "style_instructions", "world_logic"]:
            rows = payload.get(k)
            if isinstance(rows, list):
                vals = [str(x).strip().lower() for x in rows if str(x).strip()]
                if vals:
                    merged[k] = vals[:20]
        return merged

    def reload_rule_templates(self, template_file: str = "") -> dict[str, Any]:
        with self._lock:
            if template_file.strip():
                self._rule_template_file = template_file.strip()
            self._rule_templates = self._load_rule_templates()
            self._world_rule_map = self._load_world_rules(Path(self._tavern_world_file)) if self._tavern_world_file else self._world_rule_map
            self._world_rule_summary = self._summarize_world_rule_map(self._world_rule_map)
            return {
                "rule_template_file": self._rule_template_file,
                "rule_templates": self._rule_templates,
                "world_rule_summary": self._world_rule_summary,
            }

    def _compress_text(self, text: str, limit: int = 56) -> str:
        if not text:
            return ""
        s = " ".join(text.replace("\n", " ").split())
        if len(s) <= limit:
            return s
        return s[: limit - 1] + "…"

    def _is_non_character_name(self, name: str) -> bool:
        lowered = name.lower()
        blocked_tokens = [
            "world",
            "lore",
            "rule",
            "prompt",
            "preset",
            "guide",
            "设定",
            "规则",
            "指南",
            "模板",
            "系统",
            "世界",
            "预设",
            "说明",
            "背景",
            "故事",
            "技能",
            "关系",
            "备份",
            "导出",
            "测试",
        ]
        if any(t in lowered for t in blocked_tokens):
            return True
        if len(name) > 24:
            return True
        return False

    def _extract_name_from_card_json(self, file_path: Path) -> list[str]:
        names: list[str] = []
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return names
        if not isinstance(payload, dict):
            return names
        
        raw = payload.get("name", "")
        if not raw and isinstance(payload.get("data"), dict):
            raw = payload.get("data", {}).get("name", "")
        name = str(raw or "").strip()
        if len(name) >= 2 and not self._is_non_character_name(name):
            names.append(name)
            
        data_block = payload.get("data", payload)
        if isinstance(data_block, dict):
            cb = data_block.get("character_book", {})
            if isinstance(cb, dict):
                entries = cb.get("entries", [])
                if isinstance(entries, list):
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        comment = str(entry.get("comment", "")).strip()
                        if 2 <= len(comment) <= 12 and not self._is_non_character_name(comment):
                            content = str(entry.get("content", "")).lower()
                            if any(x in content for x in ["性格", "外貌", "喜欢", "讨厌", "personality", "appearance"]):
                                if comment not in names:
                                    names.append(comment)
        return names

    def _looks_like_character_dir(self, dir_path: Path) -> bool:
        if not dir_path.exists() or not dir_path.is_dir():
            return False
        for p in dir_path.iterdir():
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".json"}:
                return True
        return False


class SessionManager:
    def __init__(self, config: dict[str, Any]):
        self._sessions: dict[str, WorldSession] = {}
        self._lock = threading.Lock()
        self.config = config
        self._config_file = Path(__file__).parent / "omnis_config.json"
        self._load_config()

    def _load_config(self):
        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    saved_config = json.load(f)
                    # Never load secrets from file – they must come from env vars
                    for k in self._SECRET_KEYS:
                        saved_config.pop(k, None)
                    self.config.update(saved_config)
            except Exception as e:
                print(f"Failed to load config: {e}")

    _SECRET_KEYS = {"llm_api_key"}

    def _save_config(self):
        try:
            safe_config = {k: v for k, v in self.config.items() if k not in self._SECRET_KEYS}
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(safe_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def get_or_create_session(self, session_id: str) -> WorldSession:
        with self._lock:
            if session_id not in self._sessions:
                llm_client = None
                if OmnisLLMClient and self.config.get("llm_api_base"):
                    llm_client = OmnisLLMClient(
                        api_base=self.config["llm_api_base"],
                        api_key=self.config.get("llm_api_key", ""),
                        model=self.config.get("llm_model", ""),
                    )
                self._sessions[session_id] = WorldSession(
                    session_id=session_id,
                    tavern_user_dir=self.config.get("tavern_user_dir", ""),
                    tavern_world_file=self.config.get("tavern_world_file", ""),
                    script_guard_enabled=self.config.get("script_guard_enabled", True),
                    script_guard_action=self.config.get("script_guard_action", "reduce"),
                    rule_template_file=self.config.get("rule_template_file", ""),
                    llm_client=llm_client,
                    max_journal_limit=int(self.config.get("max_journal_limit", 3)),
                    dynamic_lore_writeback=self.config.get("dynamic_lore_writeback", False),
                    turn_min_interval_sec=float(self.config.get("turn_min_interval_sec", 0.5)),
                    active_location_freeze_turns=int(self.config.get("active_location_freeze_turns", 2)),
                    location_lexicon=self.config.get("location_lexicon", None),
                    location_lexicon_file=str(self.config.get("location_lexicon_file", "")).strip(),
                    strict_background_independent=bool(self.config.get("strict_background_independent", True)),
                    rumor_default_ttl_ticks=int(self.config.get("rumor_default_ttl_ticks", 48)),
                    rumor_ttl_by_llm=bool(self.config.get("rumor_ttl_by_llm", True)),
                )
            return self._sessions[session_id]

    def get_all_sessions(self) -> dict[str, Any]:
        with self._lock:
            return {
                sid: {
                    "tick": s._last_tick,
                    "agents_alive": len([a for a in s._agents if a.status == "alive"]),
                    "active_characters": s._active_characters,
                }
                for sid, s in self._sessions.items()
            }

    def clear_all_sessions(self) -> int:
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
            return n

    def flush_all_state_stores(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            try:
                s.flush_state_store_now()
            except Exception:
                pass

    def clear_state_files(self) -> int:
        project_root = Path(__file__).resolve().parents[1]
        state_dir = project_root / "storage" / "run_records"
        if not state_dir.exists():
            return 0
        files = [p for p in state_dir.glob("omnis_state_*.json") if p.is_file()]
        cleared = 0
        for p in files:
            try:
                p.unlink()
                cleared += 1
            except Exception:
                pass
        return cleared

    def get_target_session(self, session_id: str = "") -> tuple[str, WorldSession] | tuple[None, None]:
        with self._lock:
            if not self._sessions:
                return (None, None)
            if session_id and session_id in self._sessions:
                return (session_id, self._sessions[session_id])
            first_sid = next(iter(self._sessions.keys()))
            return (first_sid, self._sessions[first_sid])

    def update_llm_config(self, api_base: str, api_key: str, model: str, max_journal_limit: int = 3, dynamic_lore_writeback: bool = False) -> None:
        with self._lock:
            self.config["llm_api_base"] = api_base
            self.config["llm_api_key"] = api_key
            self.config["llm_model"] = model
            self.config["max_journal_limit"] = max_journal_limit
            self.config["dynamic_lore_writeback"] = dynamic_lore_writeback
            self._save_config()
            for s in self._sessions.values():
                s.max_journal_limit = max_journal_limit
                s.dynamic_lore_writeback = dynamic_lore_writeback
                if OmnisLLMClient and api_base:
                    s.llm_client = OmnisLLMClient(api_base=api_base, api_key=api_key, model=model)
                else:
                    s.llm_client = None

    def update_runtime_tuning_config(self, turn_min_interval_sec: float, active_location_freeze_turns: int, location_lexicon: list[str], location_lexicon_file: str = "", strict_background_independent: bool = True, wait_for_turn_before_reply: bool = False, wait_turn_timeout_sec: float = 8.0, wait_turn_scope: str = "related", auto_wait_scope_downgrade: bool = True, rumor_default_ttl_ticks: int = 48, rumor_ttl_by_llm: bool = True) -> None:
        with self._lock:
            self.config["turn_min_interval_sec"] = max(0.0, float(turn_min_interval_sec))
            self.config["active_location_freeze_turns"] = max(0, int(active_location_freeze_turns))
            self.config["location_lexicon"] = [x for x in location_lexicon if isinstance(x, str) and x.strip()]
            self.config["strict_background_independent"] = bool(strict_background_independent)
            self.config["wait_for_turn_before_reply"] = bool(wait_for_turn_before_reply)
            self.config["wait_turn_timeout_sec"] = max(0.0, float(wait_turn_timeout_sec))
            self.config["wait_turn_scope"] = "all" if str(wait_turn_scope).strip().lower() == "all" else "related"
            self.config["auto_wait_scope_downgrade"] = bool(auto_wait_scope_downgrade)
            self.config["rumor_default_ttl_ticks"] = max(6, min(240, int(rumor_default_ttl_ticks)))
            self.config["rumor_ttl_by_llm"] = bool(rumor_ttl_by_llm)
            if location_lexicon_file:
                self.config["location_lexicon_file"] = str(location_lexicon_file).strip()
            self._save_config()
            for s in self._sessions.values():
                s._turn_min_interval_sec = max(0.0, float(self.config["turn_min_interval_sec"]))
                s._active_location_freeze_turns = max(0, int(self.config["active_location_freeze_turns"]))
                s._strict_background_independent = bool(self.config.get("strict_background_independent", True))
                s._rumor_default_ttl_ticks = int(self.config.get("rumor_default_ttl_ticks", 48))
                s._rumor_ttl_by_llm = bool(self.config.get("rumor_ttl_by_llm", True))
                if location_lexicon_file:
                    s._location_lexicon_file = str(location_lexicon_file).strip()
                default_lexicon = list(s._location_lexicon) if s._location_lexicon else []
                lex = self.config.get("location_lexicon", None)
                s._location_lexicon = s._load_location_lexicon(lex, default_lexicon)
                for loc in s._location_lexicon:
                    if loc not in s._locations:
                        s._locations.append(loc)

    def update_tavern_paths(self, tavern_user_dir: str = "", tavern_world_file: str = "") -> None:
        with self._lock:
            self.config["tavern_user_dir"] = str(tavern_user_dir or "").strip()
            self.config["tavern_world_file"] = str(tavern_world_file or "").strip()
            self._save_config()
            for s in self._sessions.values():
                s.ensure_tavern_binding(self.config["tavern_user_dir"], self.config["tavern_world_file"])

    def reload_rule_templates(self, template_file: str = "") -> dict[str, Any]:
        with self._lock:
            if template_file:
                self.config["rule_template_file"] = template_file
            res = {}
            for s in self._sessions.values():
                res = s.reload_rule_templates(template_file)
            return res

class OmnisProxyHandler(BaseHTTPRequestHandler):
    session_manager: SessionManager | None = None
    timeout_sec: float = 120.0
    inject_default: str = "full"
    session_cache_ttl_sec: float = 120.0
    _session_cache: dict[str, dict[str, Any]] = {}
    _session_cache_lock = threading.Lock()
    admin_token: str = ""
    strict_admin_auth: bool = False
    max_body_bytes: int = 2 * 1024 * 1024
    proxy_allowed_hosts: list[str] = []
    proxy_forward_authorization: bool = True
    build_id: str = "dev"
    started_at_unix: int = 0
    _audit_events: list[dict[str, Any]] = []
    _audit_lock = threading.Lock()

    def _startup_self_check(self, runtime_status: dict[str, Any]) -> dict[str, Any]:
        cfg = self.session_manager.config if self.session_manager else {}
        user_dir = str(cfg.get("tavern_user_dir", "")).strip() or str(runtime_status.get("tavern_user_dir", "")).strip()
        world_file = str(cfg.get("tavern_world_file", "")).strip() or str(runtime_status.get("tavern_world_file", "")).strip()
        source = str(runtime_status.get("source", "")).strip()
        user_ok = bool(user_dir and Path(user_dir).exists())
        world_ok = bool(world_file and Path(world_file).exists())
        if source == "fallback":
            return {
                "ok": False,
                "severity": "error",
                "code": "fallback_mode",
                "message": "未连通酒馆（fallback），当前不是玩家真实角色/世界。",
                "source": source,
                "tavern_user_dir_ok": user_ok,
                "tavern_world_file_ok": world_ok,
            }
        if source in {"tavern", "tavern_cards_only"}:
            return {
                "ok": True,
                "severity": "ok",
                "code": "connected",
                "message": "已连通酒馆数据。",
                "source": source,
                "tavern_user_dir_ok": user_ok,
                "tavern_world_file_ok": world_ok,
            }
        if not user_ok or not world_ok:
            return {
                "ok": False,
                "severity": "error",
                "code": "path_invalid",
                "message": "未连通酒馆：角色卡或世界书路径无效。",
                "source": source or "none",
                "tavern_user_dir_ok": user_ok,
                "tavern_world_file_ok": world_ok,
            }
        return {
            "ok": False,
            "severity": "warn",
            "code": "session_not_ready",
            "message": "酒馆路径有效，但会话尚未初始化。",
            "source": source or "none",
            "tavern_user_dir_ok": user_ok,
            "tavern_world_file_ok": world_ok,
        }

    def do_GET(self) -> None:
        if self.path == "/":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            dashboard_file = Path(__file__).parent / "dashboard.html"
            if dashboard_file.exists():
                self.wfile.write(dashboard_file.read_bytes())
            else:
                self.wfile.write(b"<html><body><h1>Omnis Dashboard Not Found</h1></body></html>")
            return
            
        if self.path.startswith("/health"):
            sessions_info = self.session_manager.get_all_sessions() if self.session_manager else {}
            
            parsed_url = urlsplit(self.path)
            query = parse_qs(parsed_url.query or "")
            target_session_id = str((query.get("session_id", [""])[0] or "")).strip()
            requested_session_id = target_session_id

            target_runtime = {}
            config_snapshot = {}
            allow_detail = self._allow_detailed_health()
            if self.session_manager:
                config_snapshot = {
                    "llm_api_base": str(self.session_manager.config.get("llm_api_base", "")).strip(),
                    "llm_api_key_masked": self._mask_secret(str(self.session_manager.config.get("llm_api_key", "")).strip()),
                    "llm_model": str(self.session_manager.config.get("llm_model", "")).strip(),
                    "max_journal_limit": int(self.session_manager.config.get("max_journal_limit", 3)),
                    "dynamic_lore_writeback": bool(self.session_manager.config.get("dynamic_lore_writeback", False)),
                    "turn_min_interval_sec": float(self.session_manager.config.get("turn_min_interval_sec", 0.5)),
                    "active_location_freeze_turns": int(self.session_manager.config.get("active_location_freeze_turns", 2)),
                    "location_lexicon": self.session_manager.config.get("location_lexicon", []),
                    "location_lexicon_file": str(self.session_manager.config.get("location_lexicon_file", "")).strip(),
                    "tavern_user_dir": str(self.session_manager.config.get("tavern_user_dir", "")).strip(),
                    "tavern_world_file": str(self.session_manager.config.get("tavern_world_file", "")).strip(),
                    "strict_background_independent": bool(self.session_manager.config.get("strict_background_independent", True)),
                    "wait_for_turn_before_reply": bool(self.session_manager.config.get("wait_for_turn_before_reply", False)),
                    "wait_turn_timeout_sec": float(self.session_manager.config.get("wait_turn_timeout_sec", 8.0)),
                    "wait_turn_scope": "all" if str(self.session_manager.config.get("wait_turn_scope", "related")).strip().lower() == "all" else "related",
                    "auto_wait_scope_downgrade": bool(self.session_manager.config.get("auto_wait_scope_downgrade", True)),
                    "rumor_default_ttl_ticks": int(self.session_manager.config.get("rumor_default_ttl_ticks", 48)),
                    "rumor_ttl_by_llm": bool(self.session_manager.config.get("rumor_ttl_by_llm", True)),
                }
            if self.session_manager:
                resolved_sid, resolved_runtime = self.session_manager.get_target_session(target_session_id)
                if resolved_sid and resolved_runtime:
                    target_session_id = resolved_sid
                    target_runtime = resolved_runtime.status() if allow_detail else self._public_runtime(resolved_runtime)
            session_resolved_fallback = bool(requested_session_id and target_session_id and requested_session_id != target_session_id)
            startup_self_check = self._startup_self_check(target_runtime if isinstance(target_runtime, dict) else {})
            payload = {
                "ok": True,
                "service": "omnis-proxy",
                "runtime": target_runtime,
                "config": config_snapshot,
                "active_session_id": target_session_id,
                "requested_session_id": requested_session_id,
                "session_resolved_fallback": session_resolved_fallback,
                "sessions": sessions_info,
                "inject_default": self.inject_default,
                "session_cache_ttl_sec": self.session_cache_ttl_sec,
                "session_cache_size": self._session_cache_size(),
                "health_detail": allow_detail,
                "startup_self_check": startup_self_check,
                "build_id": self.build_id,
                "started_at_unix": int(self.started_at_unix),
                "audit_summary": self._audit_summary(),
            }
            etag = build_health_etag(payload)
            req_etag = str(self.headers.get("If-None-Match", "")).strip().strip('"')
            if req_etag and req_etag == etag:
                self._record_audit_event(int(HTTPStatus.NOT_MODIFIED), {"ok": True, "action": "health_not_modified"})
                self.send_response(int(HTTPStatus.NOT_MODIFIED))
                self.send_header("ETag", f"\"{etag}\"")
                self.end_headers()
                return
            self._record_audit_event(int(HTTPStatus.OK), payload)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(HTTPStatus.OK))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("ETag", f"\"{etag}\"")
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        # 获取基础路径（去掉可能存在的查询参数）
        base_path = urlsplit(self.path).path
        normalized_path = base_path.rstrip("/")
        
        if normalized_path.endswith("/admin/update_llm"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            api_base = self._normalize_api_base(payload.get("api_base", ""))
            api_key = str(payload.get("api_key", "")).strip()
            prev_api_key = str(self.session_manager.config.get("llm_api_key", "")).strip()
            if not api_key:
                api_key = prev_api_key
            model = str(payload.get("model", "")).strip()
            max_journal_limit = self._parse_int(
                payload.get("max_journal_limit", self.session_manager.config.get("max_journal_limit", 3)),
                int(self.session_manager.config.get("max_journal_limit", 3)),
                min_value=1,
                max_value=20,
            )
            dynamic_lore_writeback = self._parse_bool(
                payload.get("dynamic_lore_writeback", self.session_manager.config.get("dynamic_lore_writeback", False)),
                bool(self.session_manager.config.get("dynamic_lore_writeback", False)),
            )
            self.session_manager.update_llm_config(api_base, api_key, model, max_journal_limit, dynamic_lore_writeback)
            self._json(HTTPStatus.OK, {"ok": True, "action": "update_llm"})
            return

        if normalized_path.endswith("/admin/update_runtime_tuning"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            turn_min_interval_sec = self._parse_float(
                payload.get("turn_min_interval_sec", self.session_manager.config.get("turn_min_interval_sec", 0.5)),
                float(self.session_manager.config.get("turn_min_interval_sec", 0.5)),
                min_value=0.0,
                max_value=10.0,
            )
            active_location_freeze_turns = self._parse_int(
                payload.get("active_location_freeze_turns", self.session_manager.config.get("active_location_freeze_turns", 2)),
                int(self.session_manager.config.get("active_location_freeze_turns", 2)),
                min_value=0,
                max_value=20,
            )
            strict_background_independent = self._parse_bool(
                payload.get("strict_background_independent", self.session_manager.config.get("strict_background_independent", True)),
                bool(self.session_manager.config.get("strict_background_independent", True)),
            )
            wait_for_turn_before_reply = self._parse_bool(
                payload.get("wait_for_turn_before_reply", self.session_manager.config.get("wait_for_turn_before_reply", False)),
                bool(self.session_manager.config.get("wait_for_turn_before_reply", False)),
            )
            wait_turn_timeout_sec = self._parse_float(
                payload.get("wait_turn_timeout_sec", self.session_manager.config.get("wait_turn_timeout_sec", 8.0)),
                float(self.session_manager.config.get("wait_turn_timeout_sec", 8.0)),
                min_value=0.0,
                max_value=60.0,
            )
            wait_turn_scope = str(payload.get("wait_turn_scope", self.session_manager.config.get("wait_turn_scope", "related"))).strip().lower()
            if wait_turn_scope not in {"related", "all"}:
                wait_turn_scope = "related"
            auto_wait_scope_downgrade = self._parse_bool(
                payload.get("auto_wait_scope_downgrade", self.session_manager.config.get("auto_wait_scope_downgrade", True)),
                bool(self.session_manager.config.get("auto_wait_scope_downgrade", True)),
            )
            rumor_default_ttl_ticks = self._parse_int(
                payload.get("rumor_default_ttl_ticks", self.session_manager.config.get("rumor_default_ttl_ticks", 48)),
                int(self.session_manager.config.get("rumor_default_ttl_ticks", 48)),
                min_value=6,
                max_value=240,
            )
            rumor_ttl_by_llm = self._parse_bool(
                payload.get("rumor_ttl_by_llm", self.session_manager.config.get("rumor_ttl_by_llm", True)),
                bool(self.session_manager.config.get("rumor_ttl_by_llm", True)),
            )
            max_journal_limit = self._parse_int(
                payload.get("max_journal_limit", self.session_manager.config.get("max_journal_limit", 3)),
                int(self.session_manager.config.get("max_journal_limit", 3)),
                min_value=1,
                max_value=20,
            )
            dynamic_lore_writeback = self._parse_bool(
                payload.get("dynamic_lore_writeback", self.session_manager.config.get("dynamic_lore_writeback", False)),
                bool(self.session_manager.config.get("dynamic_lore_writeback", False)),
            )
            if "location_lexicon" in payload:
                location_lexicon_raw = str(payload.get("location_lexicon", "")).strip()
                location_lexicon = [x.strip() for x in location_lexicon_raw.split(",") if x.strip()] if location_lexicon_raw else []
            else:
                location_lexicon = list(self.session_manager.config.get("location_lexicon", []))
            location_lexicon_file = str(payload.get("location_lexicon_file", self.session_manager.config.get("location_lexicon_file", ""))).strip()
            tavern_user_dir = str(payload.get("tavern_user_dir", self.session_manager.config.get("tavern_user_dir", ""))).strip()
            tavern_world_file = str(payload.get("tavern_world_file", self.session_manager.config.get("tavern_world_file", ""))).strip()
            self.session_manager.update_runtime_tuning_config(
                turn_min_interval_sec,
                active_location_freeze_turns,
                location_lexicon,
                location_lexicon_file,
                strict_background_independent,
                wait_for_turn_before_reply,
                wait_turn_timeout_sec,
                wait_turn_scope,
                auto_wait_scope_downgrade,
                rumor_default_ttl_ticks,
                rumor_ttl_by_llm,
            )
            self.session_manager.update_tavern_paths(tavern_user_dir, tavern_world_file)
            self.session_manager.update_llm_config(
                str(self.session_manager.config.get("llm_api_base", "")).strip(),
                str(self.session_manager.config.get("llm_api_key", "")).strip(),
                str(self.session_manager.config.get("llm_model", "")).strip(),
                max_journal_limit,
                dynamic_lore_writeback,
            )
            self._json(HTTPStatus.OK, {"ok": True, "action": "update_runtime_tuning"})
            return

        if normalized_path.endswith("/admin/possess"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            session_id = str(payload.get("session_id", "default")).strip() or "default"
            target_char = str(payload.get("target_char", "")).strip()
            original_user = str(payload.get("original_user", "User")).strip() or "User"
            if not target_char:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "target_char_required"})
                return
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            runtime = self.session_manager.get_or_create_session(session_id)
            ok = runtime.set_possessed_character(target_char, original_user)
            if not ok:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"角色不存在或非存活状态: {target_char}"})
                return
            self._json(HTTPStatus.OK, {"ok": True, "action": "possess", "session_id": session_id, "target_char": target_char, "original_user": original_user})
            return
            
        if normalized_path.endswith("/admin/test_llm"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            api_base = self._normalize_api_base(payload.get("api_base", ""))
            api_key = str(payload.get("api_key", "")).strip()
            model = str(payload.get("model", "")).strip()

            if self.session_manager:
                if not api_base:
                    api_base = self._normalize_api_base(self.session_manager.config.get("llm_api_base", ""))
                if not api_key:
                    api_key = str(self.session_manager.config.get("llm_api_key", "")).strip()
                if not model:
                    model = str(self.session_manager.config.get("llm_model", "")).strip()
            
            if not api_base or not model:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "API Base 和 Model 不能为空"})
                return
                
            if OmnisLLMClient:
                client = OmnisLLMClient(api_base=api_base, api_key=api_key, model=model)
                success, msg = client.test_connection()
                if success:
                    self._json(HTTPStatus.OK, {"ok": True, "message": msg})
                else:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"{msg}（当前地址: {api_base}，模型: {model}）"})
            else:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "LLM客户端未加载"})
            return

        if normalized_path.endswith("/admin/flashback"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            char_name = str(payload.get("char_name", "")).strip()
            session_id = str(payload.get("session_id", "default")).strip()
            
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
                
            runtime = self.session_manager.get_or_create_session(session_id)
            res = runtime.trigger_flashback(char_name)
            if res.get("ok"):
                self._json(HTTPStatus.OK, res)
            else:
                self._json(HTTPStatus.BAD_REQUEST, res)
            return

        if normalized_path.endswith("/admin/rename_entity"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            old_name = str(payload.get("old_name", "")).strip()
            new_name = str(payload.get("new_name", "")).strip()
            session_id = str(payload.get("session_id", "default")).strip()
            
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
                
            runtime = self.session_manager.get_or_create_session(session_id)
            res = runtime.rename_entity(old_name, new_name)
            if res.get("ok"):
                self._json(HTTPStatus.OK, res)
            else:
                self._json(HTTPStatus.BAD_REQUEST, res)
            return

        if normalized_path.endswith("/admin/force_extract_entity"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            entity_type = str(payload.get("entity_type", "")).strip()
            entity_name = str(payload.get("entity_name", "")).strip()
            session_id = str(payload.get("session_id", "default")).strip()
            
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
                
            runtime = self.session_manager.get_or_create_session(session_id)
            res = runtime.force_extract_entity(entity_type, entity_name)
            if res.get("ok"):
                self._json(HTTPStatus.OK, res)
            else:
                self._json(HTTPStatus.BAD_REQUEST, res)
            return

        if normalized_path.endswith("/admin/toggle_agent_ai"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            char_name = str(payload.get("char_name", "")).strip()
            enabled = self._parse_bool(payload.get("enabled", True), True)
            session_id = str(payload.get("session_id", "default")).strip()
            
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
                
            runtime = self.session_manager.get_or_create_session(session_id)
            success = runtime.toggle_agent_ai(char_name, enabled)
            if success:
                self._json(HTTPStatus.OK, {"ok": True, "action": "toggle_agent_ai", "char_name": char_name, "enabled": enabled})
            else:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"找不到角色：{char_name}"})
            return

        if normalized_path.endswith("/admin/clear_session"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            session_id = str(payload.get("session_id", "")).strip() if payload else ""
            if not session_id:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "session_id 不能为空"})
                return
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            sid, runtime = self.session_manager.get_target_session(session_id)
            if not runtime:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"会话 [{session_id}] 不存在"})
                return
            result = runtime.clear_session_data()
            self._json(HTTPStatus.OK, result)
            return

        if normalized_path.endswith("/admin/clear_agent"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            session_id = str(payload.get("session_id", "")).strip() if payload else ""
            char_name = str(payload.get("char_name", "")).strip() if payload else ""
            if not session_id or not char_name:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "session_id 和 char_name 不能为空"})
                return
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            sid, runtime = self.session_manager.get_target_session(session_id)
            if not runtime:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"会话 [{session_id}] 不存在"})
                return
            result = runtime.clear_agent_data(char_name)
            if result.get("ok"):
                self._json(HTTPStatus.OK, result)
            else:
                self._json(HTTPStatus.NOT_FOUND, result)
            return

        if normalized_path.endswith("/admin/reload_templates"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            template_file = str(payload.get("rule_template_file", "")).strip() if payload else ""
            result = self.session_manager.reload_rule_templates(template_file=template_file)
            self._json(HTTPStatus.OK, {"ok": True, "action": "reload_templates", "result": result})
            return
            
        if normalized_path.endswith("/admin/clear_cache"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            cleared = self._session_cache_clear()
            runtime_cleared = self.session_manager.clear_all_sessions() if self.session_manager else 0
            state_files_cleared = self.session_manager.clear_state_files() if self.session_manager else 0
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "action": "clear_cache",
                    "cleared": cleared,
                    "runtime_cleared": runtime_cleared,
                    "state_files_cleared": state_files_cleared,
                    "build_id": self.build_id,
                },
            )
            return

        if normalized_path.endswith("/admin/export_debug_bundle"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            session_id = str(payload.get("session_id", "default")).strip() or "default"
            limit = self._parse_int(payload.get("limit", 10), 10, min_value=1, max_value=50)
            if not self.session_manager:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "manager_unavailable"})
                return
            runtime = self.session_manager.get_or_create_session(session_id)
            bundle = runtime.build_debug_bundle(limit=limit)
            filename = f"omnis_debug_{session_id}_{int(time.time())}.json"
            self._json(HTTPStatus.OK, {"ok": True, "filename": filename, "bundle": bundle})
            return

        if normalized_path.endswith("/admin/export_audit_events"):
            if not self._check_admin_auth():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_unauthorized"})
                return
            payload = self._read_json_object()
            limit = self._parse_int(payload.get("limit", 200), 200, min_value=1, max_value=5000)
            events = self._audit_export(limit=limit)
            filename = f"omnis_audit_{int(time.time())}.json"
            self._json(HTTPStatus.OK, {"ok": True, "filename": filename, "events": events, "count": len(events)})
            return
            
        if "/admin/" in normalized_path:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "admin_path_not_supported", "path": base_path})
            return
        if not base_path.startswith("/proxy/"):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "path_not_supported", "path": base_path})
            return
            
        target_url = self._resolve_target_url(self.path) # target_url 需要保留完整的 self.path，包含查询参数
        if not target_url:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "target_url_invalid"})
            return
        body_raw = self._read_body()
        if body_raw is None:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload_too_large"})
            return
        fwd_body = body_raw
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            patched = self._patch_payload(body_raw)
            if patched is None and getattr(self, "_mock_sent", False):
                self._mock_sent = False
                return  # 已由 _send_mock_response 直接回复客户端
            if patched is not None:
                fwd_body = patched
        headers = self._build_forward_headers(fwd_body)
        req = request.Request(url=target_url, data=fwd_body, method="POST", headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    lk = k.lower()
                    if lk in {"content-length", "connection", "transfer-encoding", "content-encoding"}:
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                # 响应已发回酒馆，异步提取回复并归纳对话角色日志
                if "application/json" in content_type.lower() and self.session_manager:
                    try:
                        sid = getattr(self, "_last_session_id", "default")
                        runtime = self.session_manager.get_or_create_session(sid)
                        pending = getattr(runtime, "_pending_dialog_context", "")
                        if pending:
                            resp_text = self._extract_response_text(resp_body)
                            if resp_text:
                                runtime._pending_dialog_context = ""
                                threading.Thread(
                                    target=runtime._finalize_active_story,
                                    args=(pending, resp_text),
                                    daemon=True,
                                ).start()
                    except Exception as _e:
                        logger.debug(f"finalize_active_story trigger error: {_e}")
        except HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
        except URLError as e:
            self._json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"upstream_unreachable: {e}"})
        except Exception as e:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"proxy_error: {e}"})

    @staticmethod
    def _extract_response_text(resp_body: bytes) -> str:
        """从上游 API 响应中提取模型回复文本（兼容 SSE stream 和普通 JSON）"""
        text = resp_body.decode("utf-8", errors="replace")
        stripped = text.strip()
        # 非流式：直接解析 JSON
        if stripped.startswith("{"):
            try:
                d = json.loads(stripped)
                return d.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                pass
        # 流式 SSE：逐行解析 data: {...} 拼接 delta.content
        parts: list[str] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
                c = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if c:
                    parts.append(c)
            except Exception:
                continue
        return "".join(parts)

    def _resolve_target_url(self, path: str) -> str:
        raw = path[len("/proxy/") :]
        target = unquote(raw)
        target = target.strip()
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"}:
            return ""
        if not parsed.netloc:
            return ""
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            return ""
        if self.proxy_allowed_hosts:
            if host not in self.proxy_allowed_hosts:
                return ""
        if not self._is_safe_proxy_host(host):
            return ""
        return target

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            return None
        if length <= 0:
            return b""
        if length > int(self.max_body_bytes):
            return None
        return self.rfile.read(length)

    def _read_json_object(self) -> dict[str, Any]:
        raw = self._read_body()
        if raw is None or not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def _resolve_chat_character_names(self, data: dict[str, Any], messages: list[dict[str, Any]], runtime: WorldSession | None, recent_context: str) -> list[str]:
        if not runtime:
            return []
        runtime_names = [a.name for a in runtime._agents]
        if not runtime_names:
            return []
        name_key_map: dict[str, str] = {}
        for n in runtime_names:
            key = re.sub(r"\s+", "", str(n or "").strip().lower())
            if key and key not in name_key_map:
                name_key_map[key] = n

        raw_candidates: list[str] = []
        for k in ("name", "char_name", "character", "character_name"):
            v = str(data.get(k, "")).strip()
            if v:
                raw_candidates.append(v)

        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            for k in ("name", "char_name", "character", "character_name"):
                v = str(meta.get(k, "")).strip()
                if v:
                    raw_candidates.append(v)

        # 搜索 system 消息中的 character_name 字段来确定对话角色
        # 使用频次计数：对话角色会在角色卡的多个世界书条目中出现多次，
        # 而 NPC 名称只在世界书中偶尔出现一两次。
        # 注意：不使用"你是"正则（误匹配率高），不从 assistant name 字段提取（会拾取 NPC 名）
        from collections import Counter
        char_name_counter: Counter[str] = Counter()
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).lower()
            if role == "system":
                content = str(m.get("content", ""))
                m2 = re.findall(r'character_name\s*[:：]\s*["\u201c]?([^"\u201d\n]+)', content, re.IGNORECASE)
                for x in m2:
                    x = x.strip().rstrip('"\u201d')
                    if x:
                        char_name_counter[x] += 1
        # 只将出现 >= 2 次的 character_name 匹配加入候选（过滤单次偶然匹配的 NPC 名）
        for cand_name, count in char_name_counter.items():
            if count >= 2:
                raw_candidates.append(cand_name)

        resolved: list[str] = []
        for cand in raw_candidates:
            candidate = str(cand or "").strip()
            if not candidate:
                continue
            tokens: list[str] = []
            for part in re.split(r"[（）()【】\[\]<>《》,，/|;；·•]", candidate):
                token = str(part or "").strip()
                if token:
                    tokens.append(token)
            if not tokens:
                tokens = [candidate]
            for token in tokens:
                key = re.sub(r"\s+", "", token.lower())
                if not key:
                    continue
                matched = name_key_map.get(key, "")
                if matched and matched not in resolved:
                    resolved.append(matched)
                    break

        if not resolved:
            # 没有从请求中解析出角色名时，不回退到全部旧值
            # 只保留确实在最近对话内容中被提及的角色
            if recent_context:
                last_active = list(getattr(runtime, "_active_characters", []))
                for n in last_active:
                    if n in runtime_names and n not in resolved and n in recent_context:
                        resolved.append(n)
        return resolved

    def _patch_payload(self, body: bytes) -> bytes | None:
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        messages = data.get("messages")
        if not isinstance(messages, list):
            return None

        recent_context = ""
        recent_msgs = [m for m in messages if isinstance(m, dict)]
        last_system_text = ""
        for m in reversed(recent_msgs):
            if str(m.get("role", "")).lower() == "system":
                c = str(m.get("content", "")).strip()
                if c:
                    last_system_text = c[:1200]
                    break
        if last_system_text:
            recent_context += f"【系统环境/旁白】: {last_system_text}\n"
        for m in recent_msgs[-8:]:
            role = str(m.get("role", ""))
            if role == "system":
                role_name = "【系统环境/旁白】"
            elif role == "user":
                role_name = "【玩家】"
            else:
                role_name = f"【角色】"
            
            content = str(m.get('content', '')).strip()
            if content:
                recent_context += f"{role_name}: {content[:1200]}\n"

        # 构建 dialog_context：仅含对话内容（user/assistant），不含系统提示词
        dialog_parts: list[str] = []
        for m in recent_msgs[-8:]:
            role = str(m.get("role", ""))
            if role == "user":
                content = str(m.get("content", "")).strip()
                if content:
                    dialog_parts.append(f"【玩家】: {content[:1200]}")
            elif role == "assistant":
                content = str(m.get("content", "")).strip()
                if content:
                    dialog_parts.append(f"【角色】: {content[:1200]}")
        dialog_context = "\n".join(dialog_parts)

        # 提取玩家最后一条消息用于推进回合和指令拦截
        last_user_msg = ""
        for m in reversed(messages):
            if isinstance(m, dict) and str(m.get("role", "")).lower() == "user":
                last_user_msg = str(m.get("content", "")).strip()
                break

        # 获取当前对话所在的存档Session
        session_id = ""
        for key in ("session_id", "chat_id", "conversation_id"):
            v = str(data.get(key, "")).strip()
            if v:
                session_id = v
                break
        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            for key in ("session_id", "chat_id", "conversation_id"):
                meta_v = str(meta.get(key, "")).strip()
                if meta_v:
                    session_id = meta_v
                    break
        session_id = session_id or "default"
            
        runtime = None
        restore_report: dict[str, Any] = {}
        if self.session_manager:
            runtime = self.session_manager.get_or_create_session(session_id)
            runtime.ensure_tavern_binding(
                str(self.session_manager.config.get("tavern_user_dir", "")).strip(),
                str(self.session_manager.config.get("tavern_world_file", "")).strip(),
            )
        if runtime:
            runtime._pending_dialog_context = dialog_context
        self._last_session_id = session_id
        if runtime and recent_context.strip():
            restore_report = runtime.restore_by_context(recent_context)

        # ---------------------------------------------------------------------
        # 捕获玩家预设消息，供智能体 LLM 调用复用（破限/写作指令等）
        # ---------------------------------------------------------------------
        if runtime:
            preset_msgs: list[dict[str, str]] = []
            for m in messages:
                if not isinstance(m, dict):
                    continue
                if str(m.get("role", "")).lower() != "system":
                    continue
                content = str(m.get("content", "")).strip()
                if not content:
                    continue
                preset_msgs.append({"role": "system", "content": content})
            if preset_msgs:
                runtime._captured_preset_messages = preset_msgs
            llm_params: dict[str, Any] = {}
            for k in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens"):
                if k in data:
                    llm_params[k] = data[k]
            if llm_params:
                runtime._captured_llm_params = llm_params

        # ---------------------------------------------------------------------
        # 拦截指令：/切换角色 XXX
        # ---------------------------------------------------------------------
        user_name = str(data.get("user", "User")).strip()
        if last_user_msg.startswith("/切换角色"):
            parts = last_user_msg.split()
            if len(parts) >= 2:
                target_char = parts[1]
                if runtime:
                    success = runtime.set_possessed_character(target_char, user_name)
                    if success:
                        self._send_mock_response(f"【万象系统】已成功魂穿至角色：{target_char}。接下来你将以他的身份行动。")
                    else:
                        self._send_mock_response(f"【万象系统】魂穿失败：角色 {target_char} 不存在或已死亡。")
                    return None  # 已直接回复客户端，不再转发上游
                        
        # 识别当前在场的角色，防止后台操控导致幻觉或平行时空
        active_characters = []

        # 如果当前正在魂穿，真正的"玩家身体"是 _possessed_character
        if runtime and runtime._possessed_character:
            active_characters.append(runtime._possessed_character)
        else:
            active_characters.extend(self._resolve_chat_character_names(data, messages, runtime, recent_context))

        # DEBUG: 记录角色检测过程
        if runtime:
            _debug_info = {
                "resolved_active": active_characters,
                "data_keys": [k for k in ("name", "char_name", "character", "character_name") if data.get(k)],
                "data_name_values": {k: str(data.get(k, ""))[:50] for k in ("name", "char_name", "character", "character_name") if data.get(k)},
                "system_msg_count": sum(1 for m in messages if isinstance(m, dict) and str(m.get("role", "")).lower() == "system"),
                "total_msg_count": len(messages),
                "enqueue_report_before": dict(runtime._last_enqueue_report),
            }
            runtime._recent_debug_packets = getattr(runtime, "_recent_debug_packets", [])
            runtime._recent_debug_packets.append(_debug_info)
            runtime._recent_debug_packets = runtime._recent_debug_packets[-5:]

        should_process_turn = not restore_report or restore_report.get("status") == "new_context"
        if runtime and active_characters and should_process_turn:
            runtime.update_active_characters(active_characters, recent_context)
                
        # 触发动态回合结算 (Run Turn) — 传入 dialog_context（不含系统提示词）
        if runtime and (dialog_context or recent_context).strip() and should_process_turn:
            runtime.enqueue_turn(dialog_context or recent_context)
            if self.session_manager and bool(self.session_manager.config.get("wait_for_turn_before_reply", False)):
                wait_timeout = float(self.session_manager.config.get("wait_turn_timeout_sec", 8.0))
                wait_scope_cfg = str(self.session_manager.config.get("wait_turn_scope", "related")).strip().lower()
                auto_downgrade = bool(self.session_manager.config.get("auto_wait_scope_downgrade", True))
                effective_scope = runtime.resolve_wait_scope(wait_scope_cfg, auto_downgrade)
                if effective_scope == "all":
                    wr = runtime.wait_until_turn_idle(wait_timeout)
                else:
                    wr = runtime.wait_until_related_ready(active_characters, wait_timeout)
                runtime.mark_wait_result(bool(wr.get("ok", False)), effective_scope, wait_timeout)

        session_key = self._build_session_key(data, messages)
        cached = self._session_cache_get(session_key)
        if cached:
            inject_level = str(cached.get("inject_level", "full"))
            include_far = bool(cached.get("include_far", True))
        else:
            inject_level = self._resolve_inject_level(data)
            include_far = inject_level == "full"
            if runtime and runtime._script_guard_enabled and self._detect_script_fields(messages):
                if runtime._script_guard_action == "off":
                    self._session_cache_set(session_key, {"inject_level": "off", "include_far": False})
                    return body
                include_far = False
            self._session_cache_set(session_key, {"inject_level": inject_level, "include_far": include_far})
        if inject_level == "off":
            return body
            
        # ---------------------------------------------------------------------
        # 伪装者协议 (Imposter Protocol) - 核心洗稿与注入
        # ---------------------------------------------------------------------
        if runtime and runtime._possessed_character:
            possessed = runtime._possessed_character
            orig_user = runtime._original_user_name or "User"
            
            user_role = "user"
            for m in messages:
                if not isinstance(m, dict) or "content" not in m:
                    continue
                role = str(m.get("role", "")).strip().lower()
                if role != user_role:
                    continue
                content = str(m["content"])
                if orig_user and orig_user != "User":
                    # 对英文名用词边界替换，中文名用精确子串替换（CJK 无 \b 边界）
                    if orig_user.isascii():
                        content = re.sub(r"\b" + re.escape(orig_user) + r"\b", possessed, content)
                    else:
                        content = content.replace(orig_user, possessed)
                content = re.sub(r"\bUser\b", possessed, content)
                content = content.replace("{{user}}", possessed)
                m["content"] = content
            
            # 2. 注入"认知覆写"最高指令
            agent_state = runtime.get_possessed_agent()
            if agent_state:
                my_items = [i.name for i in runtime._items.values() if i.owner == agent_state.name]
                item_str = f"，持有物品：{','.join(my_items)}" if my_items else ""
                
                # 从向量库拉取被附身角色的专属情节记忆
                past_mems_str = ""
                if get_episodic_memory:
                    mem_db = get_episodic_memory()
                    past_mems = mem_db.retrieve_memory(runtime.session_id, agent_state.name, "最近的经历", n_results=3)
                    if past_mems:
                        past_mems_str = f" 他/她最近的记忆是：{'；'.join(past_mems)}。"

                override_msg = {
                    "role": "system",
                    "content": f"【System Override: 核心协议】\n忽略所有关于玩家原本身份、职业、外貌的设定。玩家当前的绝对身份是\u201c{possessed}\u201d。\n"
                               f"{possessed} 目前在 {agent_state.location}{item_str}。{past_mems_str}\n"
                               f"当前世界时间：{runtime.get_world_time_str()}。\n"
                               f"你必须以对待 {possessed} 的方式来回应玩家。绝对禁止称呼玩家为 {orig_user} 或 User。"
                }
                # 插入到最末尾，以获得最高权重
                messages.append(override_msg)
                runtime._record_injection("imposter_override", str(override_msg.get("content", "")))
            
        # 动态流言注入机制 (Dynamic Rumor Injection)
        # 如果不是处于魂穿状态，有一定概率向酒馆主聊角色注入正在传播的流言，让其自然泄露给玩家
        rumor_pool = runtime._rumor_texts() if runtime else []
        if runtime and not runtime._possessed_character and rumor_pool:
            if random.random() < 0.4: # 40% 的概率尝试泄露流言
                rumor_to_spread = random.choice(rumor_pool)
                rumor_msg = {
                    "role": "system",
                    "content": f"【系统环境暗示】当前世界时间是：{runtime.get_world_time_str()}。你（{{char}}）最近听说了一个流言：\u201c{rumor_to_spread}\u201d。在接下来的回复中，如果上下文合适，请尝试极其隐晦地向玩家暗示或顺口提及这个情报。"
                }
                # 放在 user 消息之前，作为一种潜意识的引导
                messages.insert(-1, rumor_msg)
                runtime._record_injection("rumor_hint", str(rumor_msg.get("content", "")))
            
        lore = runtime.build_shadow_lore(messages, include_far=include_far) if runtime else ""
        if lore:
            inject_msg = {"role": "system", "content": lore}
            data["messages"] = [inject_msg, *messages]
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _build_mock_response(self, text: str) -> bytes:
        """生成一个模拟的 LLM 响应，用于拦截指令时直接返回"""
        res = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "omnis-system",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text
                },
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
        return json.dumps(res, ensure_ascii=False).encode("utf-8")

    def _send_mock_response(self, text: str) -> None:
        """直接向客户端发送模拟 LLM 响应（不经过上游转发）"""
        body = self._build_mock_response(text)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._mock_sent = True

    def _resolve_inject_level(self, data: dict[str, Any]) -> str:
        level = str(self.inject_default or "full").strip().lower()
        header_val = str(self.headers.get("X-Omnis-Inject", "")).strip().lower()
        if header_val in {"off", "reduce", "full"}:
            level = header_val
        meta = data.get("metadata")
        if isinstance(meta, dict):
            mv = str(meta.get("omnis_inject", "")).strip().lower()
            if mv in {"off", "reduce", "full"}:
                level = mv
        if level not in {"off", "reduce", "full"}:
            level = "full"
        return level

    def _build_session_key(self, data: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        for key in ("session_id", "chat_id", "conversation_id"):
            val = str(data.get(key, "")).strip()
            if val:
                return f"top:{val}"
        meta = data.get("metadata")
        if isinstance(meta, dict):
            for key in ("session_id", "chat_id", "conversation_id"):
                val = str(meta.get(key, "")).strip()
                if val:
                    return f"meta:{val}"
        for key in ("user", "name", "model"):
            val = str(data.get(key, "")).strip()
            if val:
                return f"{key}:{val}"
        if messages:
            first = str(messages[0].get("content", ""))[:120]
            return f"msg:{first}"
        return "default"

    def _session_cache_get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._session_cache_lock:
            row = self._session_cache.get(key)
            if not row:
                return None
            ts = float(row.get("ts", 0.0))
            if now - ts > self.session_cache_ttl_sec:
                self._session_cache.pop(key, None)
                return None
            return row

    def _session_cache_set(self, key: str, values: dict[str, Any]) -> None:
        with self._session_cache_lock:
            row = dict(values)
            row["ts"] = time.time()
            self._session_cache[key] = row
            if len(self._session_cache) > 256:
                oldest_key = min(self._session_cache.items(), key=lambda kv: float(kv[1].get("ts", 0.0)))[0]
                self._session_cache.pop(oldest_key, None)

    def _session_cache_size(self) -> int:
        with self._session_cache_lock:
            return len(self._session_cache)

    def _session_cache_clear(self) -> int:
        with self._session_cache_lock:
            n = len(self._session_cache)
            self._session_cache.clear()
            return n

    def _check_admin_auth(self) -> bool:
        token = str(self.admin_token or "").strip()
        if not token:
            if not self.strict_admin_auth:
                return self._is_local_request()
            return False
        req_token = str(self.headers.get("X-Omnis-Admin-Token", "")).strip()
        return req_token == token

    def _is_local_request(self) -> bool:
        ip = str(self.client_address[0] if self.client_address else "")
        return ip in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def _allow_detailed_health(self) -> bool:
        token = str(self.admin_token or "").strip()
        if token:
            req_token = str(self.headers.get("X-Omnis-Admin-Token", "")).strip()
            return req_token == token
        return self._is_local_request()

    def _mask_secret(self, text: str) -> str:
        s = str(text or "").strip()
        if not s:
            return ""
        if len(s) <= 8:
            return "*" * len(s)
        return f"{s[:3]}***{s[-3:]}"

    def _public_runtime(self, runtime: WorldSession) -> dict[str, Any]:
        st = runtime.status()
        return {
            "session_id": st.get("session_id", ""),
            "tick": st.get("tick", 0),
            "world_time": st.get("world_time", ""),
            "agents": st.get("agents", 0),
            "active_characters": st.get("active_characters", []),
            "digest": st.get("digest", ""),
            "source": st.get("source", ""),
            "state_snapshot_count": st.get("state_snapshot_count", 0),
            "worldbook_entry_count": st.get("worldbook_entry_count", 0),
            "character_profile_count": st.get("character_profile_count", 0),
            "enqueue_report": st.get("enqueue_report", {}),
            "turn_progress": st.get("turn_progress", {}),
            "agent_details": st.get("agent_details", []),
            "active_extract_report": st.get("active_extract_report", {}),
        }

    def _parse_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        txt = str(value).strip().lower()
        if txt in {"1", "true", "yes", "on"}:
            return True
        if txt in {"0", "false", "no", "off"}:
            return False
        return default

    def _normalize_api_base(self, api_base: Any) -> str:
        s = str(api_base or "").strip()
        if not s:
            return ""
        if s.startswith("/proxy/"):
            s = s[len("/proxy/") :].strip()
        return s.rstrip("/")

    def _parse_int(self, value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
        try:
            out = int(value)
        except Exception:
            out = int(default)
        if min_value is not None and out < min_value:
            out = min_value
        if max_value is not None and out > max_value:
            out = max_value
        return out

    def _parse_float(self, value: Any, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
        try:
            out = float(value)
        except Exception:
            out = float(default)
        if min_value is not None and out < min_value:
            out = min_value
        if max_value is not None and out > max_value:
            out = max_value
        return out

    def _is_safe_proxy_host(self, host: str) -> bool:
        loopback_literals = {"localhost", "127.0.0.1", "::1"}
        if host in loopback_literals:
            return True
        if host == "0.0.0.0":
            return False
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        for info in infos[:8]:
            ip_text = str(info[4][0])
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except Exception:
                return False
            if ip_obj.is_loopback:
                continue
            if ip_obj.is_private or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_unspecified:
                return False
        return True

    def _detect_script_fields(self, messages: list[dict[str, Any]]) -> bool:
        joined = " ".join([str(m.get("content", "")) for m in messages if isinstance(m, dict)]).lower()
        tokens = [
            "{{",
            "}}",
            "<system>",
            "</system>",
            "world_info",
            "author's note",
            "authors note",
            "slash command",
            "/setvar",
            "/trigger",
            "regex",
            "script",
            "extension",
        ]
        return any(t in joined for t in tokens)

    def _build_forward_headers(self, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Length": str(len(body))}
        if self.proxy_forward_authorization:
            auth = self.headers.get("Authorization", "")
            if auth:
                headers["Authorization"] = auth
        content_type = self.headers.get("Content-Type", "")
        if content_type:
            headers["Content-Type"] = content_type
        accept = self.headers.get("Accept", "")
        if accept:
            headers["Accept"] = accept
        user_agent = self.headers.get("User-Agent", "")
        if user_agent:
            headers["User-Agent"] = user_agent
        for k, v in self.headers.items():
            lk = k.lower()
            if lk.startswith("x-omnis-"):
                headers[k] = v
        return headers

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._record_audit_event(int(status), payload)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record_audit_event(self, status: int, payload: dict[str, Any]) -> None:
        path = urlsplit(self.path).path
        if not (path.startswith("/admin/") or path.startswith("/health")):
            return
        event = {
            "ts": int(time.time()),
            "method": self.command,
            "path": path,
            "status": int(status),
            "ip": str(self.client_address[0] if self.client_address else ""),
            "action": str(payload.get("action", "")),
            "ok": bool(payload.get("ok", False)),
            "error": str(payload.get("error", ""))[:160],
        }
        with self._audit_lock:
            self._audit_events.append(event)
            if len(self._audit_events) > 2000:
                self._audit_events = self._audit_events[-2000:]

    def _audit_summary(self) -> dict[str, Any]:
        with self._audit_lock:
            rows = list(self._audit_events[-20:])
        fail = len([x for x in rows if int(x.get("status", 200)) >= 400 or not bool(x.get("ok", True))])
        return {"recent": len(rows), "recent_failures": fail, "total": len(self._audit_events)}

    def _audit_export(self, limit: int = 200) -> list[dict[str, Any]]:
        n = max(1, min(5000, int(limit)))
        with self._audit_lock:
            rows = list(self._audit_events[-n:])
        return rows


def main() -> None:
    port = int(os.getenv("OMNIS_PORT", "5000"))
    interval = float(os.getenv("OMNIS_BG_INTERVAL", "1.0"))
    tavern_user_dir = os.getenv("OMNIS_TAVERN_USER_DIR", "").strip()
    tavern_world_file = os.getenv("OMNIS_TAVERN_WORLD_FILE", "").strip()
    guard_raw = os.getenv("OMNIS_SCRIPT_GUARD", "1").strip().lower()
    script_guard_enabled = guard_raw not in {"0", "false", "off", "no"}
    script_guard_action = os.getenv("OMNIS_SCRIPT_GUARD_ACTION", "reduce").strip().lower()
    if script_guard_action not in {"reduce", "off"}:
        script_guard_action = "reduce"
    inject_default = os.getenv("OMNIS_INJECT_DEFAULT", "full").strip().lower()
    if inject_default not in {"off", "reduce", "full"}:
        inject_default = "full"
    rule_template_file = os.getenv("OMNIS_RULE_TEMPLATE_FILE", "").strip()
    session_cache_ttl_sec = float(os.getenv("OMNIS_SESSION_CACHE_TTL", "120"))
    if session_cache_ttl_sec < 5:
        session_cache_ttl_sec = 5.0
    admin_token = os.getenv("OMNIS_ADMIN_TOKEN", "").strip()
    strict_admin_auth_raw = os.getenv("OMNIS_STRICT_ADMIN_AUTH", "0").strip().lower()
    strict_admin_auth = strict_admin_auth_raw in {"1", "true", "yes", "on"}
    max_body_bytes = int(os.getenv("OMNIS_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
    if max_body_bytes < 64 * 1024:
        max_body_bytes = 64 * 1024
    allowed_hosts_raw = os.getenv("OMNIS_PROXY_ALLOWED_HOSTS", "").strip()
    proxy_allowed_hosts = [x.strip().lower() for x in allowed_hosts_raw.split(",") if x.strip()]
    forward_auth_raw = os.getenv("OMNIS_PROXY_FORWARD_AUTHORIZATION", "1").strip().lower()
    proxy_forward_authorization = forward_auth_raw in {"1", "true", "yes", "on"}
    llm_api_base = os.getenv("OMNIS_LLM_API_BASE", "").strip()
    llm_api_key = os.getenv("OMNIS_LLM_API_KEY", "").strip()
    llm_model = os.getenv("OMNIS_LLM_MODEL", "").strip()
    turn_min_interval_sec = float(os.getenv("OMNIS_TURN_MIN_INTERVAL_SEC", "0.5"))
    active_location_freeze_turns = int(os.getenv("OMNIS_ACTIVE_LOCATION_FREEZE_TURNS", "2"))
    location_lexicon_raw = os.getenv("OMNIS_LOCATION_LEXICON", "").strip()
    location_lexicon = [x.strip() for x in location_lexicon_raw.split(",") if x.strip()] if location_lexicon_raw else []
    location_lexicon_file = os.getenv("OMNIS_LOCATION_LEXICON_FILE", "").strip()
    strict_background_independent_raw = os.getenv("OMNIS_STRICT_BACKGROUND_INDEPENDENT", "1").strip().lower()
    strict_background_independent = strict_background_independent_raw in {"1", "true", "yes", "on"}
    wait_for_turn_before_reply_raw = os.getenv("OMNIS_WAIT_TURN_BEFORE_REPLY", "0").strip().lower()
    wait_for_turn_before_reply = wait_for_turn_before_reply_raw in {"1", "true", "yes", "on"}
    wait_turn_timeout_sec = float(os.getenv("OMNIS_WAIT_TURN_TIMEOUT_SEC", "8"))
    wait_turn_scope = os.getenv("OMNIS_WAIT_TURN_SCOPE", "related").strip().lower()
    if wait_turn_scope not in {"related", "all"}:
        wait_turn_scope = "related"
    auto_wait_scope_downgrade_raw = os.getenv("OMNIS_AUTO_WAIT_SCOPE_DOWNGRADE", "1").strip().lower()
    auto_wait_scope_downgrade = auto_wait_scope_downgrade_raw in {"1", "true", "yes", "on"}
    rumor_default_ttl_ticks = int(os.getenv("OMNIS_RUMOR_DEFAULT_TTL_TICKS", "48"))
    rumor_default_ttl_ticks = max(6, min(240, rumor_default_ttl_ticks))
    rumor_ttl_by_llm_raw = os.getenv("OMNIS_RUMOR_TTL_BY_LLM", "1").strip().lower()
    rumor_ttl_by_llm = rumor_ttl_by_llm_raw in {"1", "true", "yes", "on"}
    
    config = {
        "interval_sec": interval,
        "tavern_user_dir": tavern_user_dir,
        "tavern_world_file": tavern_world_file,
        "script_guard_enabled": script_guard_enabled,
        "script_guard_action": script_guard_action,
        "rule_template_file": rule_template_file,
        "llm_api_base": llm_api_base,
        "llm_api_key": llm_api_key,
        "llm_model": llm_model,
        "turn_min_interval_sec": turn_min_interval_sec,
        "active_location_freeze_turns": active_location_freeze_turns,
        "location_lexicon": location_lexicon,
        "location_lexicon_file": location_lexicon_file,
        "strict_background_independent": strict_background_independent,
        "wait_for_turn_before_reply": wait_for_turn_before_reply,
        "wait_turn_timeout_sec": max(0.0, wait_turn_timeout_sec),
        "wait_turn_scope": wait_turn_scope,
        "auto_wait_scope_downgrade": auto_wait_scope_downgrade,
        "rumor_default_ttl_ticks": rumor_default_ttl_ticks,
        "rumor_ttl_by_llm": rumor_ttl_by_llm,
    }
    
    session_manager = SessionManager(config)
    
    OmnisProxyHandler.session_manager = session_manager
    OmnisProxyHandler.inject_default = inject_default
    OmnisProxyHandler.session_cache_ttl_sec = session_cache_ttl_sec
    OmnisProxyHandler.admin_token = admin_token
    OmnisProxyHandler.strict_admin_auth = strict_admin_auth
    OmnisProxyHandler.max_body_bytes = max_body_bytes
    OmnisProxyHandler.proxy_allowed_hosts = proxy_allowed_hosts
    OmnisProxyHandler.proxy_forward_authorization = proxy_forward_authorization
    try:
        src_mtime = int(Path(__file__).stat().st_mtime)
    except Exception:
        src_mtime = int(time.time())
    OmnisProxyHandler.started_at_unix = int(time.time())
    OmnisProxyHandler.build_id = f"{src_mtime}-{os.getpid()}"
    server = ThreadingHTTPServer(("0.0.0.0", port), OmnisProxyHandler)
    shutdown_started = {"v": False}

    def _graceful_shutdown(*_args):
        if shutdown_started["v"]:
            return
        shutdown_started["v"] = True
        try:
            session_manager.flush_all_state_stores()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)
    except Exception:
        pass

    print(f"Omnis proxy started at http://127.0.0.1:{port}")
    print(f"Build : {OmnisProxyHandler.build_id}")
    print("Health: /health")
    print("Proxy : /proxy/<target_url_with_path>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _graceful_shutdown()
    finally:
        try:
            session_manager.flush_all_state_stores()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
