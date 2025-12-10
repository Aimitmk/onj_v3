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
bot.py          # Discord integration: slash commands, DMs, UI components, game state management
config.py       # Game constants, role configs, message templates
game/
  models.py     # Data structures: Role, Team, GamePhase, Player, GameState, NightAction
  logic.py      # Pure game logic with no Discord dependencies (testable)
  llm_player.py # LLM player implementation using Grok API
```

### Key Design Pattern

**Separation of concerns**: `game/logic.py` contains pure game logic functions with no Discord dependencies. All Discord integration (commands, messages, UI) lives in `bot.py`. This makes the game logic testable and reusable.

### State Management

- In-memory dictionary: `games: dict[int, GameState]` maps channel ID to active game
- One game per channel
- Ephemeral state (lost on bot restart)

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
NIGHT_ACTION_ORDER = [Role.WEREWOLF, Role.SEER, Role.THIEF, Role.HUNTER]
```
Note: Hunter designates a revenge target at night, but execution happens during voting phase if Hunter is killed.

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

## LLM Player Feature

AIプレイヤーは xAI Grok API を使用:
- `XAI_API_KEY` 環境変数が必要
- `XAI_MODEL` でモデル指定可能（デフォルト: grok-4-1-fast-reasoning）
- API rate limit: 1 call/second (`API_CALL_INTERVAL` in llm_player.py)

**Implementation details**:
- LLMプレイヤーは `is_llm=True` フラグで識別
- 負のUser IDを使用（Discord IDと衝突しない）
- 7 character personalities (Alice, Bob, Charlie, Diana, Emily, Frank, Grace)
- `get_perceived_role()` returns what LLM believes its role is (differs after Thief swap)
- Discussion history: `GameState.discussion_history: list[tuple[str, str]]`

**Phases**:
- 夜フェーズ: 役職に応じた行動を自動実行
- 議論フェーズ: 初回発言 + 自動発言ループ + 名指し時に反応発言
- 投票フェーズ: 役職と得た情報に基づいて投票
