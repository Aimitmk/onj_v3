"""
ゲームロジック

役職ごとの夜アクション処理、勝敗判定などを実装する。
Discord依存のコードは含めず、純粋なゲームロジックのみを記述する。
"""

import random
from typing import Optional
from game.models import (
    Role,
    Team,
    GamePhase,
    GameState,
    Player,
    NightAction,
    NightActionType,
    get_team,
)


# =============================================================================
# 夜の行動順序
# =============================================================================
# ワンナイト人狼の標準的な行動順序
# 人狼 → 占い師 → 怪盗 の順（狩人は処刑時に道連れを選ぶ）
NIGHT_ACTION_ORDER: list[Role] = [
    Role.WEREWOLF,
    Role.SEER,
    Role.THIEF,
]


def setup_game(state: GameState, role_list: list[Role]) -> None:
    """
    ゲームを初期化し、役職を配布する。
    
    Args:
        state: ゲーム状態
        role_list: 使用する役職のリスト（プレイヤー数 + 中央カード2枚分）
    
    Note:
        role_listはプレイヤー数 + 2（中央カード）の長さである必要がある。
    """
    player_count = state.player_count
    expected_cards = player_count + 2  # 中央カードは常に2枚
    
    if len(role_list) != expected_cards:
        raise ValueError(
            f"役職カード数が不正です。期待: {expected_cards}, 実際: {len(role_list)}"
        )
    
    # 役職をシャッフル
    shuffled_roles = role_list.copy()
    random.shuffle(shuffled_roles)
    
    # プレイヤーに役職を配布
    player_ids = list(state.players.keys())
    for i, user_id in enumerate(player_ids):
        role = shuffled_roles[i]
        state.players[user_id].initial_role = role
        state.players[user_id].current_role = role
    
    # 残りを中央カードに
    state.center_cards = shuffled_roles[player_count:]
    
    # 夜の行動順序を設定
    state.night_action_order = NIGHT_ACTION_ORDER.copy()
    state.night_action_index = 0
    
    # フェーズを夜に
    state.phase = GamePhase.NIGHT


def get_current_night_role(state: GameState) -> Optional[Role]:
    """
    現在行動すべき役職を取得する。
    
    Returns:
        現在の役職。全ての行動が終了していればNone。
    """
    if state.night_action_index >= len(state.night_action_order):
        return None
    return state.night_action_order[state.night_action_index]


def advance_night_phase(state: GameState) -> Optional[Role]:
    """
    夜フェーズを次の役職に進める。
    
    Returns:
        次の役職。全ての行動が終了していればNone。
    """
    state.night_action_index += 1
    return get_current_night_role(state)


def is_night_phase_complete(state: GameState) -> bool:
    """夜フェーズが完了したかどうかを返す。"""
    return state.night_action_index >= len(state.night_action_order)


# =============================================================================
# 人狼の夜行動
# =============================================================================

def get_all_wolves(state: GameState) -> list[Player]:
    """人狼陣営の狼（人狼・大狼）をすべて取得する。"""
    werewolves = state.get_players_by_initial_role(Role.WEREWOLF)
    alpha_wolves = state.get_players_by_initial_role(Role.ALPHA_WOLF)
    return werewolves + alpha_wolves


def process_werewolf_night(state: GameState) -> dict[int, list[Player]]:
    """
    人狼・大狼の夜行動を処理する。
    
    人狼と大狼はお互いを確認できる。
    大狼はさらに中央カードも確認できる。
    
    Returns:
        人狼/大狼のuser_idをキー、他の人狼/大狼プレイヤーのリストを値とする辞書
    """
    # 初期役職が人狼または大狼のプレイヤーを取得
    all_wolves = get_all_wolves(state)
    
    result: dict[int, list[Player]] = {}
    
    for wolf in all_wolves:
        # 自分以外の人狼/大狼
        other_wolves = [w for w in all_wolves if w.user_id != wolf.user_id]
        result[wolf.user_id] = other_wolves
        
        # 行動を記録
        if other_wolves:
            result_text = f"他の人狼: {', '.join(w.username for w in other_wolves)}"
        else:
            result_text = "あなたは唯一の人狼です"
        
        # 大狼は中央カードも確認
        if wolf.initial_role == Role.ALPHA_WOLF:
            center_cards = state.center_cards
            center_text = ", ".join(r.value for r in center_cards)
            result_text += f"\n中央カード: {center_text}"
        
        wolf.night_action = NightAction(
            action_type=NightActionType.WEREWOLF_CHECK,
            result=result_text
        )
        wolf.has_acted = True
    
    return result


