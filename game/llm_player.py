"""
LLMプレイヤー

Grok 4.1 Fast APIを使用してAIプレイヤーを実装する。
人数が足りない場合にLLMプレイヤーで補完できる。
"""

import os
import random
import asyncio
import ssl
import time
from pathlib import Path
from typing import Optional
import httpx
from game.models import Role, GameState, Player, NightActionType


def get_perceived_role(player: Player) -> Role:
    """プレイヤーが自分で認識している役職を返す。

    怪盗が交換を実行した場合のみ、交換後の役職を知っている。
    それ以外のプレイヤーは初期役職のまま。
    """
    if player.initial_role == Role.THIEF:
        if player.night_action and player.night_action.action_type == NightActionType.THIEF_SWAP:
            return player.current_role
    return player.initial_role


def load_rules_md() -> str:
    """rules.mdファイルを読み込む。"""
    rules_path = Path(__file__).parent / "rules.md"
    if rules_path.exists():
        return rules_path.read_text(encoding="utf-8")
    return ""


# =============================================================================
# 設定
# =============================================================================

# xAI API設定
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# モデル名は環境変数で上書き可能
# 利用可能: grok-4-1-fast-reasoning, grok-4-1-fast-non-reasoning
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning")

# API呼び出しレート制限
API_CALL_INTERVAL = 1.0  # 最小呼び出し間隔（秒）
_last_api_call_time: float = 0

# 7種類のLLMキャラクター（名前、性格、口調、絵文字）
LLM_CHARACTERS = [
    {
        "name": "アリス",
        "emoji": "🎀",
        "personality": "明るくポジティブ。みんなを励ます。",
        "speech_style": "です・ます調。「〜だね！」「がんばろう！」"
    },
    {
        "name": "ボブ",
        "emoji": "🧢",
        "personality": "冷静で論理的。データを重視する。",
        "speech_style": "淡々とした口調。「〜だと思う」「論理的に考えると〜」"
    },
    {
        "name": "チャーリー",
        "emoji": "🕵️",
        "personality": "疑り深い。誰も信用しない。",
        "speech_style": "疑問形が多い。「本当に？」「怪しいな〜」"
    },
    {
        "name": "ダイアナ",
        "emoji": "👑",
        "personality": "自信家でリーダー気質。",
        "speech_style": "断定的。「間違いない」「私について来て」"
    },
    {
        "name": "エミリー",
        "emoji": "🌸",
        "personality": "優しくて協調的。争いを避ける。",
        "speech_style": "柔らかい口調。「〜かな？」「みんなはどう思う？」"
    },
    {
        "name": "フランク",
        "emoji": "🔥",
        "personality": "熱血で直感的。勢いで行動。",
        "speech_style": "熱い口調。「絶対〜だ！」「行くぞ！」"
    },
    {
        "name": "グレース",
        "emoji": "🔮",
        "personality": "神秘的で洞察力がある。",
        "speech_style": "含みのある言い方。「〜かもしれないわね」「見えるわ〜」"
    },
]

# 使用済みキャラクターのインデックス（ゲーム内で重複しないように）
_used_character_indices: set[int] = set()


def get_xai_api_key() -> Optional[str]:
    """環境変数からxAI APIキーを取得する。"""
    return os.getenv("XAI_API_KEY")


def get_next_llm_character(existing_names: set[str]) -> dict:
    """次のLLMキャラクターを取得（重複なし）"""
    global _used_character_indices

    # 名前が既に使われているキャラクターを除外
    available = [
        i for i in range(len(LLM_CHARACTERS))
        if i not in _used_character_indices
        and LLM_CHARACTERS[i]["name"] not in existing_names
    ]

    if not available:
        # 全て使用済みの場合はリセット
        _used_character_indices.clear()
        available = [
            i for i in range(len(LLM_CHARACTERS))
            if LLM_CHARACTERS[i]["name"] not in existing_names
        ]

    if not available:
        # それでもなければ最初のキャラクター
        available = list(range(len(LLM_CHARACTERS)))

    index = random.choice(available)
    _used_character_indices.add(index)
    return LLM_CHARACTERS[index]


