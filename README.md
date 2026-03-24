# Omnis（万象）

[English README](./README_EN.md)

SillyTavern 的后台世界增强代理。在你和角色聊天的同时，万象在幕后驱动整个世界运转——NPC 有自己的意图和行动，会互相交谈、争执、交易，世界时间在流逝，流言在传播，关系在变化。

## 它能做什么

**你用酒馆正常聊天，万象在背后做这些事：**

- 后台 NPC 每轮独立思考和行动（去哪里、做什么、和谁互动）
- 当多个 NPC 在同一地点相遇，DM（主持人）自动裁定他们的交互结果
- NPC 之间会产生对话、关系变化、物品转移、甚至死亡
- 流言在 NPC 之间传播，并可能在聊天中被暗示给你
- 世界有昼夜循环，深夜 NPC 活动减少
- 你可以切换到任意 NPC 身上，以他的身份体验世界
- 所有这些状态变化会作为"世界事实"注入到你和角色的对话中，让模型的回复自然反映世界动态

**对酒馆完全无侵入**——不改酒馆代码、不改 API Key 配置、不改角色卡，只需把酒馆的 API 地址指向万象代理。

## 核心特色

**三阶段推演架构**：NPC 先声明意图 → DM 裁定交互结果 → 生成最终叙事。避免两个 NPC 各说各话，确保交互有因果关系。

**自适应等待**：可配置是否等 NPC 回合结束再回复你。超时自动降级，不会让你等太久。

**全功能管理面板**：实时查看所有 NPC 的位置、意图、日志、关系图谱、物品流转，一键调整所有运行参数。

## 项目结构

```
omnis/
├── omnis/                         # 万象核心
│   ├── omnis_proxy.py             # 主服务：代理、会话、NPC 推演、DM 裁定
│   ├── omnis_llm.py               # LLM 调用客户端（DeepSeek/OpenAI 兼容）
│   ├── models.py                  # 数据结构（AgentState, ItemState）
│   ├── turn_engine.py             # DM 场景构建
│   ├── proxy_handler.py           # ETag 工具
│   ├── world_session.py           # 状态快照工具
│   ├── memory.py                  # 向量记忆（可选，需 ChromaDB）
│   ├── dashboard.html             # 管理面板（纯前端）
│   ├── omnis_config.example.json  # 配置模板
│   ├── location_lexicon.json      # 地点词典
│   └── rule_templates.default.json
├── src/tavern/                    # 提示词引擎
│   ├── engine.py                  # TavernEngine（NPC prompt 构建）
│   ├── history.py                 # 对话历史管理
│   ├── card_manager.py            # 角色卡解析
│   └── renderer.py                # Jinja2 模板渲染
├── src/presets/                   # LLM 指令格式预设
├── storage/run_records/           # 运行时状态（自动生成）
├── .env.example                   # 环境变量模板
└── requirements.txt               # Python 依赖
```

## 快速开始

### 1. 环境准备

需要 Python 3.10+。

```bash
pip install jinja2
```

可选（NPC 向量记忆增强）：
```bash
pip install chromadb onnxruntime
```

### 2. 配置

复制配置模板：
```bash
cp .env.example .env
cp omnis/omnis_config.example.json omnis/omnis_config.json
```

编辑 `.env`，填入你的 LLM API Key：
```
OMNIS_LLM_API_BASE=如:https://api.deepseek.com/v1
OMNIS_LLM_API_KEY=sk-你的密钥
OMNIS_LLM_MODEL=deepseek-chat
```

编辑 `omnis/omnis_config.json`（可选）：
```json
{
    "tavern_user_dir": "你的酒馆路径/data/default-user",
    "tavern_world_file": "你的酒馆路径/data/default-user/worlds/你的世界书.json"
}
```

留空时会按默认路径尝试：

`项目根目录/酒馆/data/default-user`

如果你的酒馆不在该位置，再手动填写上面两个路径即可。

> API Key 只通过环境变量注入，不会被写入配置文件。

### 3. 启动

```bash
python omnis/omnis_proxy.py
```

看到以下输出表示启动成功：
```
Omnis proxy started at http://127.0.0.1:5000
Health: /health
Proxy : /proxy/<target_url_with_path>
```

