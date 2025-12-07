# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ワンナイト人狼 (One Night Werewolf) Discord Bot - A Japanese party game bot for 3-8 players using discord.py.
LLMプレイヤー機能により、人数が足りない場合でもAIプレイヤーで補完してゲームを開始できます。

## Development Commands

```bash
# Setup
uv venv .venv
source .venv/bin/activate  # macOS/Linux
uv pip install discord.py python-dotenv httpx

# Run the bot
python bot.py
```

## Environment Variables

Copy `.env.example` to `.env` and set:
- `DISCORD_TOKEN` - Bot token from Discord Developer Portal
- `GUILD_ID` - (Optional) Server ID for instant command sync
- `XAI_API_KEY` - (Optional) xAI API key for LLM players (Grok 4.1 Fast)

## Architecture

```
bot.py          # Discord integration: slash commands, DMs, UI components, game state management
config.py       # Game constants, role configs, message templates
game/
  models.py     # Data structures: Role, Team, GamePhase, Player, GameState, NightAction
  logic.py      # Pure game logic with no Discord dependencies (testable)
  llm_player.py # LLM player implementation using Grok 4.1 Fast API
```

### Key Design Pattern

**Separation of concerns**: `game/logic.py` contains pure game logic functions with no Discord dependencies. All Discord integration (commands, messages, UI) lives in `bot.py`. This makes the game logic testable and reusable.

### State Management

- In-memory dictionary: `games: dict[int, GameState]` maps channel ID to active game
- One game per channel
- Ephemeral state (lost on bot restart)

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

## Discord Commands

**Slash commands** (`/onj` command group):
- `start`, `join`, `leave`, `players` - Lobby management
- `roles` - Customize role composition (host only)
- `add_bot [count]`, `remove_bot [count]` - Add/remove AI players (host only)
- `begin` - Start game (host only)
- `vote @player`, `skip` - Voting phase
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

AIプレイヤーは xAI Grok 4.1 Fast Reasoning を使用:
- `XAI_API_KEY` 環境変数が必要
- `XAI_MODEL` でモデル指定可能（デフォルト: grok-4-1-fast-reasoning）
  - 利用可能: grok-4-1-fast-reasoning, grok-4-1-fast-non-reasoning
- LLMプレイヤーは `is_llm=True` フラグで識別
- 負のUser IDを使用（Discord IDと衝突しない）
- 夜フェーズ: 役職に応じた行動を自動実行
- 投票フェーズ: 役職と得た情報に基づいて投票
- 議論フェーズ: 人間の発言に反応して順番に発言
