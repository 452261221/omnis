from __future__ import annotations

from typing import Any


def build_dm_scenario(
    target_loc: str,
    group: list[Any],
    identities: dict[str, dict[str, str]],
    items: dict[str, Any],
    *,
    relationships: dict[str, dict[str, int]] | None = None,
    actor_results: dict[str, dict[str, Any]] | None = None,
) -> str:
    parts: list[str] = [f"地点：{target_loc}。"]
    group_names = {str(getattr(x, "name", "")) for x in group}

    # 硬性身份关系
    identity_lines: list[str] = []
    for a in group:
        name = str(getattr(a, "name", ""))
        if name in identities:
            for tgt_name, rel_type in identities[name].items():
                if str(tgt_name) in group_names:
                    identity_lines.append(f"[{name}]认为[{tgt_name}]是{rel_type}。")
    if identity_lines:
        parts.append("已知身份关系：")
        parts.extend(identity_lines)

    # 软性好感度
    if relationships:
        rel_lines: list[str] = []
        for a in group:
            name = str(getattr(a, "name", ""))
            if name in relationships:
                for tgt, score in relationships[name].items():
                    if str(tgt) in group_names and int(score) != 0:
                        rel_lines.append(f"[{name}]对[{tgt}]好感度: {int(score)}")
        if rel_lines:
            parts.append("好感度（-100仇恨 ~ +100亲密）：")
            parts.extend(rel_lines)

    # 角色详情：intent + narrative + 性格
    _actor_results = actor_results or {}
    for a in group:
        name = str(getattr(a, "name", ""))
        intent = str(getattr(a, "intent", ""))
        summary = str(getattr(a, "summary", ""))[:80]
        held_items = [str(getattr(i, "name", "")) for i in items.values() if str(getattr(i, "owner", "")) == name]
        item_str = f" (持有: {','.join(held_items)})" if held_items else ""
        line = f"角色[{name}]{item_str} 意图：{intent}"
        if summary and summary != "暂无简介":
            line += f" | 性格：{summary}"
        ar = _actor_results.get(name, {})
        narrative = str(ar.get("narrative", "")).strip()
        if narrative:
            line += f"\n  本轮行为: {narrative[:150]}"
        parts.append(line)

    # 地上物品
    loc_items = [str(getattr(i, "name", "")) for i in items.values() if not str(getattr(i, "owner", "")).strip() and str(getattr(i, "location", "")) == target_loc]
    if loc_items:
        parts.append(f"该地点掉落的物品：{','.join(loc_items)}")
    return "\n".join(parts)