def reset_character_selection() -> None:
    """キャラクター選択をリセット"""
    global _used_character_indices
    _used_character_indices.clear()


# =============================================================================
# プロンプトテンプレート
# =============================================================================

# ルールファイルを読み込み（モジュールロード時に1回だけ）
RULES_CONTENT = load_rules_md()

SYSTEM_PROMPT = """あなたは「ワンナイト人狼」というゲームのプレイヤーです。

{rules}

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
    "吊り人陣営": "あなたは人狼のふりをしてください。人狼だと疑われるように振る舞い、矛盾した発言や怪しい態度を取りましょう。ただし、自分から「吊ってほしい」「処刑してほしい」とは絶対に言わないでください。",
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


def build_role_composition_text(game: GameState) -> str:
    """ゲームの役職構成をテキスト形式で返す。"""
    from collections import Counter
    from config import ROLE_CONFIG

    if game.custom_role_config is not None:
        role_list = game.custom_role_config
    else:
        role_list = ROLE_CONFIG.get(game.player_count, [])

    # 役職ごとにカウント
    counter = Counter(r.value for r in role_list)
    composition = ", ".join(f"{role}: {count}" for role, count in counter.items())

    return (
        f"【このゲームの役職構成】\n"
        f"プレイヤー {game.player_count}人 + 中央カード 2枚\n"
        f"役職: {composition}\n"
        f"※ 中央カードには2枚の役職が伏せられています"
    )


def build_system_prompt(role: Role, game: Optional[GameState] = None) -> str:
    """役職に応じたシステムプロンプトを構築する。"""
    team_name = get_role_team_name(role)

    # 役職構成テキスト
    role_composition = ""
    if game is not None:
        role_composition = "\n\n" + build_role_composition_text(game)

    return SYSTEM_PROMPT.format(
        rules=RULES_CONTENT,
        role=role.value,
        team=team_name,
        role_description=get_role_description(role),
        goal=TEAM_GOALS.get(team_name, "ゲームを楽しみましょう。"),
    ) + role_composition


# =============================================================================
# LLM API呼び出し
# =============================================================================

async def call_grok_api(
    messages: list[dict[str, str]],
    temperature: float = 0.8,
    max_tokens: int = 256,
    max_retries: int = 3,
) -> Optional[str]:
    """
    Grok APIを呼び出してレスポンスを取得する。

    Args:
        messages: チャットメッセージのリスト
        temperature: 生成の温度パラメータ
        max_tokens: 最大トークン数
        max_retries: 接続エラー時の最大リトライ回数

    Returns:
        生成されたテキスト。エラー時はNone。
    """
    global _last_api_call_time

    api_key = get_xai_api_key()
    if not api_key:
        print("Warning: XAI_API_KEY is not set")
        return None

    # レート制限: 前回呼び出しから一定時間経過を待つ
    elapsed = time.time() - _last_api_call_time
    if elapsed < API_CALL_INTERVAL:
        await asyncio.sleep(API_CALL_INTERVAL - elapsed)
    _last_api_call_time = time.time()

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

    for attempt in range(max_retries):
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
            return None  # HTTPエラーはリトライしない
        except (httpx.RequestError, ssl.SSLError) as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 1  # 1秒, 2秒, 3秒
                print(f"Grok API connection error (attempt {attempt + 1}/{max_retries}): {e}")
                print(f"Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                continue
            print(f"Grok API connection error after {max_retries} retries: {e}")
            return None
        except (KeyError, IndexError) as e:
            print(f"Grok API response parse error: {e}")
            return None

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
    system_prompt = build_system_prompt(player.initial_role, game)
    
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
    system_prompt = build_system_prompt(player.initial_role, game)
    
    player_names = ", ".join(p.username for p in other_players)
    user_prompt = f"""あなたは怪盗です。夜フェーズで誰かとカードを交換してください。
交換後、相手の役職があなたの新しい役職になります。

他のプレイヤー: {player_names}

以下の形式で回答してください:
交換: [プレイヤー名]

プレイヤーを1人選んでください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    response = await call_grok_api(messages)

    if response:
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return p.user_id

    # デフォルト: 必ず交換
    return random.choice(other_players).user_id


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
    system_prompt = build_system_prompt(player.initial_role, game)
    
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


