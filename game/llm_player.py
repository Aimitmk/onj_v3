"""
LLMプレイヤー

Grok 4.1 Fast APIを使用してAIプレイヤーを実装する。
人数が足りない場合にLLMプレイヤーで補完できる。
"""

import os
import random
import asyncio
from typing import Optional
import httpx
from game.models import Role, GameState, Player


# =============================================================================
# 設定
# =============================================================================

# xAI API設定
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# モデル名は環境変数で上書き可能
# 利用可能: grok-4-1-fast-reasoning, grok-4-1-fast-non-reasoning
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning")

# LLMプレイヤーの名前プリセット
LLM_PLAYER_NAMES = [
    "AIアリス", "AIボブ", "AIチャーリー", "AIダイアナ",
    "AIエミリー", "AIフランク", "AIグレース", "AIヘンリー",
    "AI太郎", "AI花子", "AI次郎", "AI美咲",
]


def get_xai_api_key() -> Optional[str]:
    """環境変数からxAI APIキーを取得する。"""
    return os.getenv("XAI_API_KEY")


def generate_llm_player_name(existing_names: set[str]) -> str:
    """重複しないLLMプレイヤー名を生成する。"""
    available = [name for name in LLM_PLAYER_NAMES if name not in existing_names]
    if available:
        return random.choice(available)
    # 全て使用済みの場合は番号付きで生成
    i = 1
    while f"AIプレイヤー{i}" in existing_names:
        i += 1
    return f"AIプレイヤー{i}"


# =============================================================================
# プロンプトテンプレート
# =============================================================================

SYSTEM_PROMPT = """あなたは「ワンナイト人狼」というゲームのプレイヤーです。

# ゲームルール
- 3〜8人でプレイする推理ゲームです
- 各プレイヤーに役職が配られ、中央に2枚のカードが伏せられます
- 夜フェーズで各役職が能力を使い、昼フェーズで議論し、投票で処刑者を決めます
- 人狼を処刑すれば村人陣営の勝ち、人狼が生き残れば人狼陣営の勝ちです

# あなたの役職と陣営
あなたの役職: {role}
あなたの陣営: {team}

# 役職説明
{role_description}

# あなたの目標
{goal}

# 重要
- 短く自然な日本語で回答してください
- 嘘をついても構いません（特に人狼陣営の場合）
- ゲームを楽しんでください
"""

# 陣営ごとの目標
TEAM_GOALS = {
    "村人陣営": "人狼を見つけ出し、投票で処刑することで村人陣営を勝利に導きましょう。",
    "人狼陣営": "自分が人狼であることを隠し、村人を欺いて生き残りましょう。狂人も人狼陣営です。",
    "吊り人陣営": "自分が処刑されることを目指しましょう。怪しい行動を取りつつ、吊られるように誘導してください。",
}


def get_role_team_name(role: Role) -> str:
    """役職から陣営名を取得する。"""
    from game.models import get_team, Team
    team = get_team(role)
    if team == Team.VILLAGE:
        return "村人陣営"
    elif team == Team.WEREWOLF:
        return "人狼陣営"
    elif team == Team.TANNER:
        return "吊り人陣営"
    return "不明"


def get_role_description(role: Role) -> str:
    """役職の説明を取得する。"""
    from config import ROLE_DESCRIPTIONS
    return ROLE_DESCRIPTIONS.get(role, "特別な能力はありません。")


def build_system_prompt(role: Role) -> str:
    """役職に応じたシステムプロンプトを構築する。"""
    team_name = get_role_team_name(role)
    return SYSTEM_PROMPT.format(
        role=role.value,
        team=team_name,
        role_description=get_role_description(role),
        goal=TEAM_GOALS.get(team_name, "ゲームを楽しみましょう。"),
    )


# =============================================================================
# LLM API呼び出し
# =============================================================================

async def call_grok_api(
    messages: list[dict[str, str]],
    temperature: float = 0.8,
    max_tokens: int = 256,
) -> Optional[str]:
    """
    Grok APIを呼び出してレスポンスを取得する。
    
    Args:
        messages: チャットメッセージのリスト
        temperature: 生成の温度パラメータ
        max_tokens: 最大トークン数
    
    Returns:
        生成されたテキスト。エラー時はNone。
    """
    api_key = get_xai_api_key()
    if not api_key:
        print("Warning: XAI_API_KEY is not set")
        return None
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": XAI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(XAI_API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            print(f"Grok API 403 Forbidden: APIキーが無効か、モデル '{XAI_MODEL}' にアクセス権がありません")
            print("環境変数 XAI_MODEL でモデルを変更できます")
        elif e.response.status_code == 401:
            print("Grok API 401 Unauthorized: APIキーが設定されていないか無効です")
        else:
            print(f"Grok API HTTP error: {e}")
        return None
    except httpx.RequestError as e:
        print(f"Grok API request error: {e}")
        return None
    except (KeyError, IndexError) as e:
        print(f"Grok API response parse error: {e}")
        return None


# =============================================================================
# ゲームアクション
# =============================================================================

