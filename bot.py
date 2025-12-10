"""
ワンナイト人狼 Discord Bot

エントリーポイント。Discord Botの起動とコマンド定義を行う。
スラッシュコマンド（/onj）を使用。
"""

import os
import asyncio
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

from collections import Counter
from config import (
    ROLE_CONFIG,
    MIN_PLAYERS,
    MAX_PLAYERS,
    CENTER_CARD_COUNT,
    MESSAGES,
    ROLE_DESCRIPTIONS,
    DISCUSSION_TIME,
)
from game.models import Role, GamePhase, GameState, Player
from game.logic import (
    setup_game,
    process_werewolf_night,
    process_seer_action,
    process_thief_action,
    process_hunter_action,
    register_vote,
    calculate_votes,
    determine_execution,
    get_executed_hunters,
    add_hunter_target_to_execution,
    determine_winner,
    get_winner_message,
    get_final_roles_message,
    get_execution_message,
    get_current_night_role,
    advance_night_phase,
    is_night_phase_complete,
)
from game.llm_player import (
    get_next_llm_character,
    reset_character_selection,
    llm_seer_action,
    llm_thief_action,
    llm_hunter_action,
    llm_hunter_revenge_action,
    llm_vote,
    llm_generate_discussion_message,
    get_xai_api_key,
)

# 環境変数の読み込み
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # テスト用サーバーのID（オプション）

if not TOKEN:
    raise ValueError("DISCORD_TOKEN が設定されていません。.env ファイルを確認してください。")


# =============================================================================
# Bot設定
# =============================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# チャンネルごとのゲーム状態を管理
# channel_id -> GameState
games: dict[int, GameState] = {}


# =============================================================================
# ユーティリティ関数
# =============================================================================

def get_game(channel_id: int) -> Optional[GameState]:
    """チャンネルのゲーム状態を取得する。"""
    return games.get(channel_id)


def create_game(channel_id: int, host_id: int) -> GameState:
    """新しいゲームを作成する。"""
    state = GameState(channel_id=channel_id, host_id=host_id)
    games[channel_id] = state
    return state


def end_game(channel_id: int) -> None:
    """ゲームを終了し、状態を削除する。"""
    if channel_id in games:
        del games[channel_id]


def reset_game_keep_players(game: GameState) -> None:
    """ゲームをリセットし、参加者は保持する（再戦用）。"""
    from game.models import Role
    
    # 各プレイヤーの状態をリセット
    for player in game.players.values():
        player.initial_role = Role.VILLAGER  # 仮の役職
        player.current_role = Role.VILLAGER
        player.night_action = None
        player.has_acted = False
        player.vote_target_id = None
        player.my_statements.clear()  # 発言履歴をリセット
    
    # ゲーム状態をリセット
    game.phase = GamePhase.WAITING
    game.center_cards.clear()
    game.current_night_role = None
    game.night_action_order.clear()
    game.night_action_index = 0
    game.executed_player_ids.clear()
    game.winners.clear()
    game.discussion_history.clear()  # 議論履歴をリセット


async def send_role_dm(user: discord.User, player: Player) -> bool:
    """プレイヤーにDMで役職を通知する。"""
    try:
        role = player.initial_role
        description = ROLE_DESCRIPTIONS.get(role, "")
        message = MESSAGES["role_notification"].format(
            role=role.value,
            description=description
        )
        await user.send(message)
        return True
    except discord.Forbidden:
        return False


# =============================================================================
# スラッシュコマンドグループ
# =============================================================================