async def llm_hunter_revenge_action(
    game: GameState,
    player: Player,
    other_players: list[Player],
) -> int:
    """
    処刑されたLLM狩人が道連れ対象を決定する。
    議論内容や夜の情報を元に、最も人狼か大狼だと思うプレイヤーを選ぶ。

    Returns:
        target_id: 道連れ対象のID（必ず誰かを選ぶ）
    """
    system_prompt = build_system_prompt(player.current_role, game)

    player_info = []
    for p in other_players:
        player_info.append(f"- {p.username}")
    player_list = "\n".join(player_info)

    # 夜の行動結果があれば追加情報として含める
    night_info = ""
    if player.night_action and player.night_action.result:
        night_info = f"\n\n【あなたが夜に得た情報】\n{player.night_action.result}"

    # 議論履歴を取得
    discussion_text = ""
    if game.discussion_history:
        discussion_text = f"\n\n【議論の内容】\n{game.get_discussion_history_text(limit=9999)}"

    # 自分の発言履歴
    my_statements_text = ""
    if player.my_statements:
        recent_statements = player.my_statements[-5:]  # 最新5件
        my_statements_text = f"\n\n【あなたの過去の発言】\n" + "\n".join(f"- {s}" for s in recent_statements)

    user_prompt = f"""あなたは処刑されました！狩人の能力で、最も人狼か大狼だと思うプレイヤーを道連れにできます。

【他のプレイヤー】
{player_list}
{night_info}{discussion_text}{my_statements_text}

あなたの陣営の勝利のために、最も人狼か大狼だと思うプレイヤーを道連れにしてください。
議論の内容をよく思い出し、最も疑わしいプレイヤーを選んでください。
必ず誰かを道連れにしてください。

以下の形式で回答してください:
道連れ: [プレイヤー名]

理由は不要です。道連れ対象のみ回答してください。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = await call_grok_api(messages)

    if response:
        # プレイヤー名を探す
        for p in other_players:
            if p.username in response:
                return p.user_id

    # デフォルト: ランダムに1人指名（スキップしない）
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
        discussion_context: 議論の内容（履歴）
    
    Returns:
        投票先のuser_id。-1は平和村（誰も処刑しない）
    """
    # プレイヤーが認識している役職を取得
    perceived_role = get_perceived_role(player)

    # 吊り人は必ず平和村を選ぶ（自分が処刑される可能性を高める戦略）
    if perceived_role == Role.TANNER:
        return -1

    system_prompt = build_system_prompt(perceived_role, game)
    
    player_info = []
    for p in other_players:
        player_info.append(f"- {p.username}")
    player_list = "\n".join(player_info)
    
    # 夜の行動結果があれば追加情報として含める
    night_info = ""
    if player.night_action and player.night_action.result:
        night_info = f"\n\n【あなたが夜に得た情報】\n{player.night_action.result}"
    
    # 議論履歴を取得（引数で渡されたか、GameStateから取得）
    discussion_text = ""
    if discussion_context:
        discussion_text = f"\n\n【議論の内容】\n{discussion_context}"
    elif game.discussion_history:
        discussion_text = f"\n\n【議論の内容】\n{game.get_discussion_history_text(limit=9999)}"
    
    # 自分の発言履歴
    my_statements_text = ""
    if player.my_statements:
        recent_statements = player.my_statements[-5:]  # 最新5件
        my_statements_text = f"\n\n【あなたの過去の発言】\n" + "\n".join(f"- {s}" for s in recent_statements)
    
    user_prompt = f"""投票フェーズです。誰に投票しますか？

【他のプレイヤー】
{player_list}
{night_info}{discussion_text}{my_statements_text}

【選択肢】
1. 上記のプレイヤーから1人を選んで投票する
2. 「平和村」を選ぶ（誰も処刑しない）

あなたの役職（{perceived_role.value}）と陣営の目標を考慮して、最善の選択をしてください。
議論の内容をよく思い出し、最も疑わしいプレイヤーを投票してください。
あなたが人狼なら、自分以外の誰かに疑いを向けてください。

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
    _context: str = "",
) -> Optional[str]:
    """
    LLMプレイヤーが議論フェーズでの発言を生成する。

    Args:
        game: ゲーム状態
        player: 発言するプレイヤー
        other_players: 他のプレイヤーリスト
        _context: 最新の発言（トリガー）- 現在未使用、将来の拡張用

    Returns:
        発言内容
    """
    # プレイヤーが認識している役職を取得
    perceived_role = get_perceived_role(player)

    system_prompt = build_system_prompt(perceived_role, game)

    player_names = ", ".join(p.username for p in other_players)
    
    # 夜の行動結果
    night_info = ""
    if player.night_action and player.night_action.result:
        night_info = f"\n\n【夜に得た情報（他のプレイヤーには見えていない）】\n{player.night_action.result}"
    
    # 議論履歴全体を取得
    discussion_history_text = game.get_discussion_history_text(limit=15)
    
    # 自分の過去の発言
    my_statements_text = ""
    if player.my_statements:
        recent_statements = player.my_statements[-3:]  # 最新3件
        my_statements_text = f"\n\n【あなたの過去の発言】\n" + "\n".join(f"- {s}" for s in recent_statements)

    # 性格・口調設定
    personality_text = ""
    if player.personality:
        personality_text = f"\n\n【あなたの性格】\n{player.personality}"
    if player.speech_style:
        personality_text += f"\n\n【あなたの口調】\n{player.speech_style}"

    # 人狼協調：発言者が人狼の場合、仲間の発言を追跡
    wolf_cooperation_text = ""
    if player.initial_role in (Role.WEREWOLF, Role.ALPHA_WOLF):
        # 仲間の人狼を探す
        fellow_wolves = [
            p for p in other_players
            if p.initial_role in (Role.WEREWOLF, Role.ALPHA_WOLF)
        ]
        if fellow_wolves:
            wolf_names = ", ".join(w.username for w in fellow_wolves)
            # 仲間の発言を議論履歴から抽出
            wolf_statements = []
            for speaker_name, msg in game.discussion_history[-15:]:
                if any(w.username == speaker_name for w in fellow_wolves):
                    wolf_statements.append(f"- {speaker_name}: {msg}")
            wolf_statements_text = "\n".join(wolf_statements[-5:]) if wolf_statements else "（まだ発言なし）"
            wolf_cooperation_text = f"""

