# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ワンナイト人狼 (One Night Werewolf) Discord Bot - A Japanese party game bot for 3-8 players using discord.py.
LLMプレイヤー機能により、人数が足りない場合でもAIプレイヤーで補完してゲームを開始できます。

## Development Commands

```bash
# Setup
uv venv .venv
source .venv/bin/activate       # macOS/Linux
.venv\Scripts\activate.bat      # Windows

uv pip install discord.py python-dotenv httpx

# Run the bot
python bot.py
```

## Environment Variables

Copy `.env.example` to `.env` and set:
- `DISCORD_TOKEN` - Bot token from Discord Developer Portal
- `GUILD_ID` - (Optional) Server ID for instant command sync
- `XAI_API_KEY` - (Optional) xAI API key for LLM players
- `XAI_MODEL` - (Optional) Model name (default: `grok-4-1-fast-reasoning`)

**Discord Bot Requirements**: Enable these Privileged Gateway Intents in Developer Portal:
- SERVER MEMBERS INTENT
- MESSAGE CONTENT INTENT

## Architecture

```
bot.py            # Discord integration: slash commands, DMs, UI components, game state management
config.py         # Game constants, role configs, message templates
game/
  models.py       # Data structures: Role, Team, GamePhase, Player, GameState, NightAction
  logic.py        # Pure game logic with no Discord dependencies (testable)
  llm_player.py   # LLM player implementation using Grok API
  characters.json # LLM character definitions (name, emoji, personality, speech_style)
  rules.md        # Game rules documentation (used in LLM system prompts)
```

### Key Design Pattern

**Separation of concerns**: `game/logic.py` contains pure game logic functions with no Discord dependencies. All Discord integration (commands, messages, UI) lives in `bot.py`. This makes the game logic testable and reusable.

### State Management

- In-memory dictionary: `games: dict[int, GameState]` maps channel ID to active game
- One game per channel
- Ephemeral state (lost on bot restart)

### Key Data Model Fields (game/models.py)

**Player**:
- `initial_role` vs `current_role` - Critical distinction for Thief swap logic. Night actions use `initial_role`, win conditions use `current_role`
- `is_llm` - Boolean flag identifying AI players (use negative user_id to avoid Discord ID collisions)
- `has_acted` - Tracks night action completion

**GameState**:
- `discussion_history: list[tuple[str, str]]` - (speaker_name, message) pairs for LLM context
- `executed_player_ids` - Includes both vote-executed and hunter-dragged players

### Key Constants (config.py)

- `MIN_PLAYERS=3`, `MAX_PLAYERS=8`
- `DISCUSSION_TIME=180` (seconds) - 議論フェーズのみタイムアウトあり
- Role card constraint: `total_cards == player_count + 2` (always 2 center cards)

### Game Flow

1. WAITING → Player recruitment via `/onj start`, `/onj join`
2. NIGHT → Role actions via DM commands (`!seer`, `!thief`, `!hunter`)
3. DISCUSSION → Timed discussion phase (180 seconds)
4. VOTING → `/onj vote @player` or `/onj skip`
5. ENDED → Results announcement, rematch option

### Night Action Order

Defined in `game/logic.py`:
```python
NIGHT_ACTION_ORDER = [Role.WEREWOLF, Role.SEER, Role.THIEF]
```
Note: Hunter designates a revenge target at night via DM, but the revenge execution happens during voting phase only if Hunter is killed.

### Key Logic Functions (game/logic.py)

**Game Setup**:
- `setup_game(state, role_list)` - Shuffle and distribute roles, initialize night phase

**Night Actions**:
- `process_werewolf_night(state)` - Wolves see each other; Alpha Wolf also sees center cards
- `process_seer_action_player(state, seer_id, target_id)` - See player's current role
- `process_seer_action_center(state, seer_id)` - See both center cards
- `process_thief_action(state, thief_id, target_id)` - Swap cards (updates both players' `current_role`)
- `process_hunter_action(state, hunter_id, target_id)` - Record revenge target

**Voting & Execution**:
- `calculate_votes(state)` - Mayor has 2 votes; returns `{user_id: count}`, `-1` = peace village
- `determine_execution(state)` - Highest votes executed; ties = both executed
- `add_hunter_target_to_execution(state, target_id)` - Add revenge kill to execution list

**Win Conditions** (`determine_winner`):
1. Tanner executed → **Tanner wins** (exclusive, highest priority)
2. Wolf executed → **Village wins**
3. Wolf not executed → **Wolf wins**
4. Peace village (no wolves in game):
   - No execution → Everyone wins
   - Someone executed → Executed player "wins" (special case)
   - Madman becomes Village team if no wolves exist

## Discord Commands

**Slash commands** (`/onj` command group):
- `start`, `join`, `leave`, `players` - Lobby management
- `roles` - Customize role composition (host only)
- `add_bot [count]`, `remove_bot [count]` - Add/remove AI players (host only)
- `begin` - Start game (host only)
- `vote` - Voting phase (includes peace village option)
- `cancel` - Cancel game (host only)

**DM commands** (night phase):
- `!seer player <name>` or `!seer center`
- `!thief <name>` or `!thief skip`
- `!hunter <name>` or `!hunter skip`

## Game Rules Reference

- 9 roles: 村人, 人狼, 大狼, 占い師, 怪盗, 狩人, 吊り人, 狂人, 村長
- Always 2 center cards
- 村長 (Mayor) has 2 votes
- 狩人 (Hunter) revenge kills on execution
- 怪盗 (Thief) swaps cards and takes new role
- Win conditions in `game/logic.py:determine_winner()`
- Full rules documentation: `game/rules.md`

## LLM Player Feature

AIプレイヤーは xAI Grok API を使用:
- `XAI_API_KEY` 環境変数が必要
- `XAI_MODEL` でモデル指定可能（デフォルト: grok-4-1-fast-reasoning）
- API rate limit: 1 call/second (`API_CALL_INTERVAL` in llm_player.py)

**Character System** (`game/characters.json`):
- 7 unique personalities: アリス, ボブ, チャーリー, ダイアナ, エミリー, フランク, グレース
- Each character has: `name`, `emoji`, `personality`, `speech_style`
- Characters are not reused within a single game
- Edit `characters.json` to add/modify characters (all 4 fields required)

**Key Functions** (game/llm_player.py):
- `get_perceived_role(player)` - Returns what LLM believes its role is (differs after being Thief-swapped)
- `llm_seer_action()`, `llm_thief_action()`, `llm_hunter_action()` - Night action decisions
- `llm_generate_discussion_message()` - In-character discussion statements
- `llm_vote()` - Voting decision based on role and gathered information

**Discussion Phase Behavior**:
- Initial statement when discussion starts
- Auto-generated follow-up statements in rotation
- Reaction statements when specifically mentioned by name
- Context: Last 15 discussion messages + own last 3 statements