class OnenightCommands(app_commands.Group):
    """ワンナイト人狼のコマンドグループ"""
    
    def __init__(self):
        super().__init__(name="onj", description="ワンナイト人狼のコマンド")
    
    @app_commands.command(name="start", description="ゲームの参加者募集を開始する")
    async def start(self, interaction: discord.Interaction) -> None:
        """ゲームの募集を開始する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        # 既存のゲームがあるか確認
        existing_game = get_game(channel_id)
        if existing_game and existing_game.phase != GamePhase.ENDED:
            await interaction.response.send_message(
                MESSAGES["game_already_running"],
                ephemeral=True
            )
            return
        
        # 新しいゲームを作成
        game = create_game(channel_id, interaction.user.id)
        game.add_player(interaction.user.id, interaction.user.display_name)
        
        await interaction.response.send_message(
            f"🐺 **ワンナイト人狼** の参加者を募集中！\n"
            f"`/onj join` で参加してください。\n"
            f"現在の参加者: 1人 ({interaction.user.display_name})\n\n"
            f"参加者が {MIN_PLAYERS}〜{MAX_PLAYERS}人 になったら、\n"
            f"ホストは `/onj begin` でゲームを開始できます。"
        )
    
    @app_commands.command(name="join", description="ゲームに参加する")
    async def join(self, interaction: discord.Interaction) -> None:
        """ゲームに参加する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                "⚠️ 現在参加募集中のゲームがありません。`/onj start` で開始してください。",
                ephemeral=True
            )
            return
        
        if game.player_count >= MAX_PLAYERS:
            await interaction.response.send_message(
                MESSAGES["too_many_players"].format(max=MAX_PLAYERS),
                ephemeral=True
            )
            return
        
        if not game.add_player(interaction.user.id, interaction.user.display_name):
            await interaction.response.send_message(
                MESSAGES["already_joined"],
                ephemeral=True
            )
            return
        
        player_names = ", ".join(p.username for p in game.player_list)
        
        # カスタム役職構成がある場合の警告
        warning = ""
        if game.custom_role_config is not None:
            required = game.player_count + CENTER_CARD_COUNT
            if len(game.custom_role_config) != required:
                warning = f"\n⚠️ 役職構成の調整が必要です（`/onj roles` で変更してください）"
        
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name} さんが参加しました！\n"
            f"現在の参加者: {game.player_count}人 ({player_names}){warning}"
        )
    
    @app_commands.command(name="leave", description="ゲームから離脱する")
    async def leave(self, interaction: discord.Interaction) -> None:
        """ゲームから離脱する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                MESSAGES["wrong_phase"],
                ephemeral=True
            )
            return
        
        if not game.remove_player(interaction.user.id):
            await interaction.response.send_message(
                MESSAGES["not_in_game"],
                ephemeral=True
            )
            return
        
        # ホストが離脱した場合はゲームをキャンセル
        if interaction.user.id == game.host_id:
            end_game(channel_id)
            await interaction.response.send_message(
                "❌ ホストが離脱したため、ゲームがキャンセルされました。"
            )
            return
        
        player_names = ", ".join(p.username for p in game.player_list)
        
        # カスタム役職構成がある場合の警告
        warning = ""
        if game.custom_role_config is not None:
            required = game.player_count + CENTER_CARD_COUNT
            if len(game.custom_role_config) != required:
                warning = f"\n⚠️ 役職構成の調整が必要です（`/onj roles` で変更してください）"
        
        await interaction.response.send_message(
            f"❌ {interaction.user.display_name} さんが離脱しました。\n"
            f"現在の参加者: {game.player_count}人 ({player_names}){warning}"
        )
    
    @app_commands.command(name="players", description="現在の参加者を表示する")
    async def players(self, interaction: discord.Interaction) -> None:
        """現在の参加者を表示する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None:
            await interaction.response.send_message(
                "⚠️ このチャンネルでゲームは行われていません。",
                ephemeral=True
            )
            return
        
        player_list = "\n".join(
            f"• {p.username}" + (" (ホスト)" if p.user_id == game.host_id else "")
            for p in game.player_list
        )
        
        phase_names = {
            GamePhase.WAITING: "参加募集中",
            GamePhase.NIGHT: "夜フェーズ",
            GamePhase.DISCUSSION: "議論フェーズ",
            GamePhase.VOTING: "投票フェーズ",
            GamePhase.ENDED: "終了",
        }
        
        await interaction.response.send_message(
            f"📋 **参加者一覧** ({game.player_count}人)\n"
            f"フェーズ: {phase_names.get(game.phase, '不明')}\n\n"
            f"{player_list}",
            ephemeral=True
        )
    
    @app_commands.command(name="roles", description="役職構成を変更する（ホストのみ）")
    async def roles(self, interaction: discord.Interaction) -> None:
        """役職構成を変更する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                "⚠️ 参加募集中のゲームがありません。`/onj start` で開始してください。",
                ephemeral=True
            )
            return
        
        if interaction.user.id != game.host_id:
            await interaction.response.send_message(
                MESSAGES["not_host"],
                ephemeral=True
            )
            return
        
        if game.player_count < MIN_PLAYERS:
            await interaction.response.send_message(
                f"⚠️ プレイヤーが{MIN_PLAYERS}人以上必要です。（現在{game.player_count}人）\n"
                f"役職構成はプレイヤー人数が確定してから設定してください。",
                ephemeral=True
            )
            return
        
        # 役職構成変更UIを表示
        view = RoleConfigView(game, interaction.user.id)
        await interaction.response.send_message(
            get_role_config_message(game),
            view=view
        )
    
    @app_commands.command(name="begin", description="ゲームを開始する（ホストのみ）")
    async def begin(self, interaction: discord.Interaction) -> None:
        """ゲームを開始する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                MESSAGES["wrong_phase"],
                ephemeral=True
            )
            return
        
        if interaction.user.id != game.host_id:
            await interaction.response.send_message(
                MESSAGES["not_host"],
                ephemeral=True
            )
            return
        
        if game.player_count < MIN_PLAYERS:
            await interaction.response.send_message(
                MESSAGES["not_enough_players"].format(min=MIN_PLAYERS, current=game.player_count),
                ephemeral=True
            )
            return
        
        if game.player_count > MAX_PLAYERS:
            await interaction.response.send_message(
                MESSAGES["too_many_players"].format(max=MAX_PLAYERS),
                ephemeral=True
            )
            return
        
        # 役職構成を取得（カスタム設定があればそちらを優先）
        if game.custom_role_config is not None:
            role_list = game.custom_role_config
            config_type = "カスタム"
        else:
            role_list = ROLE_CONFIG.get(game.player_count)
            config_type = "デフォルト"
        
        if role_list is None:
            await interaction.response.send_message(
                f"⚠️ {game.player_count}人用の役職構成が定義されていません。",
                ephemeral=True
            )
            return
        
        # 役職構成の枚数チェック
        required_cards = game.player_count + CENTER_CARD_COUNT
        if len(role_list) != required_cards:
            await interaction.response.send_message(
                f"⚠️ 役職カードの枚数が不正です。\n"
                f"必要: {required_cards}枚、現在: {len(role_list)}枚\n"
                f"`/onj roles` で役職構成を調整してください。",
                ephemeral=True
            )
            return
        
        # 役職構成を集計して表示用文字列を作成
        role_counts = Counter(role.value for role in role_list)
        role_composition = "、".join(
            f"{role}×{count}" if count > 1 else role
            for role, count in role_counts.items()
        )
        
        await interaction.response.send_message(
            f"🌙 **ゲームを開始します！**\n\n"
            f"📋 **役職構成（{len(role_list)}枚・{config_type}）**\n"
            f"{role_composition}\n"
            f"（プレイヤー{game.player_count}人 + 中央カード{CENTER_CARD_COUNT}枚）\n\n"
            f"各プレイヤーにDMで役職を通知します..."
        )
        
        # ゲームをセットアップ
        setup_game(game, role_list)
        
        # 各プレイヤーにDMで役職を通知（LLMプレイヤーはスキップ）
        dm_failed: list[str] = []
        for player in game.player_list:
            # LLMプレイヤーはDMを送信しない
            if player.is_llm:
                continue
            
            user = bot.get_user(player.user_id)
            if user is None:
                try:
                    user = await bot.fetch_user(player.user_id)
                except discord.NotFound:
                    dm_failed.append(player.username)
                    continue
            
            success = await send_role_dm(user, player)
            if not success:
                dm_failed.append(player.username)
        
        if dm_failed:
            if interaction.channel:
                await interaction.channel.send(
                    f"⚠️ 以下のプレイヤーにDMを送信できませんでした: {', '.join(dm_failed)}\n"
                    f"DMを受け取れるよう設定を確認してください。"
                )
        
        # 夜フェーズを開始
        await start_night_phase(interaction.channel, game)
    
    async def vote_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """投票先のオートコンプリート（ゲーム参加者のみ表示）"""
        channel_id = interaction.channel_id
        if channel_id is None:
            return []
        
        game = get_game(channel_id)
        if game is None or game.phase != GamePhase.VOTING:
            return []
        
        # 自分以外のゲーム参加者をフィルタリング
        choices = []

        # 平和村オプションを最初に追加
        if "平和" in current.lower() or current == "":
            choices.append(
                app_commands.Choice(name="平和村", value="-1")
            )

        for player in game.player_list:
            if player.user_id == interaction.user.id:
                continue  # 自分自身は除外
            if current.lower() in player.username.lower():
                choices.append(
                    app_commands.Choice(name=player.username, value=str(player.user_id))
                )

        return choices[:25]  # Discord の上限は25件
    
    @app_commands.command(name="vote", description="プレイヤーに投票する")
    @app_commands.describe(player="投票先のプレイヤー")
    @app_commands.autocomplete(player=vote_autocomplete)
    async def vote(self, interaction: discord.Interaction, player: str) -> None:
        """プレイヤーに投票する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.VOTING:
            await interaction.response.send_message(
                MESSAGES["wrong_phase"],
                ephemeral=True
            )
            return
        
        voter = game.get_player(interaction.user.id)
        if voter is None:
            await interaction.response.send_message(
                MESSAGES["not_in_game"],
                ephemeral=True
            )
            return
        
        if voter.vote_target_id is not None:
            await interaction.response.send_message(
                MESSAGES["already_voted"],
                ephemeral=True
            )
            return

        # 平和村投票の処理
        if player == "-1":
            voter.vote_target_id = -1
            await interaction.response.send_message(
                f"✅ {interaction.user.display_name} さんが投票しました。"
                f"（{game.voted_count()}/{game.player_count}）"
            )
            if game.all_voted():
                await end_voting_phase(interaction.channel, game)
            return

        # player はユーザーIDの文字列
        try:
            target_id = int(player)
        except ValueError:
            # 名前で検索を試みる
            target = None
            for p in game.player_list:
                if p.username.lower() == player.lower():
                    target = p
                    break
            if target is None:
                await interaction.response.send_message(
                    MESSAGES["invalid_target"],
                    ephemeral=True
                )
                return
            target_id = target.user_id
        
        target = game.get_player(target_id)
        if target is None:
            await interaction.response.send_message(
                MESSAGES["invalid_target"],
                ephemeral=True
            )
            return
        
        if interaction.user.id == target_id:
            await interaction.response.send_message(
                MESSAGES["cannot_vote_self"],
                ephemeral=True
            )
            return
        
        if not register_vote(game, interaction.user.id, target_id):
            await interaction.response.send_message(
                "⚠️ 投票に失敗しました。",
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name} さんが投票しました。"
            f"（{game.voted_count()}/{game.player_count}）"
        )
        
        # 全員投票完了したら結果発表
        if game.all_voted():
            await end_voting_phase(interaction.channel, game)

    @app_commands.command(name="cancel", description="ゲームをキャンセルする（ホストのみ）")
    async def cancel(self, interaction: discord.Interaction) -> None:
        """ゲームをキャンセルする。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None:
            await interaction.response.send_message(
                "⚠️ このチャンネルでゲームは行われていません。",
                ephemeral=True
            )
            return
        
        if interaction.user.id != game.host_id:
            await interaction.response.send_message(
                MESSAGES["not_host"],
                ephemeral=True
            )
            return
        
        end_game(channel_id)
        await interaction.response.send_message("❌ ゲームがキャンセルされました。")
    
    @app_commands.command(name="add_bot", description="AIプレイヤーを追加する（ホストのみ）")
    @app_commands.describe(count="追加するAIプレイヤーの人数（デフォルト: 1）")
    async def add_bot(self, interaction: discord.Interaction, count: int = 1) -> None:
        """AIプレイヤーを追加する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                "⚠️ 参加募集中のゲームがありません。`/onj start` で開始してください。",
                ephemeral=True
            )
            return
        
        if interaction.user.id != game.host_id:
            await interaction.response.send_message(
                MESSAGES["not_host"],
                ephemeral=True
            )
            return
        
        # APIキーチェック
        if not get_xai_api_key():
            await interaction.response.send_message(
                "⚠️ XAI_API_KEY が設定されていません。\n"
                ".env ファイルに `XAI_API_KEY=your_api_key` を追加してください。",
                ephemeral=True
            )
            return
        
        if count < 1 or count > 7:
            await interaction.response.send_message(
                "⚠️ 追加できるAIプレイヤーは1〜7人です。",
                ephemeral=True
            )
            return
        
        if game.player_count + count > MAX_PLAYERS:
            await interaction.response.send_message(
                f"⚠️ プレイヤー数の上限は{MAX_PLAYERS}人です。"
                f"（現在{game.player_count}人、追加可能: {MAX_PLAYERS - game.player_count}人）",
                ephemeral=True
            )
            return
        
        # AIプレイヤーを追加
        existing_names = {p.username for p in game.player_list}
        added_names = []

        for _ in range(count):
            # LLMプレイヤー用に負のIDを生成（重複しないように）
            llm_id = -1000 - game.llm_player_count - len(added_names)

            # キャラクターを取得
            character = get_next_llm_character(existing_names)
            name = character["name"]
            existing_names.add(name)

            game.add_player(llm_id, name, is_llm=True)

            # キャラクター設定を割り当て
            player = game.get_player(llm_id)
            if player:
                player.personality = character["personality"]
                player.speech_style = character["speech_style"]
                player.emoji = character["emoji"]

            added_names.append(f"{character['emoji']} {name}")
        
        player_names = ", ".join(p.username for p in game.player_list)
        
        # カスタム役職構成がある場合の警告
        warning = ""
        if game.custom_role_config is not None:
            required = game.player_count + CENTER_CARD_COUNT
            if len(game.custom_role_config) != required:
                warning = f"\n⚠️ 役職構成の調整が必要です（`/onj roles` で変更してください）"
        
        await interaction.response.send_message(
            f"🤖 AIプレイヤーを追加しました: {', '.join(added_names)}\n"
            f"現在の参加者: {game.player_count}人 ({player_names}){warning}"
        )
    
    @app_commands.command(name="remove_bot", description="AIプレイヤーを削除する（ホストのみ）")
    @app_commands.describe(count="削除するAIプレイヤーの人数（デフォルト: 1、0で全削除）")
    async def remove_bot(self, interaction: discord.Interaction, count: int = 1) -> None:
        """AIプレイヤーを削除する。"""
        channel_id = interaction.channel_id
        
        if channel_id is None:
            await interaction.response.send_message("このチャンネルでは使用できません。", ephemeral=True)
            return
        
        game = get_game(channel_id)
        
        if game is None or game.phase != GamePhase.WAITING:
            await interaction.response.send_message(
                MESSAGES["wrong_phase"],
                ephemeral=True
            )
            return
        
        if interaction.user.id != game.host_id:
            await interaction.response.send_message(
                MESSAGES["not_host"],
                ephemeral=True
            )
            return
        
        llm_players = game.get_llm_players()
        
        if not llm_players:
            await interaction.response.send_message(
                "⚠️ AIプレイヤーはいません。",
                ephemeral=True
            )
            return
        
        # count=0 の場合は全削除
        if count == 0:
            count = len(llm_players)
        
        removed_names = []
        for i, player in enumerate(llm_players):
            if i >= count:
                break
            game.remove_player(player.user_id)
            removed_names.append(player.username)
        
        player_names = ", ".join(p.username for p in game.player_list) if game.player_count > 0 else "なし"
        
        # カスタム役職構成がある場合の警告
        warning = ""
        if game.custom_role_config is not None and game.player_count > 0:
            required = game.player_count + CENTER_CARD_COUNT
            if len(game.custom_role_config) != required:
                warning = f"\n⚠️ 役職構成の調整が必要です（`/onj roles` で変更してください）"
        
        await interaction.response.send_message(
            f"🤖 AIプレイヤーを削除しました: {', '.join(removed_names)}\n"
            f"現在の参加者: {game.player_count}人 ({player_names}){warning}"
        )
    
    @app_commands.command(name="help", description="コマンド一覧と遊び方を表示する")
    async def help(self, interaction: discord.Interaction) -> None:
        """ヘルプを表示する。"""
        help_text = """🐺 **ワンナイト人狼 ヘルプ**