# =============================================================================
# 占い師の夜行動
# =============================================================================

def process_seer_action_player(
    state: GameState,
    seer_id: int,
    target_id: int
) -> Optional[Role]:
    """
    占い師が他プレイヤーの役職を見る。
    
    Args:
        state: ゲーム状態
        seer_id: 占い師のUser ID
        target_id: 対象プレイヤーのUser ID
    
    Returns:
        対象の現在の役職。無効な対象の場合はNone。
    """
    seer = state.get_player(seer_id)
    target = state.get_player(target_id)
    
    if seer is None or target is None:
        return None
    
    if seer.initial_role != Role.SEER:
        return None
    
    if seer_id == target_id:
        return None  # 自分自身は占えない
    
    # 行動を記録
    seer.night_action = NightAction(
        action_type=NightActionType.SEER_PLAYER,
        target_player_id=target_id,
        result=f"{target.username} の役職は {target.current_role.value} です"
    )
    seer.has_acted = True
    
    return target.current_role


def process_seer_action_center(
    state: GameState,
    seer_id: int
) -> Optional[list[Role]]:
    """
    占い師が中央カード2枚を見る。
    
    Args:
        state: ゲーム状態
        seer_id: 占い師のUser ID
    
    Returns:
        中央カード2枚の役職リスト。無効な場合はNone。
    """
    seer = state.get_player(seer_id)
    
    if seer is None:
        return None
    
    if seer.initial_role != Role.SEER:
        return None
    
    # 行動を記録
    center_roles = state.center_cards.copy()
    seer.night_action = NightAction(
        action_type=NightActionType.SEER_CENTER,
        result=f"中央カード: {center_roles[0].value}, {center_roles[1].value}"
    )
    seer.has_acted = True
    
    return center_roles


def process_seer_action(
    state: GameState,
    seer_id: int,
    target_player_id: Optional[int] = None,
    view_center: bool = False
) -> Optional[str]:
    """
    占い師の行動を統合的に処理する。
    
    Args:
        state: ゲーム状態
        seer_id: 占い師のUser ID
        target_player_id: 対象プレイヤーのUser ID（プレイヤーを占う場合）
        view_center: 中央カードを見る場合True
    
    Returns:
        結果メッセージ。無効な場合はNone。
    """
    if view_center:
        roles = process_seer_action_center(state, seer_id)
        if roles:
            return f"🔮 中央カードは **{roles[0].value}** と **{roles[1].value}** です"
        return None
    elif target_player_id is not None:
        role = process_seer_action_player(state, seer_id, target_player_id)
        if role:
            target = state.get_player(target_player_id)
            if target:
                return f"🔮 {target.username} の役職は **{role.value}** です"
        return None
    return None


# =============================================================================
# 怪盗の夜行動
# =============================================================================

def process_thief_action(
    state: GameState,
    thief_id: int,
    target_id: Optional[int] = None
) -> Optional[Role]:
    """
    怪盗が他プレイヤーとカードを交換する。
    
    Args:
        state: ゲーム状態
        thief_id: 怪盗のUser ID
        target_id: 対象プレイヤーのUser ID。Noneの場合はスキップ。
    
    Returns:
        交換後の怪盗の新しい役職。スキップまたは無効な場合はNone。
    """
    thief = state.get_player(thief_id)
    
    if thief is None:
        return None
    
    if thief.initial_role != Role.THIEF:
        return None
    
    # スキップの場合
    if target_id is None:
        thief.night_action = NightAction(
            action_type=NightActionType.THIEF_SKIP,
            result="何もしませんでした"
        )
        thief.has_acted = True
        return None
    
    target = state.get_player(target_id)
    
    if target is None:
        return None
    
    if thief_id == target_id:
        return None  # 自分自身とは交換できない
    
    # カードを交換
    old_thief_role = thief.current_role
    new_thief_role = target.current_role
    
    thief.current_role = new_thief_role
    target.current_role = old_thief_role
    
    # 行動を記録
    thief.night_action = NightAction(
        action_type=NightActionType.THIEF_SWAP,
        target_player_id=target_id,
        result=f"{target.username} とカードを交換しました。新しい役職: {new_thief_role.value}"
    )
    thief.has_acted = True
    
    return new_thief_role


# =============================================================================
# 狩人の夜行動
# =============================================================================