【重要：あなたは人狼です】
仲間の人狼: {wolf_names}
仲間の最近の発言:
{wolf_statements_text}

※ 仲間の発言と矛盾しないように注意してください。
※ 仲間を庇いすぎると疑われるので自然に振る舞ってください。"""

    # 吊り人の場合：戦略的注意事項
    tanner_warning_text = ""
    if perceived_role == Role.TANNER:
        tanner_warning_text = """

【重要：あなたは吊り人です - 戦略的アドバイス】
- 人狼のふりをして疑いを集めてください
- 矛盾した発言や、他人を不自然に庇うなど怪しい行動を取りましょう
- 「吊ってほしい」「処刑してほしい」「自分を投票して」などの発言は絶対にしないでください
- 不自然な自白や、わざとらしい怪しさは逆効果です。さりげなく怪しく振る舞いましょう
- 他のプレイヤーに「人狼では？」と疑われるのが理想です"""

    user_prompt = f"""昼の議論フェーズです。他のプレイヤーと話し合い、人狼を見つけ出しましょう。

【他のプレイヤー】
{player_names}
{night_info}{personality_text}{wolf_cooperation_text}{tanner_warning_text}

【これまでの議論】
{discussion_history_text}{my_statements_text}

あなたの役職（{perceived_role.value}）と陣営の目標を考慮して発言してください。
- 嘘をついても構いません
- 他のプレイヤーに質問しても良いです
- 自分の役職をカミングアウトしても良いし、しなくても良いです
- 過去の自分の発言と矛盾しないようにしてください
- 議論の流れに沿った発言をしてください
- あなたの性格と口調に合った発言をしてください

短く自然な発言をしてください（1〜2文程度）。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    
    return await call_grok_api(messages, temperature=0.9, max_tokens=128)