**【コマンド一覧】**
`/onj start` - ゲームの参加者募集を開始
`/onj join` - ゲームに参加
`/onj leave` - ゲームから離脱
`/onj players` - 参加者一覧を表示
`/onj roles` - 役職構成を変更（ホストのみ）
`/onj begin` - ゲームを開始（ホストのみ）
`/onj vote <プレイヤー>` - プレイヤーに投票（平和村も選択可）
`/onj cancel` - ゲームをキャンセル（ホストのみ）
`/onj add_bot [人数]` - AIプレイヤーを追加（ホストのみ）
`/onj remove_bot [人数]` - AIプレイヤーを削除（ホストのみ）
`/onj help` - このヘルプを表示

**【遊び方】**
1️⃣ `/onj start` でゲームを開始し、参加者を募集
2️⃣ 参加者は `/onj join` で参加（3〜8人）
3️⃣ 人数が足りない時は `/onj add_bot` でAIプレイヤーを追加
4️⃣ ホストは `/onj roles` で役職構成を変更可能
5️⃣ ホストが `/onj begin` でゲーム開始
6️⃣ 各プレイヤーにDMで役職が通知される
7️⃣ 夜フェーズ：役職に応じてDMで行動
8️⃣ 昼フェーズ：議論後、投票で処刑者を決定
9️⃣ 結果発表！