def process_hunter_action(
    state: GameState,
    hunter_id: int,
    target_id: Optional[int] = None
) -> bool:
    """
    狩人が道連れ対象を指名する。
    
    Args:
        state: ゲーム状態
        hunter_id: 狩人のUser ID
        target_id: 道連れ対象のUser ID。Noneの場合はスキップ。
    
    Returns:
        行動が成功した場合True
    """
    hunter = state.get_player(hunter_id)
    
    if hunter is None:
        return False
    
    if hunter.initial_role != Role.HUNTER:
        return False
    
    # スキップの場合
    if target_id is None:
        hunter.night_action = NightAction(
            action_type=NightActionType.HUNTER_SKIP,
            result="道連れ対象を指名しませんでした"
        )
        hunter.has_acted = True
        return True
    
    target = state.get_player(target_id)
    
    if target is None:
        return False
    
    if hunter_id == target_id:
        return False  # 自分自身は指名できない
    
    # 道連れ対象を記録
    hunter.night_action = NightAction(
        action_type=NightActionType.HUNTER_TARGET,
        target_player_id=target_id,
        result=f"{target.username} を道連れに指名しました"
    )
    hunter.has_acted = True
    
    return True


def get_hunter_target(state: GameState, hunter_id: int) -> Optional[int]:
    """
    狩人の道連れ対象を取得する。
    
    Args:
        state: ゲーム状態
        hunter_id: 狩人のUser ID
    
    Returns:
        道連れ対象のUser ID。指名していない場合はNone。
    """
    hunter = state.get_player(hunter_id)
    if hunter is None:
        return None
    
    if hunter.night_action is None:
        return None
    
    if hunter.night_action.action_type != NightActionType.HUNTER_TARGET:
        return None
    
    return hunter.night_action.target_player_id


# =============================================================================
# 投票処理
# =============================================================================

def register_vote(state: GameState, voter_id: int, target_id: int) -> bool:
    """
    投票を登録する。
    
    Args:
        state: ゲーム状態
        voter_id: 投票者のUser ID
        target_id: 投票先のUser ID
    
    Returns:
        投票が有効な場合True
    """
    voter = state.get_player(voter_id)
    target = state.get_player(target_id)
    
    if voter is None or target is None:
        return False
    
    if voter_id == target_id:
        return False  # 自分自身には投票できない
    
    if voter.vote_target_id is not None:
        return False  # 既に投票済み
    
    voter.vote_target_id = target_id
    return True


def calculate_votes(state: GameState) -> dict[int, int]:
    """
    投票を集計する。
    
    村長（MAYOR）の票は2票としてカウントする。
    
    Returns:
        user_idをキー、得票数を値とする辞書
        -1 は「平和村」（誰も処刑しない）への投票を表す
    """
    vote_counts: dict[int, int] = {p.user_id: 0 for p in state.players.values()}
    vote_counts[-1] = 0  # 平和村への投票
    
    for player in state.players.values():
        if player.vote_target_id is not None:
            # 村長は2票、それ以外は1票
            vote_power = 2 if player.current_role == Role.MAYOR else 1
            
            if player.vote_target_id in vote_counts:
                vote_counts[player.vote_target_id] += vote_power
            elif player.vote_target_id == -1:
                vote_counts[-1] += vote_power
    
    return vote_counts


def determine_execution(state: GameState) -> list[int]:
    """
    処刑対象を決定する（狩人の道連れは含まない）。

    最多得票者を処刑する。同票の場合は全員処刑（両吊り）。
    平和村（-1）が最多得票に含まれる場合は、平和村を除いた同票者を処刑。

    ※ 狩人の道連れは別途 add_hunter_target_to_execution() で追加する

    Returns:
        処刑されるプレイヤーのUser IDリスト（0人以上）
    """
    vote_counts = calculate_votes(state)

    if not vote_counts:
        return []

    max_votes = max(vote_counts.values())

    if max_votes == 0:
        return []

    # 最多得票者を取得
    max_voted = [uid for uid, count in vote_counts.items() if count == max_votes]

    # 平和村（-1）を除外
    max_voted_players = [uid for uid in max_voted if uid != -1]

    # 平和村のみが最多得票の場合は誰も処刑しない
    if not max_voted_players:
        state.executed_player_ids = []
        return []

    # 同票でも全員処刑（両吊り）
    executed = list(max_voted_players)

    state.executed_player_ids = executed
    return executed