### 4. 酒馆连接

在 SillyTavern 的 API 设置中，把 API URL 改为：

```
http://127.0.0.1:5000/proxy/https://api.deepseek.com/v1（或其他链接）
```

其他设置（API Key、模型名）保持原样。酒馆的 API Key 用于主聊天，万象的 API Key 用于后台 NPC 推演，两者互不干扰。

### 5. 管理面板

浏览器打开 `http://127.0.0.1:5000`，即可看到万象管理面板。

## 管理面板功能

面板左侧是系统状态和配置，右侧是 NPC 列表和世界信息。

**你可以做的事：**
- 查看所有 NPC 的位置、意图、活跃度、日志
- 点击任意 NPC 查看详细记忆、物品、关系图谱
- 一键"切换"到某个 NPC
- 开关某个 NPC 的 AI 控制
- 修改 LLM 配置和所有运行参数
- 查看 DM 裁定记录、注入日志、性能指标
- 清除角色数据或整个会话
- 导出调试包和审计日志

## 玩家命令

在酒馆聊天框中直接输入：

| 命令 | 效果 |
|------|------|
| `/切换角色 张三` | 魂穿到张三，以他的身份行动 |
| `/退出魂穿` | 解除魂穿，回到自己的身体 |

切换期间，模型会以被附身角色的口吻回复你，其他 NPC 也会把你当作该角色对待。

## 运行参数

所有参数都可以通过面板、`omnis_config.json`、或环境变量设置。

### 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `wait_for_turn_before_reply` | `true` | 是否等 NPC 回合完成再回复你 |
| `wait_turn_timeout_sec` | `8` | 等待超时秒数 |
| `wait_turn_scope` | `related` | 等待范围：`related`（相关角色）或 `all`（全部） |
| `auto_wait_scope_downgrade` | `true` | 连续超时时自动降级等待范围 |
| `turn_min_interval_sec` | `0.9` | NPC 回合最小间隔 |
| `max_journal_limit` | `10` | 每个 NPC 的日志上限 |
| `dynamic_lore_writeback` | `true` | DM 生成的新设定是否回写到世界书 |
| `strict_background_independent` | `true` | NPC 只在对话上下文提及时才与他人互动 |
| `rumor_default_ttl_ticks` | `48` | 流言默认存活时间（tick，1 tick = 1 小时） |

### 推荐组合

**低延迟（不等 NPC）**：
```json
{ "wait_for_turn_before_reply": false }
```

**平衡（推荐）**：
```json
{
    "wait_for_turn_before_reply": true,
    "wait_turn_timeout_sec": 8,
    "wait_turn_scope": "related",
    "auto_wait_scope_downgrade": true
}
```

## 环境变量一览

| 变量 | 说明 |
|------|------|
| `OMNIS_PORT` | 监听端口，默认 `5000` |
| `OMNIS_LLM_API_BASE` | 后台 NPC 用的 LLM API 地址 |
| `OMNIS_LLM_API_KEY` | 后台 NPC 用的 API Key |
| `OMNIS_LLM_MODEL` | 后台 NPC 用的模型名 |
| `OMNIS_ADMIN_TOKEN` | 管理接口令牌（可选） |
| `OMNIS_INJECT_DEFAULT` | 默认注入等级：`full`/`reduce`/`off` |
| `OMNIS_TAVERN_USER_DIR` | 酒馆用户目录 |
| `OMNIS_TAVERN_WORLD_FILE` | 世界书 JSON 文件路径 |

## 工作原理简述

```
玩家消息 → 酒馆 → 万象代理 → 上游 LLM（主聊天）
                      ↓
               注入世界状态补丁
                      ↓
              后台 NPC 推演（异步）
              ├─ Phase 1: 每个 NPC 独立声明意图
              ├─ Phase 2: DM 裁定同地点 NPC 的交互
              └─ Phase 3: 生成最终叙事，写入日志
```

万象不修改酒馆发出的消息内容，只在消息列表头部插入一条 system 消息，包含当前世界的近场角色、远场态势、规则摘要等。上游 LLM 看到这些事实后，会自然地在回复中体现世界动态。

## 许可

MIT License