**【役職】**
🧑‍🌾 **村人** - 特殊能力なし
🐺 **人狼** - 仲間の人狼を確認できる
🐺👑 **大狼** - 人狼＋中央カードも見れる
🔮 **占い師** - 他プレイヤー1人 or 中央カード2枚を見る
🦹 **怪盗** - 他プレイヤーとカードを交換
🏹 **狩人** - 処刑されたら指名者を道連れ
🎭 **吊り人** - 自分が処刑されれば単独勝利
🤪 **狂人** - 人狼陣営だが人狼が誰かわからない
👑 **村長** - 投票時に2票を持つ

**【勝利条件】**
• **村人陣営**: 人狼を1人以上処刑する
• **人狼陣営**: 人狼/大狼が処刑されない（狂人も勝利）
• **吊り人**: 自分が処刑される（単独勝利）

**【AIプレイヤーについて】**
🤖 人数が足りない場合、AIプレイヤーで補完できます。
AIプレイヤーは Grok 4.1 Fast を使用し、役職に応じて
自動で夜の行動と投票を行います。"""
        
        await interaction.response.send_message(help_text, ephemeral=True)


# コマンドグループをBotに追加
bot.tree.add_command(OnenightCommands())


# =============================================================================
# 役職構成変更UI
# =============================================================================

# 利用可能な役職リスト
AVAILABLE_ROLES = [Role.VILLAGER, Role.WEREWOLF, Role.ALPHA_WOLF, Role.SEER, Role.THIEF, Role.HUNTER, Role.TANNER, Role.MADMAN, Role.MAYOR]

# 役職の絵文字
ROLE_EMOJI = {
    Role.VILLAGER: "🧑‍🌾",
    Role.WEREWOLF: "🐺",
    Role.ALPHA_WOLF: "🐺👑",
    Role.SEER: "🔮",
    Role.THIEF: "🦹",
    Role.HUNTER: "🏹",
    Role.TANNER: "🎭",
    Role.MADMAN: "🤪",
    Role.MAYOR: "👑",
}


def get_role_config_message(game: GameState) -> str:
    """現在の役職構成を表示するメッセージを生成する。"""
    # 現在の役職構成を取得
    if game.custom_role_config is not None:
        role_list = game.custom_role_config
        config_type = "カスタム"
    else:
        role_list = ROLE_CONFIG.get(game.player_count, [])
        config_type = "デフォルト"
    
    # 役職をカウント
    role_counts = Counter(role_list)
    
    lines = [f"📋 **役職構成**（{config_type}）"]
    lines.append("")
    
    for role in AVAILABLE_ROLES:
        count = role_counts.get(role, 0)
        emoji = ROLE_EMOJI.get(role, "")
        lines.append(f"{emoji} {role.value}: **{count}枚**")
    
    lines.append("")
    lines.append(f"合計: **{len(role_list)}枚**（プレイヤー{game.player_count}人 + 中央{CENTER_CARD_COUNT}枚 = {game.player_count + CENTER_CARD_COUNT}枚必要）")
    
    # 枚数チェック
    required = game.player_count + CENTER_CARD_COUNT
    if len(role_list) != required:
        diff = len(role_list) - required
        if diff > 0:
            lines.append(f"⚠️ {diff}枚多いです")
        else:
            lines.append(f"⚠️ {-diff}枚足りません")
    else:
        lines.append("✅ 枚数OK")
    
    return "\n".join(lines)


class RoleConfigView(discord.ui.View):
    """役職構成を変更するためのView"""
    
    def __init__(self, game: GameState, host_id: int):
        super().__init__(timeout=300)  # 5分でタイムアウト
        self.game = game
        self.host_id = host_id
        self._add_buttons()
    
    def _add_buttons(self) -> None:
        """役職ごとに増減ボタンを追加する（Discord最大5行制限対応）"""
        # Discordは最大5行（row 0-4）まで
        # 9役職 × 2ボタン = 18ボタン
        # 1行に3役職（6ボタン）は不可（1行5ボタンまで）
        # 解決策: 最初の4行に2役職ずつ（8役職）、最後の1行に1役職+リセット+完了
        
        for idx, role in enumerate(AVAILABLE_ROLES):
            # 最初の8役職は2役職/行で配置（row 0-3）
            # 最後の役職（idx=8）はrow 4に配置
            if idx < 8:
                row = idx // 2
            else:
                row = 4
            
            # 追加ボタン
            add_btn = discord.ui.Button(
                label=f"+{role.value}",
                style=discord.ButtonStyle.success,
                custom_id=f"add_{role.name}",
                row=row,
            )
            add_btn.callback = self._make_add_callback(role)
            self.add_item(add_btn)
            
            # 削除ボタン
            remove_btn = discord.ui.Button(
                label=f"-{role.value}",
                style=discord.ButtonStyle.danger,
                custom_id=f"remove_{role.name}",
                row=row,
            )
            remove_btn.callback = self._make_remove_callback(role)
            self.add_item(remove_btn)
        
        # リセットボタン（row 4、最後の役職と同じ行）
        reset_btn = discord.ui.Button(
            label="🔄 リセット",
            style=discord.ButtonStyle.secondary,
            custom_id="reset",
            row=4,
        )
        reset_btn.callback = self._reset_callback
        self.add_item(reset_btn)
        
        # 完了ボタン（row 4、最後の役職と同じ行）
        done_btn = discord.ui.Button(
            label="✅ 完了",
            style=discord.ButtonStyle.primary,
            custom_id="done",
            row=4,
        )
        done_btn.callback = self._done_callback
        self.add_item(done_btn)
    
    def _get_current_roles(self) -> list[Role]:
        """現在の役職構成を取得する"""
        if self.game.custom_role_config is not None:
            return self.game.custom_role_config.copy()
        return ROLE_CONFIG.get(self.game.player_count, []).copy()
    
    def _make_add_callback(self, role: Role):
        """役職追加のコールバックを作成する"""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.host_id:
                await interaction.response.send_message(
                    "⚠️ ホストのみ役職構成を変更できます。",
                    ephemeral=True
                )
                return
            
            # 現在の構成を取得して役職を追加
            current = self._get_current_roles()
            current.append(role)
            self.game.custom_role_config = current
            
            await interaction.response.edit_message(
                content=get_role_config_message(self.game),
                view=self
            )
        return callback
    
    def _make_remove_callback(self, role: Role):
        """役職削除のコールバックを作成する"""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.host_id:
                await interaction.response.send_message(
                    "⚠️ ホストのみ役職構成を変更できます。",
                    ephemeral=True
                )
                return
            
            # 現在の構成を取得して役職を削除
            current = self._get_current_roles()
            if role in current:
                current.remove(role)
                self.game.custom_role_config = current
            
            await interaction.response.edit_message(
                content=get_role_config_message(self.game),
                view=self
            )
        return callback
    
    async def _reset_callback(self, interaction: discord.Interaction):
        """デフォルトにリセットするコールバック"""
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "⚠️ ホストのみ役職構成を変更できます。",
                ephemeral=True
            )
            return
        
        self.game.custom_role_config = None
        
        await interaction.response.edit_message(
            content=get_role_config_message(self.game),
            view=self
        )
    
    async def _done_callback(self, interaction: discord.Interaction):
        """完了ボタンのコールバック"""
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "⚠️ ホストのみ操作できます。",
                ephemeral=True
            )
            return
        
        # 枚数チェック
        role_list = self._get_current_roles()
        required = self.game.player_count + CENTER_CARD_COUNT
        
        if len(role_list) != required:
            diff = len(role_list) - required
            if diff > 0:
                await interaction.response.send_message(
                    f"⚠️ 役職が{diff}枚多いです。調整してください。",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ 役職が{-diff}枚足りません。調整してください。",
                    ephemeral=True
                )
            return
        
        # 設定完了
        self.stop()
        
        # 役職構成を表示用に整形
        role_counts = Counter(r.value for r in role_list)
        role_composition = "、".join(
            f"{r}×{c}" if c > 1 else r
            for r, c in role_counts.items()
        )
        
        await interaction.response.edit_message(
            content=f"✅ **役職構成を設定しました！**\n\n{role_composition}\n\n`/onj begin` でゲームを開始できます。",
            view=None
        )


# =============================================================================
# 夜フェーズ処理
# =============================================================================

async def start_night_phase(channel: discord.abc.Messageable, game: GameState) -> None:
    """夜フェーズを開始する。"""
    await channel.send(MESSAGES["night_start"])
    
    # 人狼の行動
    await process_werewolves(game)
    
    # 占い師の行動
    await process_seers(channel, game)
    
    # 怪盗の行動
    await process_thieves(channel, game)
    
    # 狩人の行動
    await process_hunters(channel, game)
    
    # 昼フェーズへ
    await start_day_phase(channel, game)


async def process_werewolves(game: GameState) -> None:
    """人狼・大狼の夜行動を処理する。"""
    result = process_werewolf_night(game)
    
    for user_id, other_wolves in result.items():
        player = game.get_player(user_id)
        if player is None:
            continue
        
        # LLMプレイヤーはDMをスキップ
        if player.is_llm:
            continue
        
        user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except discord.NotFound:
                continue
        
        try:
            # 仲間の人狼情報
            if other_wolves:
                partner_names = ", ".join(w.username for w in other_wolves)
                message = f"🐺 他の人狼: **{partner_names}**"
            else:
                message = MESSAGES["werewolf_alone"]
            
            # 大狼は中央カードも確認
            if player.initial_role == Role.ALPHA_WOLF:
                center_cards = game.center_cards
                center_text = ", ".join(f"**{r.value}**" for r in center_cards)
                message += f"\n\n🔮 中央カード: {center_text}"
            
            await user.send(message)
        except discord.Forbidden:
            pass
    
    advance_night_phase(game)


async def process_seers(channel: discord.abc.Messageable, game: GameState) -> None:
    """占い師の夜行動を処理する。"""
    seers = game.get_players_by_initial_role(Role.SEER)
    
    if not seers:
        advance_night_phase(game)
        return
    
    # LLMプレイヤーと人間プレイヤーを分離
    human_seers = [s for s in seers if not s.is_llm]
    llm_seers = [s for s in seers if s.is_llm]
    
    # 人間プレイヤーにDMを送信
    for seer in human_seers:
        user = bot.get_user(seer.user_id)
        if user is None:
            try:
                user = await bot.fetch_user(seer.user_id)
            except discord.NotFound:
                continue
        
        try:
            # 他プレイヤーのリストを作成
            other_players = [
                p for p in game.player_list 
                if p.user_id != seer.user_id
            ]
            player_list = "\n".join(
                f"• {p.username}" for p in other_players
            )
            
            await user.send(
                f"🔮 **占い師の行動**\n\n"
                f"以下のいずれかのコマンドをこのDMで入力してください：\n\n"
                f"**プレイヤーを占う場合:**\n"
                f"`!seer player プレイヤー名`\n"
                f"（対象プレイヤー: {', '.join(p.username for p in other_players)}）\n\n"
                f"**中央カード2枚を見る場合:**\n"
                f"`!seer center`"
            )
        except discord.Forbidden:
            pass
    
    # LLMプレイヤーの行動を処理（並列実行）
    async def process_llm_seer(seer: Player) -> None:
        other_players = [p for p in game.player_list if p.user_id != seer.user_id]
        action_type, target_id = await llm_seer_action(game, seer, other_players)
        
        if action_type == "center":
            process_seer_action(game, seer.user_id, view_center=True)
        elif target_id is not None:
            process_seer_action(game, seer.user_id, target_player_id=target_id)
    
    llm_tasks = [process_llm_seer(seer) for seer in llm_seers]
    
    # 人間とLLMの処理を並列実行
    if human_seers:
        await asyncio.gather(
            wait_for_seer_actions(game, human_seers),
            *llm_tasks
        )
    elif llm_tasks:
        await asyncio.gather(*llm_tasks)
    
    advance_night_phase(game)


async def wait_for_seer_actions(game: GameState, seers: list[Player]) -> None:
    """占い師の行動入力を待つ（全員が行動するまで待機）。"""
    
    def check(message: discord.Message) -> bool:
        if message.guild is not None:  # DMのみ
            return False
        if message.author.id not in [s.user_id for s in seers]:
            return False
        player = game.get_player(message.author.id)
        if player is None or player.has_acted:
            return False
        return message.content.startswith("!seer")
    
    pending_seers = {s.user_id for s in seers}
    
    while pending_seers:
        try:
            message = await bot.wait_for("message", check=check)
        except asyncio.CancelledError:
            break
        
        seer = game.get_player(message.author.id)
        if seer is None:
            continue
        
        parts = message.content.split()
        if len(parts) < 2:
            await message.channel.send("⚠️ 無効なコマンドです。`!seer player 名前` または `!seer center` を使用してください。")
            continue
        
        action = parts[1].lower()
        
        if action == "center":
            result = process_seer_action(game, seer.user_id, view_center=True)
            if result:
                await message.channel.send(result)
                pending_seers.discard(seer.user_id)
            else:
                await message.channel.send("⚠️ 行動に失敗しました。")
        
        elif action == "player":
            if len(parts) < 3:
                await message.channel.send("⚠️ プレイヤー名を指定してください。")
                continue
            
            target_name = " ".join(parts[2:])
            target = None
            for p in game.player_list:
                if p.username.lower() == target_name.lower() or target_name.lower() in p.username.lower():
                    target = p
                    break
            
            if target is None:
                await message.channel.send(f"⚠️ プレイヤー '{target_name}' が見つかりません。")
                continue
            
            if target.user_id == seer.user_id:
                await message.channel.send("⚠️ 自分自身は占えません。")
                continue
            
            result = process_seer_action(game, seer.user_id, target_player_id=target.user_id)
            if result:
                await message.channel.send(result)
                pending_seers.discard(seer.user_id)
            else:
                await message.channel.send("⚠️ 行動に失敗しました。")
        
        else:
            await message.channel.send("⚠️ 無効なコマンドです。`!seer player 名前` または `!seer center` を使用してください。")


async def process_thieves(channel: discord.abc.Messageable, game: GameState) -> None:
    """怪盗の夜行動を処理する。"""
    thieves = game.get_players_by_initial_role(Role.THIEF)
    
    if not thieves:
        advance_night_phase(game)
        return
    
    # LLMプレイヤーと人間プレイヤーを分離
    human_thieves = [t for t in thieves if not t.is_llm]
    llm_thieves = [t for t in thieves if t.is_llm]
    
    # 人間プレイヤーにDMを送信
    for thief in human_thieves:
        user = bot.get_user(thief.user_id)
        if user is None:
            try:
                user = await bot.fetch_user(thief.user_id)
            except discord.NotFound:
                continue
        
        try:
            other_players = [
                p for p in game.player_list 
                if p.user_id != thief.user_id
            ]
            
            await user.send(
                f"🦹 **怪盗の行動**\n\n"
                f"以下のいずれかのコマンドをこのDMで入力してください：\n\n"
                f"**カードを交換する場合:**\n"
                f"`!thief プレイヤー名`\n"
                f"（対象プレイヤー: {', '.join(p.username for p in other_players)}）\n\n"
                f"**何もしない場合:**\n"
                f"`!thief skip`"
            )
        except discord.Forbidden:
            pass
    
    # LLMプレイヤーの行動を処理
    async def process_llm_thief(thief: Player) -> None:
        other_players = [p for p in game.player_list if p.user_id != thief.user_id]
        target_id = await llm_thief_action(game, thief, other_players)
        process_thief_action(game, thief.user_id, target_id=target_id)
    
    llm_tasks = [process_llm_thief(thief) for thief in llm_thieves]
    
    # 人間とLLMの処理を並列実行
    if human_thieves:
        await asyncio.gather(
            wait_for_thief_actions(game, human_thieves),
            *llm_tasks
        )
    elif llm_tasks:
        await asyncio.gather(*llm_tasks)
    
    advance_night_phase(game)


async def wait_for_thief_actions(game: GameState, thieves: list[Player]) -> None:
    """怪盗の行動入力を待つ（全員が行動するまで待機）。"""
    
    def check(message: discord.Message) -> bool:
        if message.guild is not None:
            return False
        if message.author.id not in [t.user_id for t in thieves]:
            return False
        player = game.get_player(message.author.id)
        if player is None or player.has_acted:
            return False
        return message.content.startswith("!thief")
    
    pending_thieves = {t.user_id for t in thieves}
    
    while pending_thieves:
        try:
            message = await bot.wait_for("message", check=check)
        except asyncio.CancelledError:
            break
        
        thief = game.get_player(message.author.id)
        if thief is None:
            continue
        
        parts = message.content.split()
        if len(parts) < 2:
            await message.channel.send("⚠️ 無効なコマンドです。`!thief プレイヤー名` または `!thief skip` を使用してください。")
            continue
        
        action = parts[1].lower()
        
        if action == "skip":
            process_thief_action(game, thief.user_id, target_id=None)
            await message.channel.send("🦹 何もしませんでした。あなたの役職は **怪盗** のままです。")
            pending_thieves.discard(thief.user_id)
        
        else:
            target_name = " ".join(parts[1:])
            target = None
            for p in game.player_list:
                if p.username.lower() == target_name.lower() or target_name.lower() in p.username.lower():
                    target = p
                    break
            
            if target is None:
                await message.channel.send(f"⚠️ プレイヤー '{target_name}' が見つかりません。")
                continue
            
            if target.user_id == thief.user_id:
                await message.channel.send("⚠️ 自分自身とは交換できません。")
                continue
            
            new_role = process_thief_action(game, thief.user_id, target_id=target.user_id)
            if new_role:
                await message.channel.send(
                    f"🦹 {target.username} とカードを交換しました！\n"
                    f"あなたの新しい役職は **{new_role.value}** です。"
                )
                pending_thieves.discard(thief.user_id)
            else:
                await message.channel.send("⚠️ 行動に失敗しました。")


async def process_hunters(channel: discord.abc.Messageable, game: GameState) -> None:
    """狩人の夜行動を処理する。"""
    # 狩人は夜フェーズでの行動なし（役職通知は begin コマンドで済み）
    # 道連れ選択は処刑時に行う（process_hunter_revenge）
    advance_night_phase(game)


# =============================================================================
# 昼フェーズ処理
# =============================================================================

async def start_day_phase(channel: discord.abc.Messageable, game: GameState) -> None:
    """昼フェーズ（議論）を開始する。"""
    game.phase = GamePhase.DISCUSSION

    await channel.send(
        f"☀️ **朝になりました！**\n\n"
        f"これから {DISCUSSION_TIME}秒間 、自由に議論してください。\n"
        f"誰が人狼か、話し合いましょう！\n\n"
        f"議論終了後、投票フェーズに移ります。"
    )

    # LLMプレイヤーの初回発言（順番に1回ずつ）と自動発言ループを開始
    llm_players = game.get_llm_players()
    if llm_players:
        asyncio.create_task(initial_then_auto_speak(channel, game))

    # 議論時間を待つ
    await asyncio.sleep(DISCUSSION_TIME)

    # 投票フェーズへ
    await start_voting_phase(channel, game)


async def start_voting_phase(channel: discord.abc.Messageable, game: GameState) -> None:
    """投票フェーズを開始する。"""
    game.phase = GamePhase.VOTING
    
    player_list = "\n".join(f"• {p.username}" for p in game.player_list)
    
    await channel.send(
        f"🗳️ **投票フェーズです！**\n\n"
        f"`/onj vote` で投票してください。\n"
        f"投票先で「平和村」を選ぶと誰も処刑しない投票ができます。\n"
        f"※自分以外のプレイヤーに投票できます。\n\n"
        f"**参加者:**\n{player_list}\n\n"
        f"全員の投票が完了すると結果が発表されます。"
    )
    
    # LLMプレイヤーの自動投票（少し遅延を入れてから投票）
    llm_players = game.get_llm_players()
    if llm_players:
        asyncio.create_task(process_llm_votes(channel, game, llm_players))


async def process_llm_votes(
    channel: discord.abc.Messageable,
    game: GameState,
    llm_players: list[Player]
) -> None:
    """LLMプレイヤーの投票を処理する。"""
    # 自然な遅延（議論を見ているような演出）
    await asyncio.sleep(3)
    
    for player in llm_players:
        if game.phase != GamePhase.VOTING:
            break
        
        if player.vote_target_id is not None:
            continue  # 既に投票済み
        
        # 他のプレイヤー（自分以外）
        other_players = [p for p in game.player_list if p.user_id != player.user_id]
        
        # LLMに投票先を決定させる
        target_id = await llm_vote(game, player, other_players)
        
        # 投票を登録
        emoji = player.emoji or "🤖"
        if target_id == -1:
            player.vote_target_id = -1
            await channel.send(
                f"{emoji} {player.username} が投票しました。"
                f"（{game.voted_count()}/{game.player_count}）"
            )
        else:
            if register_vote(game, player.user_id, target_id):
                await channel.send(
                    f"{emoji} {player.username} が投票しました。"
                    f"（{game.voted_count()}/{game.player_count}）"
                )
        
        # 全員投票完了したら結果発表
        if game.all_voted():
            await end_voting_phase(channel, game)
            break
        
        # 次のLLMプレイヤーの投票前に少し待つ
        await asyncio.sleep(1)


async def process_hunter_revenge(
    channel: discord.abc.Messageable,
    game: GameState,
    executed_hunters: list[Player]
) -> None:
    """
    処刑された狩人の道連れ処理を行う。

    Args:
        channel: 送信先チャンネル
        game: ゲーム状態
        executed_hunters: 処刑される狩人のリスト
    """
    import random

    for hunter in executed_hunters:
        # 道連れ対象候補（自分以外のプレイヤー）
        candidates = [p for p in game.player_list if p.user_id != hunter.user_id]
        if not candidates:
            continue

        candidate_names = ", ".join(p.username for p in candidates)

        await channel.send(
            f"🏹 **{hunter.username}** が処刑されます！\n\n"
            f"狩人の能力で、誰かを道連れにできます。\n"
            f"対象候補: {candidate_names}"
        )

        if hunter.is_llm:
            # LLMプレイヤーの場合は自動決定
            await asyncio.sleep(2)  # 自然な遅延
            target = await llm_hunter_revenge(game, hunter, candidates)
            if target:
                add_hunter_target_to_execution(game, target.user_id)
                await channel.send(
                    f"🏹 **{hunter.username}** は **{target.username}** を道連れに選びました！"
                )
            else:
                await channel.send(
                    f"🏹 **{hunter.username}** は道連れを選びませんでした。"
                )
        else:
            # 人間プレイヤーの場合はDMで選択
            user = bot.get_user(hunter.user_id)
            if user is None:
                # ユーザーが見つからない場合はスキップ
                await channel.send(
                    f"⚠️ {hunter.username} のユーザーが見つかりません。道連れはスキップされます。"
                )
                continue

            try:
                dm_channel = await user.create_dm()
                candidate_list = "\n".join(f"• {p.username}" for p in candidates)
                await dm_channel.send(
                    f"🏹 **あなたは処刑されます！**\n\n"
                    f"狩人の能力で、誰かを道連れにできます。\n\n"
                    f"**対象候補:**\n{candidate_list}\n\n"
                    f"`!hunter <プレイヤー名>` で道連れを指名\n"
                    f"`!hunter skip` でスキップ"
                )

                # 返答を待つ
                def check(m: discord.Message) -> bool:
                    return (
                        m.author.id == hunter.user_id
                        and m.channel == dm_channel
                        and m.content.startswith("!hunter")
                    )

                response = await bot.wait_for(
                    "message",
                    check=check
                )

                content = response.content.lower()
                if "skip" in content:
                    await dm_channel.send("道連れをスキップしました。")
                    await channel.send(
                        f"🏹 **{hunter.username}** は道連れを選びませんでした。"
                    )
                else:
                    # プレイヤー名を探す
                    target = None
                    for p in candidates:
                        if p.username.lower() in response.content.lower():
                            target = p
                            break

                    if target:
                        add_hunter_target_to_execution(game, target.user_id)
                        await dm_channel.send(f"**{target.username}** を道連れにしました！")
                        await channel.send(
                            f"🏹 **{hunter.username}** は **{target.username}** を道連れに選びました！"
                        )
                    else:
                        await dm_channel.send(
                            "⚠️ プレイヤー名が見つかりませんでした。道連れはスキップされます。"
                        )
                        await channel.send(
                            f"🏹 **{hunter.username}** は道連れを選びませんでした。"
                        )

            except discord.Forbidden:
                # DMが送れない場合はスキップ
                await channel.send(
                    f"⚠️ {hunter.username} にDMを送れません。道連れはスキップされます。"
                )


async def llm_hunter_revenge(
    game: GameState,
    hunter: Player,
    candidates: list[Player]
) -> Optional[Player]:
    """
    LLM狩人が道連れ対象を決定する。
    議論内容や夜の情報を元に、最も人狼か大狼だと思うプレイヤーを選ぶ。

    Returns:
        道連れ対象（基本的に必ず誰かを選ぶ）
    """
    # 処刑時用の専用関数を使用（議論履歴・夜の情報を考慮）
    target_id = await llm_hunter_revenge_action(game, hunter, candidates)

    for p in candidates:
        if p.user_id == target_id:
            return p

    return None


async def end_voting_phase(channel: discord.abc.Messageable, game: GameState) -> None:
    """投票フェーズを終了し、結果を発表する。"""
    if game.phase == GamePhase.ENDED:
        return  # 既に終了している
    
    game.phase = GamePhase.ENDED
    
    # 投票結果を集計
    vote_counts = calculate_votes(game)
    
    # 誰が誰に投票したかを表示
    vote_detail_lines = []
    for player in game.player_list:
        target_id = player.vote_target_id
        if target_id == -1:
            vote_detail_lines.append(f"• {player.username} → 🕊️ 平和村")
        elif target_id is not None:
            target = game.get_player(target_id)
            if target:
                vote_detail_lines.append(f"• {player.username} → {target.username}")
            else:
                vote_detail_lines.append(f"• {player.username} → ???")
        else:
            vote_detail_lines.append(f"• {player.username} → （未投票）")
    
    vote_details = "\n".join(vote_detail_lines)
    
    # 得票数の表示
    vote_summary_lines = []
    for player in game.player_list:
        count = vote_counts.get(player.user_id, 0)
        if count > 0:
            vote_summary_lines.append(f"• {player.username}: {count}票")
    
    # 平和村への投票を表示
    peace_votes = vote_counts.get(-1, 0)
    if peace_votes > 0:
        vote_summary_lines.append(f"• 🕊️ 平和村（処刑なし）: {peace_votes}票")
    
    vote_summary = "\n".join(vote_summary_lines) if vote_summary_lines else "（投票なし）"

    # 処刑対象を決定（狩人の道連れは含まない）
    executed = determine_execution(game)

    # 狩人が処刑対象に含まれている場合、道連れ処理（投票結果表示前に発動）
    executed_hunters = get_executed_hunters(game)
    if executed_hunters:
        await process_hunter_revenge(channel, game, executed_hunters)

    # 投票結果を表示（道連れ処理の後）
    await channel.send(
        f"📊 **投票結果**\n\n"
        f"**【投票内容】**\n{vote_details}\n\n"
        f"**【得票数】**\n{vote_summary}"
    )

    # 処刑結果を表示
    await channel.send(get_execution_message(game))

    # 勝敗を判定
    determine_winner(game)
    
    # 勝者を発表
    await channel.send(get_winner_message(game))
    
    # 最終役職を公開
    await channel.send(
        f"\n📋 **最終役職一覧**\n\n{get_final_roles_message(game)}"
    )
    
    # ゲームをリセット（参加者は保持）
    reset_game_keep_players(game)
    
    player_names = ", ".join(p.username for p in game.player_list)
    await channel.send(
        f"\n🎮 **ゲームが終了しました！**\n\n"
        f"**現在の参加者（{game.player_count}人）**: {player_names}\n\n"
        f"• `/onj begin` - 同じメンバーで再戦\n"
        f"• `/onj roles` - 役職構成を変更\n"
        f"• `/onj join` / `/onj leave` - 参加者を変更\n"
        f"• `/onj cancel` - 募集を終了"
    )


# =============================================================================
# イベントハンドラ
# =============================================================================

@bot.event
async def on_ready() -> None:
    """Bot起動時の処理。"""
    print(f"ワンナイト人狼Bot がログインしました: {bot.user}")
    
    # Botのステータスを設定
    activity = discord.Activity(
        type=discord.ActivityType.playing,
        name="/onj help でヘルプ表示"
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)
    
    # スラッシュコマンドを同期
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            
            # ギルドのコマンドを一度クリアしてから再登録
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"ギルド {GUILD_ID} にコマンドを同期しました: {len(synced)}個")
            
            # グローバルコマンドをクリア（重複防止）
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print("グローバルコマンドをクリアしました")
        else:
            # グローバルに同期（反映に最大1時間かかる）
            synced = await bot.tree.sync()
            print(f"グローバルにコマンドを同期しました: {len(synced)}個")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    """コマンドエラーのハンドラ。"""
    # !seer, !thief, !hunter などはwait_forで処理するため、CommandNotFoundは無視
    if isinstance(error, commands.CommandNotFound):
        return
    # その他のエラーは再送出
    raise error


@bot.event
async def on_message(message: discord.Message) -> None:
    """メッセージ受信時の処理（プレフィックスコマンド用＆LLM議論）。"""
    if message.author.bot:
        return
    
    # 議論フェーズ中のLLMプレイヤー発言処理
    if message.guild is not None and message.channel is not None:
        channel_id = message.channel.id
        game = get_game(channel_id)
        
        if game is not None and game.phase == GamePhase.DISCUSSION:
            # ゲーム参加者からのメッセージか確認
            sender = game.get_player(message.author.id)
            if sender is not None and not sender.is_llm:
                # 議論履歴に追加
                game.add_discussion_message(sender.username, message.content)

                # 名指しされたLLMプレイヤーがいれば発言させる
                mentioned_llm = find_mentioned_llm(game, message.content)
                if mentioned_llm:
                    asyncio.create_task(
                        trigger_llm_discussion_for_player(
                            message.channel, game, message.content, mentioned_llm
                        )
                    )
    
    await bot.process_commands(message)


# 最後にLLMが発言した時間を記録（連続発言防止）
_last_llm_speak_time: dict[int, float] = {}
# 次に発言するLLMプレイヤーのインデックス（自動発言ループ用）
_next_llm_speaker_index: dict[int, int] = {}
# 自発的発言の間隔（秒）
AUTO_SPEAK_INTERVAL = 10


def find_mentioned_llm(game: GameState, content: str) -> Optional[Player]:
    """メッセージ内で名指しされたLLMプレイヤーを検出する。"""
    llm_players = game.get_llm_players()
    for player in llm_players:
        if player.username in content:
            return player
    return None


async def initial_llm_statements(
    channel: discord.abc.Messageable,
    game: GameState
) -> None:
    """議論開始時に全LLMプレイヤーが順番に1回ずつ発言する。"""
    import time
    import random

    llm_players = game.get_llm_players()
    if not llm_players:
        return

    for speaker in llm_players:
        # 議論フェーズでない場合は中断
        if game.phase != GamePhase.DISCUSSION:
            break

        other_players = [p for p in game.player_list if p.user_id != speaker.user_id]

        # 自然な遅延（2〜4秒）
        await asyncio.sleep(random.uniform(2, 4))

        # まだ議論フェーズか確認
        if game.phase != GamePhase.DISCUSSION:
            break

        try:
            response = await llm_generate_discussion_message(game, speaker, other_players, "")
        except Exception as e:
            print(f"LLM初回発言エラー ({speaker.username}): {e}")
            continue

        if response and game.phase == GamePhase.DISCUSSION:
            _last_llm_speak_time[game.channel_id] = time.time()
            game.add_discussion_message(speaker.username, response)
            speaker.my_statements.append(response)
            emoji = speaker.emoji or "🤖"
            await channel.send(f"{emoji} **{speaker.username}**: {response}")


async def initial_then_auto_speak(
    channel: discord.abc.Messageable,
    game: GameState
) -> None:
    """初回発言を実行し、完了後に自動発言ループを開始する。"""
    await initial_llm_statements(channel, game)
    await auto_llm_speak_loop(channel, game)


async def auto_llm_speak_loop(
    channel: discord.abc.Messageable,
    game: GameState
) -> None:
    """一定間隔でLLMプレイヤーに自発的に発言させる。"""
    import time
    import random

    while game.phase == GamePhase.DISCUSSION:
        # 最後の発言からの経過時間をチェック
        current_time = time.time()
        last_time = _last_llm_speak_time.get(game.channel_id, 0)

        # 人間の発言があった直後はスキップ（重複防止）
        if current_time - last_time < 5:
            await asyncio.sleep(5)
            continue

        llm_players = game.get_llm_players()
        if not llm_players:
            break

        # 順番にLLMプレイヤーを選択
        current_index = _next_llm_speaker_index.get(game.channel_id, 0)
        if current_index >= len(llm_players):
            current_index = 0
        speaker = llm_players[current_index]
        _next_llm_speaker_index[game.channel_id] = (current_index + 1) % len(llm_players)
        other_players = [p for p in game.player_list if p.user_id != speaker.user_id]

        # 自然な遅延
        await asyncio.sleep(random.uniform(1, 3))

        # まだ議論フェーズか確認
        if game.phase != GamePhase.DISCUSSION:
            break

        # LLMに発言を生成させる
        try:
            response = await llm_generate_discussion_message(game, speaker, other_players, "")
        except Exception as e:
            print(f"LLM自発的発言エラー ({speaker.username}): {e}")
            await asyncio.sleep(AUTO_SPEAK_INTERVAL)
            continue

        if response and game.phase == GamePhase.DISCUSSION:
            _last_llm_speak_time[game.channel_id] = time.time()

            # 議論履歴に追加
            game.add_discussion_message(speaker.username, response)

            # 自分の発言履歴に追加
            speaker.my_statements.append(response)

            emoji = speaker.emoji or "🤖"
            await channel.send(f"{emoji} **{speaker.username}**: {response}")

        # 次の自発的発言まで待つ
        await asyncio.sleep(AUTO_SPEAK_INTERVAL)


async def trigger_llm_discussion_for_player(
    channel: discord.abc.Messageable,
    game: GameState,
    context: str,
    speaker: Player
) -> None:
    """特定のLLMプレイヤーに議論で発言させる（名指しされた場合）。"""
    import time
    import random

    # 連続発言を防ぐため、最低3秒間隔を空ける
    current_time = time.time()
    last_time = _last_llm_speak_time.get(game.channel_id, 0)
    if current_time - last_time < 3:
        return

    # 議論フェーズでない場合は何もしない
    if game.phase != GamePhase.DISCUSSION:
        return

    # 他のプレイヤー
    other_players = [p for p in game.player_list if p.user_id != speaker.user_id]

    # 少し待ってから発言（自然な遅延）
    await asyncio.sleep(random.uniform(2, 4))

    # まだ議論フェーズか確認
    if game.phase != GamePhase.DISCUSSION:
        return

    # LLMに発言を生成させる
    try:
        response = await llm_generate_discussion_message(game, speaker, other_players, context)
    except Exception as e:
        print(f"LLM議論発言エラー ({speaker.username}): {e}")
        return  # 静かに失敗（ゲーム継続）

    if response and game.phase == GamePhase.DISCUSSION:
        _last_llm_speak_time[game.channel_id] = time.time()

        # 議論履歴に追加
        game.add_discussion_message(speaker.username, response)

        # 自分の発言履歴に追加
        speaker.my_statements.append(response)

        emoji = speaker.emoji or "🤖"
        await channel.send(f"{emoji} **{speaker.username}**: {response}")


# =============================================================================
# メイン
# =============================================================================

def main() -> None:
    """Botを起動する。"""
    bot.run(TOKEN)


if __name__ == "__main__":
    main()