def get_executed_hunters(state: GameState) -> list[Player]:
    """
    処刑対象に含まれる狩人（現在の役職が狩人）を取得する。

    Returns:
        処刑対象の狩人のリスト
    """
    hunters = []
    for uid in state.executed_player_ids:
        player = state.get_player(uid)
        if player and player.current_role == Role.HUNTER:
            hunters.append(player)
    return hunters


def add_hunter_target_to_execution(state: GameState, target_id: int) -> bool:
    """
    狩人の道連れ対象を処刑リストに追加する。

    Args:
        state: ゲーム状態
        target_id: 道連れ対象のUser ID

    Returns:
        追加に成功した場合True
    """
    if target_id in state.executed_player_ids:
        return False  # 既に処刑リストに含まれている

    target = state.get_player(target_id)
    if target is None:
        return False

    state.executed_player_ids.append(target_id)
    return True


# =============================================================================
# 勝敗判定
# =============================================================================

def is_wolf_role(role: Role) -> bool:
    """人狼系の役職かどうかを判定する。"""
    return role in (Role.WEREWOLF, Role.ALPHA_WOLF)


def has_wolves_in_game(state: GameState) -> bool:
    """場に人狼/大狼がいるかどうかを判定する。"""
    werewolves_in_game = state.get_players_by_role(Role.WEREWOLF, use_current=True)
    alpha_wolves_in_game = state.get_players_by_role(Role.ALPHA_WOLF, use_current=True)
    return bool(werewolves_in_game or alpha_wolves_in_game)


def determine_winner(state: GameState) -> list[Team]:
    """
    勝者を決定する。

    勝敗判定ルール:
    1. 吊り人が処刑された場合 → 吊り人のみ勝利
    2. 人狼/大狼が1人以上処刑された場合 → 村人陣営勝利
    3. それ以外（人狼/大狼が処刑されなかった場合）→ 人狼陣営勝利

    特殊ケース（平和村: 場に人狼/大狼がいない）:
    - 誰も処刑されなかった場合 → 全員勝利
    - 誰かが処刑された場合 → 処刑された人の勝利

    通常ケース（人狼がいる場合）:
    - 誰も処刑されなかった場合 → 人狼陣営勝利

    Returns:
        勝者の陣営リスト
    """
    executed_ids = state.executed_player_ids

    # 処刑されたプレイヤーの情報を取得
    executed_players = [state.get_player(uid) for uid in executed_ids]
    executed_players = [p for p in executed_players if p is not None]

    # 処刑されたプレイヤーの最終役職を取得
    executed_roles = [p.current_role for p in executed_players]

    # 1. 吊り人が処刑された場合 → 吊り人のみ勝利
    if Role.TANNER in executed_roles:
        state.winners = [Team.TANNER]
        return [Team.TANNER]

    # 場に人狼がいるかチェック
    wolves_exist = has_wolves_in_game(state)

    # 誰も処刑されなかった場合の特殊処理
    if not executed_ids:
        if wolves_exist:
            # 人狼がいるのに誰も処刑されなかった → 人狼勝利
            state.winners = [Team.WEREWOLF]
            return [Team.WEREWOLF]
        else:
            # 平和村で誰も処刑されない → 全員勝利
            state.winners = [Team.VILLAGE, Team.WEREWOLF, Team.TANNER]
            return [Team.VILLAGE, Team.WEREWOLF, Team.TANNER]

    # 2. 人狼/大狼が処刑された場合 → 村人陣営勝利
    if any(is_wolf_role(role) for role in executed_roles):
        state.winners = [Team.VILLAGE]
        return [Team.VILLAGE]

    # 3. 人狼/大狼が処刑されなかった場合
    if wolves_exist:
        # 人狼がいる → 人狼陣営勝利
        state.winners = [Team.WEREWOLF]
        return [Team.WEREWOLF]
    else:
        # 平和村で誰かが処刑された → 処刑された人の勝利
        state.winners = [Team.TANNER]  # 特殊勝利として吊り人陣営を使用
        return [Team.TANNER]


