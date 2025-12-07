"""
ワンナイト人狼 ゲームロジックパッケージ
"""

from game.models import Role, Team, GamePhase, Player, GameState
from game.logic import (
    setup_game,
    process_werewolf_night,
    process_seer_action,
    process_thief_action,
    calculate_votes,
    determine_winner,
)
from game.llm_player import (
    generate_llm_player_name,
    llm_seer_action,
    llm_thief_action,
    llm_hunter_action,
    llm_vote,
    get_xai_api_key,
)

__all__ = [
    # Models
    "Role",
    "Team",
    "GamePhase",
    "Player",
    "GameState",
    # Logic
    "setup_game",
    "process_werewolf_night",
    "process_seer_action",
    "process_thief_action",
    "calculate_votes",
    "determine_winner",
    # LLM Player
    "generate_llm_player_name",
    "llm_seer_action",
    "llm_thief_action",
    "llm_hunter_action",
    "llm_vote",
    "get_xai_api_key",
]

