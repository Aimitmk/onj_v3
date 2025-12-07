"""
ゲーム用データモデル

役職、プレイヤー状態、ゲーム状態などを定義する。
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class Role(Enum):
    """
    役職を定義する列挙型。
    
    新しい役職を追加する場合は、ここに追加し、
    config.pyのROLE_CONFIGとROLE_DESCRIPTIONSも更新する。
    """
    VILLAGER = "村人"
    WEREWOLF = "人狼"
    ALPHA_WOLF = "大狼"  # おおおおかみ
    SEER = "占い師"
    THIEF = "怪盗"
    HUNTER = "狩人"
    TANNER = "吊り人"
    MADMAN = "狂人"
    MAYOR = "村長"
    
    def __str__(self) -> str:
        return self.value


class Team(Enum):
    """
    陣営を定義する列挙型。
    
    勝敗判定で使用する。
    """
    VILLAGE = "村人陣営"
    WEREWOLF = "人狼陣営"
    TANNER = "吊り人陣営"
    
    def __str__(self) -> str:
        return self.value


# 役職から陣営へのマッピング
ROLE_TO_TEAM: dict[Role, Team] = {
    Role.VILLAGER: Team.VILLAGE,
    Role.WEREWOLF: Team.WEREWOLF,
    Role.ALPHA_WOLF: Team.WEREWOLF,  # 大狼は人狼陣営
    Role.SEER: Team.VILLAGE,
    Role.THIEF: Team.VILLAGE,
    Role.HUNTER: Team.VILLAGE,  # 狩人は村人陣営
    Role.TANNER: Team.TANNER,
    Role.MADMAN: Team.WEREWOLF,  # 狂人は人狼陣営
    Role.MAYOR: Team.VILLAGE,   # 村長は村人陣営
}


def get_team(role: Role) -> Team:
    """役職から陣営を取得する。"""
    return ROLE_TO_TEAM[role]


class GamePhase(Enum):
    """
    ゲームのフェーズを定義する列挙型。
    """
    WAITING = auto()      # 募集中
    NIGHT = auto()        # 夜フェーズ
    DISCUSSION = auto()   # 昼・議論フェーズ
    VOTING = auto()       # 昼・投票フェーズ
    ENDED = auto()        # ゲーム終了


class NightActionType(Enum):
    """
    夜の行動タイプを定義する列挙型。
    """
    WEREWOLF_CHECK = auto()     # 人狼: 仲間を確認
    SEER_PLAYER = auto()        # 占い師: プレイヤーを占う
    SEER_CENTER = auto()        # 占い師: 中央カードを見る
    THIEF_SWAP = auto()         # 怪盗: カードを交換
    THIEF_SKIP = auto()         # 怪盗: 何もしない
    HUNTER_TARGET = auto()      # 狩人: 道連れ対象を指名
    HUNTER_SKIP = auto()        # 狩人: 道連れなし
    NO_ACTION = auto()          # 行動なし


@dataclass
class NightAction:
    """
    夜の行動を記録するデータクラス。
    """
    action_type: NightActionType
    target_player_id: Optional[int] = None  # 対象プレイヤーのDiscord ID
    result: Optional[str] = None            # 行動の結果（表示用テキスト）


@dataclass
class Player:
    """
    プレイヤー情報を管理するデータクラス。
    """
    user_id: int                            # Discord User ID
    username: str                           # 表示名
    initial_role: Role                      # 初期役職
    current_role: Role                      # 現在の役職（怪盗による交換後）
    night_action: Optional[NightAction] = None  # 夜の行動記録
    has_acted: bool = False                 # 夜の行動を完了したか
    vote_target_id: Optional[int] = None    # 投票先のUser ID
    is_llm: bool = False                    # LLMプレイヤーかどうか
    
    @property
    def team(self) -> Team:
        """現在の役職に基づく陣営を返す。"""
        return get_team(self.current_role)
    
    @property
    def initial_team(self) -> Team:
        """初期役職に基づく陣営を返す。"""
        return get_team(self.initial_role)


@dataclass
class GameState:
    """
    ゲーム全体の状態を管理するデータクラス。
    
    1チャンネルにつき1つのGameStateが存在する。
    """
    channel_id: int                         # Discord チャンネル ID
    host_id: int                            # ホスト（ゲーム開始者）のUser ID
    started_at: datetime = field(default_factory=datetime.now)
    phase: GamePhase = GamePhase.WAITING
    players: dict[int, Player] = field(default_factory=dict)  # user_id -> Player
    center_cards: list[Role] = field(default_factory=list)    # 中央カード（2枚）
    
    # カスタム役職構成（Noneの場合はデフォルトを使用）
    custom_role_config: Optional[list[Role]] = None
    
    # 夜フェーズの進行管理
    current_night_role: Optional[Role] = None  # 現在行動中の役職
    night_action_order: list[Role] = field(default_factory=list)  # 夜の行動順序
    night_action_index: int = 0               # 現在の行動順序インデックス
    
    # 投票結果
    executed_player_ids: list[int] = field(default_factory=list)  # 処刑されたプレイヤー
    
    # 勝敗結果
    winners: list[Team] = field(default_factory=list)
    
    @property
    def player_count(self) -> int:
        """参加プレイヤー数を返す。"""
        return len(self.players)
    
    @property
    def player_list(self) -> list[Player]:
        """プレイヤーのリストを返す。"""
        return list(self.players.values())
    
    def get_player(self, user_id: int) -> Optional[Player]:
        """User IDからプレイヤーを取得する。"""
        return self.players.get(user_id)
    
    def add_player(self, user_id: int, username: str, is_llm: bool = False) -> bool:
        """
        プレイヤーを追加する。
        
        Args:
            user_id: Discord User ID（LLMプレイヤーの場合は負の値）
            username: 表示名
            is_llm: LLMプレイヤーかどうか
        
        Returns:
            追加に成功した場合True、既に参加済みの場合False
        """
        if user_id in self.players:
            return False
        # 仮の役職を設定（後でsetup_gameで上書きされる）
        self.players[user_id] = Player(
            user_id=user_id,
            username=username,
            initial_role=Role.VILLAGER,
            current_role=Role.VILLAGER,
            is_llm=is_llm,
        )
        return True
    
    def remove_player(self, user_id: int) -> bool:
        """
        プレイヤーを削除する。
        
        Returns:
            削除に成功した場合True、存在しない場合False
        """
        if user_id not in self.players:
            return False
        del self.players[user_id]
        return True
    
    def get_players_by_role(self, role: Role, use_current: bool = True) -> list[Player]:
        """
        指定した役職のプレイヤーリストを返す。
        
        Args:
            role: 検索する役職
            use_current: Trueなら現在の役職、Falseなら初期役職で検索
        """
        if use_current:
            return [p for p in self.players.values() if p.current_role == role]
        return [p for p in self.players.values() if p.initial_role == role]
    
    def get_players_by_initial_role(self, role: Role) -> list[Player]:
        """初期役職で検索（夜フェーズ用）。"""
        return self.get_players_by_role(role, use_current=False)
    
    def all_voted(self) -> bool:
        """全員が投票済みかどうかを返す。"""
        return all(p.vote_target_id is not None for p in self.players.values())
    
    def voted_count(self) -> int:
        """投票済みの人数を返す。"""
        return sum(1 for p in self.players.values() if p.vote_target_id is not None)
    
    def reset(self) -> None:
        """ゲーム状態を完全にリセットする（次のゲームの準備）。"""
        self.phase = GamePhase.WAITING
        self.players.clear()
        self.center_cards.clear()
        self.custom_role_config = None  # カスタム役職構成もリセット
        self.current_night_role = None
        self.night_action_order.clear()
        self.night_action_index = 0
        self.executed_player_ids.clear()
        self.winners.clear()
        self.started_at = datetime.now()
    
    def get_llm_players(self) -> list[Player]:
        """LLMプレイヤーのリストを返す。"""
        return [p for p in self.players.values() if p.is_llm]
    
    def get_human_players(self) -> list[Player]:
        """人間プレイヤーのリストを返す。"""
        return [p for p in self.players.values() if not p.is_llm]
    
    @property
    def llm_player_count(self) -> int:
        """LLMプレイヤー数を返す。"""
        return sum(1 for p in self.players.values() if p.is_llm)