def get_winner_message(state: GameState) -> str:
    """勝者メッセージを生成する。"""
    winners = state.winners

    if not winners:
        return "勝者なし"

    # 平和村で全員勝利（全陣営が勝者）
    if len(winners) == 3 and Team.VILLAGE in winners and Team.WEREWOLF in winners and Team.TANNER in winners:
        return "🎉 **全員の勝利！** 人狼がいない平和村で誰も処刑されませんでした！"

    # 平和村で処刑者勝利（吊り人陣営のみだが、吊り人が処刑されていない）
    if winners == [Team.TANNER]:
        wolves_exist = has_wolves_in_game(state)
        if not wolves_exist:
            # 平和村で誰かが処刑された → 処刑された人の勝利
            executed_players = [state.get_player(uid) for uid in state.executed_player_ids]
            executed_players = [p for p in executed_players if p is not None]
            if executed_players:
                names = "、".join(p.username for p in executed_players)
                return f"🎯 **{names} の勝利！** 人狼がいない平和村で処刑されました！"
        # 通常の吊り人勝利（吊り人が処刑された場合）
        tanner_players = [
            p for p in state.players.values()
            if p.current_role == Role.TANNER and p.user_id in state.executed_player_ids
        ]
        if tanner_players:
            return f"🎭 **吊り人（{tanner_players[0].username}）の単独勝利！**"
        return "🎭 **吊り人陣営の勝利！**"

    if Team.VILLAGE in winners:
        # 人狼がいるかチェック（平和村判定）
        wolves_exist = has_wolves_in_game(state)
        if not wolves_exist:
            # 平和村の場合、狂人も勝者に含める
            madmen = state.get_players_by_role(Role.MADMAN, use_current=True)
            if madmen:
                return "🏘️ **村人陣営の勝利！** 人狼がいない平和村でした！\n🤪 狂人も村人陣営として勝利！"
            return "🏘️ **村人陣営の勝利！** 人狼がいない平和村でした！"
        return "🏘️ **村人陣営の勝利！** 人狼を処刑しました！"

    if Team.WEREWOLF in winners:
        return "🐺 **人狼陣営の勝利！** 人狼は処刑を免れました！"

    return "結果不明"


def get_final_roles_message(state: GameState) -> str:
    """最終役職一覧のメッセージを生成する。"""
    lines: list[str] = []
    
    # プレイヤーの役職
    lines.append("**【プレイヤー】**")
    for player in state.players.values():
        initial = player.initial_role.value
        current = player.current_role.value
        
        if initial != current:
            lines.append(f"• {player.username}: {initial} → **{current}**")
        else:
            lines.append(f"• {player.username}: **{current}**")
    
    # 中央カード
    lines.append("")
    lines.append("**【中央カード】**")
    for i, role in enumerate(state.center_cards, 1):
        lines.append(f"• カード{i}: **{role.value}**")
    
    return "\n".join(lines)


def get_execution_message(state: GameState) -> str:
    """処刑結果のメッセージを生成する。"""
    executed_ids = state.executed_player_ids
    
    if not executed_ids:
        # 平和村が選ばれたかを判定
        vote_counts = calculate_votes(state)
        max_votes = max(vote_counts.values()) if vote_counts else 0
        max_voted = [uid for uid, count in vote_counts.items() if count == max_votes]
        
        if -1 in max_voted:
            return "🕊️ **平和村が選ばれました！** 誰も処刑されませんでした。"
        return "⚖️ **誰も処刑されませんでした。**"
    
    executed_players = [state.get_player(uid) for uid in executed_ids]
    executed_players = [p for p in executed_players if p is not None]
    
    if not executed_players:
        return "処刑結果を取得できませんでした。"
    
    # 狩人の道連れがあるかチェック
    vote_counts = calculate_votes(state)
    max_votes = max(vote_counts.values()) if vote_counts else 0
    max_voted = [uid for uid, count in vote_counts.items() if count == max_votes and uid != -1]
    
    # 投票で処刑された人と道連れの人を分ける
    voted_executed = [p for p in executed_players if p.user_id in max_voted]
    dragged_executed = [p for p in executed_players if p.user_id not in max_voted]
    
    result_lines = []
    
    # 投票による処刑
    if voted_executed:
        names = ", ".join(p.username for p in voted_executed)
        roles = ", ".join(p.current_role.value for p in voted_executed)
        
        if len(voted_executed) > 1:
            result_lines.append(f"⚔️ **両吊り！** {names} が処刑されました。\n役職: **{roles}**")
        else:
            result_lines.append(f"⚖️ **{names}** が処刑されました。\n役職: **{roles}**")
    
    # 狩人の道連れ
    if dragged_executed:
        names = ", ".join(p.username for p in dragged_executed)
        roles = ", ".join(p.current_role.value for p in dragged_executed)
        result_lines.append(f"🏹 **道連れ！** {names} も処刑されました。\n役職: **{roles}**")
    
    return "\n\n".join(result_lines)

