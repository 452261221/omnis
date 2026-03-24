# Omnis

[中文 README](./README.md)

Omnis is a backend world-enhancement proxy for SillyTavern. While you keep chatting with a character as usual, Omnis runs the world in the background—NPCs form their own intentions, move around, talk, argue, trade, spread rumors, and change their relationships over time.

## What It Does

**You keep using SillyTavern normally, while Omnis handles these systems behind the scenes:**

- NPCs think and act independently every turn
- When multiple NPCs meet at the same location, a DM layer resolves their interaction outcome
- NPCs can talk to each other, change relationships, transfer items, or even die
- Rumors spread between NPCs and can surface naturally in chat
- The world has a day-night cycle, and NPC activity drops late at night
- You can possess any NPC and experience the world from their perspective
- All of these state changes are injected into the conversation as world facts, so the model reflects a living world naturally

**Fully non-invasive to SillyTavern**—no code changes, no card edits, no API key rewiring inside SillyTavern. You only point the API URL to the Omnis proxy.

## Core Features

**Three-phase simulation loop**: NPCs declare intentions first, then a DM layer resolves interactions, and finally Omnis generates the resulting narrative. This avoids contradictory NPC outputs and keeps interactions causal.

**Adaptive waiting**: You can choose whether Omnis waits for NPC turns to finish before replying. When the wait takes too long, it automatically downgrades scope to avoid long stalls.

**Full admin dashboard**: Inspect NPC locations, intentions, logs, relations, and item transfers in real time, and tweak runtime settings from a browser.

## Project Structure

```text
omnis/
├── omnis/                         # Core runtime
│   ├── omnis_proxy.py             # Main service: proxy, sessions, NPC simulation, DM resolution
│   ├── omnis_llm.py               # LLM client (DeepSeek/OpenAI compatible)
│   ├── models.py                  # Data models (AgentState, ItemState)
│   ├── turn_engine.py             # DM scene builder
│   ├── proxy_handler.py           # ETag utilities
│   ├── world_session.py           # State snapshot utilities
│   ├── memory.py                  # Vector memory (optional, requires ChromaDB)
│   ├── dashboard.html             # Admin dashboard
│   ├── omnis_config.example.json  # Config template
│   ├── location_lexicon.json      # Location lexicon
│   └── rule_templates.default.json
├── src/tavern/                    # Prompt engine
│   ├── engine.py                  # TavernEngine (NPC prompt building)
│   ├── history.py                 # Chat history management
│   ├── card_manager.py            # Character card parsing
│   └── renderer.py                # Jinja2 template rendering
├── src/presets/                   # Instruction presets
├── storage/run_records/           # Runtime state output
└── requirements.txt               # Python dependencies
```

## Quick Start

### 1. Requirements

Python 3.10+ is required.

```bash
pip install jinja2
```

Optional for enhanced NPC vector memory:

```bash
pip install chromadb onnxruntime
```

### 2. Configuration

Create a `.env` file and fill in your LLM settings:

```text
OMNIS_LLM_API_BASE=https://api.deepseek.com/v1
OMNIS_LLM_API_KEY=your-key
OMNIS_LLM_MODEL=deepseek-chat
```

Optionally create `omnis/omnis_config.json`:

```json
{
  "tavern_user_dir": "your-sillytavern-path/data/default-user",
  "tavern_world_file": "your-sillytavern-path/data/default-user/worlds/your-worldbook.json"
}
```

If you leave these two fields empty, Omnis tries the default path:

`<project-root>/酒馆/data/default-user`

If your SillyTavern installation is elsewhere, fill in the two paths manually.

> API keys are loaded from environment variables only and are not written into the config file.

### 3. Run

```bash
python omnis/omnis_proxy.py
```

Startup is successful when you see:

```text
Omnis proxy started at http://127.0.0.1:5000
Health: /health
Proxy : /proxy/<target_url_with_path>
```

### 4. Connect SillyTavern

In SillyTavern API settings, change the API URL to:

```text
http://127.0.0.1:5000/proxy/https://api.deepseek.com/v1
```

Leave the rest of the SillyTavern settings as they are. The SillyTavern API key continues to power your main chat, while the Omnis API key is used for background NPC simulation.

### 5. Admin Dashboard

Open `http://127.0.0.1:5000` in your browser.

## Dashboard

The left side shows system status and configuration, and the right side shows NPC lists and world information.

**You can:**

- View all NPC locations, intentions, activity, and logs
- Click any NPC to inspect memory, items, and relation graphs
- Possess an NPC with one click
- Toggle AI control for any NPC
- Update LLM and runtime settings
- Inspect DM ruling logs, injection logs, and performance metrics
- Clear character data or the entire session
- Export debug bundles and audit logs

## Player Commands

Use these directly in the SillyTavern input box:

| Command | Effect |
|------|------|
| `/切换角色 张三` | Possess Zhang San and act as that character |
| `/退出魂穿` | Stop possession and return to your original identity |

While possessed, the model speaks in that NPC's voice and other NPCs treat you as that character.

## Runtime Settings

All runtime settings can be configured through the dashboard, `omnis_config.json`, or environment variables.

### Key Settings

| Key | Default | Meaning |
|------|--------|------|
| `wait_for_turn_before_reply` | `true` | Wait for NPC turns before replying |
| `wait_turn_timeout_sec` | `8` | Wait timeout in seconds |
| `wait_turn_scope` | `related` | Scope: `related` or `all` |
| `auto_wait_scope_downgrade` | `true` | Downgrade wait scope after repeated timeouts |
| `turn_min_interval_sec` | `0.9` | Minimum interval between NPC turns |
| `max_journal_limit` | `10` | Max log entries per NPC |
| `dynamic_lore_writeback` | `true` | Write new DM-generated lore back into worldbook |
| `strict_background_independent` | `true` | NPCs only interact when referenced by context |
| `rumor_default_ttl_ticks` | `48` | Default rumor lifetime in ticks |

### Recommended Profiles

**Low latency**:

```json
{ "wait_for_turn_before_reply": false }
```

**Balanced**:

```json
{
  "wait_for_turn_before_reply": true,
  "wait_turn_timeout_sec": 8,
  "wait_turn_scope": "related",
  "auto_wait_scope_downgrade": true
}
```

## Environment Variables

| Variable | Meaning |
|------|------|
| `OMNIS_PORT` | Listen port, default `5000` |
| `OMNIS_LLM_API_BASE` | LLM API base for background NPC simulation |
| `OMNIS_LLM_API_KEY` | LLM API key for background NPC simulation |
| `OMNIS_LLM_MODEL` | Model name for background NPC simulation |
| `OMNIS_ADMIN_TOKEN` | Admin token for management endpoints |
| `OMNIS_INJECT_DEFAULT` | Default injection level: `full` / `reduce` / `off` |
| `OMNIS_TAVERN_USER_DIR` | SillyTavern user directory |
| `OMNIS_TAVERN_WORLD_FILE` | Worldbook JSON path |

## How It Works

```text
Player message -> SillyTavern -> Omnis proxy -> upstream LLM (main chat)
                                     ↓
                           Inject world-state patch
                                     ↓
                         Background NPC simulation (async)
                         ├─ Phase 1: each NPC declares intent
                         ├─ Phase 2: DM resolves interactions at shared locations
                         └─ Phase 3: final narrative is generated and logged
```

Omnis does not modify the actual chat message sent by SillyTavern. It only prepends a system message containing nearby actors, remote world state, and rule summaries. The upstream model sees these facts and naturally reflects the changing world in its reply.

## License

MIT License