async def llm_seer_action(
    game: GameState,
    player: Player,
    other_players: list[Player],
) -> tuple[str, Optional[int]]:
    """
    占い師のLLMプレイヤーが行動を決定する。
    
    Returns:
        (action_type, target_id): "center" or "player"とターゲットID
    """
    system_prompt = build_system_prompt(player.initial_role)
    
    player_names = ", ".join(p.username for p in other_players)
    user_prompt = f"""あなたは占い師です。夜フェーズで行動を選んでください。

選択肢:
1. プレイヤーを1人選んで、その人の役職を見る
2. 中央カード2枚を見る

他のプレイヤー: {player_names}

以下の形式で回答してください:
- プレイヤーを見る場合: "占う: [プレイヤー名]"
- 中央カードを見る場合: "占う: 中央"

どちらか1つだけ選んでください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    response = await call_grok_api(messages)
    
    if response:
        response_lower = response.lower()
        if "中央" in response or "center" in response_lower:
            return ("center", None)
        
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return ("player", p.user_id)
    
    # デフォルト: ランダムに選択
    if random.random() < 0.5:
        return ("center", None)
    else:
        target = random.choice(other_players)
        return ("player", target.user_id)


async def llm_thief_action(
    game: GameState,
    player: Player,
    other_players: list[Player],
) -> Optional[int]:
    """
    怪盗のLLMプレイヤーが行動を決定する。
    
    Returns:
        target_id: 交換するプレイヤーのID、スキップならNone
    """
    system_prompt = build_system_prompt(player.initial_role)
    
    player_names = ", ".join(p.username for p in other_players)
    user_prompt = f"""あなたは怪盗です。夜フェーズで行動を選んでください。

選択肢:
1. プレイヤーを1人選んで、その人とカードを交換する（交換後、相手の役職があなたの新しい役職になります）
2. 何もしない

他のプレイヤー: {player_names}

以下の形式で回答してください:
- 交換する場合: "交換: [プレイヤー名]"
- 何もしない場合: "交換: なし"

どちらか1つだけ選んでください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    response = await call_grok_api(messages)
    
    if response:
        if "なし" in response or "skip" in response.lower():
            return None
        
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return p.user_id
    
    # デフォルト: 50%の確率で交換
    if random.random() < 0.5:
        return random.choice(other_players).user_id
    return None


async def llm_hunter_action(
    game: GameState,
    player: Player,
    other_players: list[Player],
) -> Optional[int]:
    """
    狩人のLLMプレイヤーが道連れ対象を決定する。
    
    Returns:
        target_id: 道連れ対象のID、指名しない場合はNone
    """
    system_prompt = build_system_prompt(player.initial_role)
    
    player_names = ", ".join(p.username for p in other_players)
    user_prompt = f"""あなたは狩人です。夜フェーズで道連れ対象を選んでください。

あなたが処刑された場合、指名したプレイヤーも道連れになります。

他のプレイヤー: {player_names}

以下の形式で回答してください:
- 道連れを指名する場合: "道連れ: [プレイヤー名]"
- 指名しない場合: "道連れ: なし"

どちらか1つだけ選んでください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    response = await call_grok_api(messages)
    
    if response:
        if "なし" in response or "skip" in response.lower():
            return None
        
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return p.user_id
    
    # デフォルト: ランダムに1人指名
    return random.choice(other_players).user_id


async def llm_vote(
    game: GameState,
    player: Player,
    other_players: list[Player],
    discussion_context: str = "",
) -> int:
    """
    LLMプレイヤーが投票先を決定する。
    
    Args:
        game: ゲーム状態
        player: 投票するプレイヤー
        other_players: 投票可能な他のプレイヤーリスト
        discussion_context: 議論の内容（オプション）
    
    Returns:
        投票先のuser_id。-1は平和村（誰も処刑しない）
    """
    system_prompt = build_system_prompt(player.current_role)
    
    player_info = []
    for p in other_players:
        player_info.append(f"- {p.username}")
    player_list = "\n".join(player_info)
    
    # 夜の行動結果があれば追加情報として含める
    night_info = ""
    if player.night_action and player.night_action.result:
        night_info = f"\n\n【あなたが夜に得た情報】\n{player.night_action.result}"
    
    user_prompt = f"""投票フェーズです。誰に投票しますか？

【他のプレイヤー】
{player_list}
{night_info}

【選択肢】
1. 上記のプレイヤーから1人を選んで投票する
2. 「平和村」を選ぶ（誰も処刑しない）

あなたの役職（{player.current_role.value}）と陣営の目標を考慮して、最善の選択をしてください。

以下の形式で回答してください:
- プレイヤーに投票: "投票: [プレイヤー名]"
- 平和村: "投票: 平和村"

理由は不要です。投票先のみ回答してください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    response = await call_grok_api(messages)
    
    if response:
        if "平和村" in response or "平和" in response:
            return -1
        
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return p.user_id
    
    # デフォルト: ランダムに投票（平和村を含む）
    choices = [p.user_id for p in other_players] + [-1]
    return random.choice(choices)


async def llm_generate_discussion_message(
    game: GameState,
    player: Player,
    other_players: list[Player],
    context: str = "",
) -> Optional[str]:
    """
    LLMプレイヤーが議論フェーズでの発言を生成する。
    
    Args:
        game: ゲーム状態
        player: 発言するプレイヤー
        other_players: 他のプレイヤーリスト
        context: これまでの議論内容
    
    Returns:
        発言内容
    """
    system_prompt = build_system_prompt(player.current_role)
    
    player_names = ", ".join(p.username for p in other_players)
    
    # 夜の行動結果
    night_info = ""
    if player.night_action and player.night_action.result:
        night_info = f"\n\n【夜に得た情報（他のプレイヤーには見えていない）】\n{player.night_action.result}"
    
    user_prompt = f"""昼の議論フェーズです。他のプレイヤーと話し合い、人狼を見つけ出しましょう。

【他のプレイヤー】
{player_names}
{night_info}

【これまでの発言】
{context if context else "（まだ発言がありません）"}

あなたの役職（{player.current_role.value}）と陣営の目標を考慮して発言してください。
- 嘘をついても構いません
- 他のプレイヤーに質問しても良いです
- 自分の役職をカミングアウトしても良いし、しなくても良いです

短く自然な発言をしてください（1〜2文程度）。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    return await call_grok_api(messages, temperature=0.9, max_tokens=128)

