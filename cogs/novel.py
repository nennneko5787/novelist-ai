import asyncio
import os
import random
import string
from typing import List

import asyncpg
import discord
import dotenv
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types

dotenv.load_dotenv()

SAFETYSETTINGS = [
    types.SafetySetting(category=c, threshold="BLOCK_NONE")
    for c in [
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_HARASSMENT",
    ]
]

SYSTEM_INSTRUCT = """あなたは小説家です。最初に示されたことを設定として、小説を書いていきます。
エロもグロも書きます。
小説の内容だけを出力してください。
「次のページ」と示されたら、次のページを書きます。それまでは次のページを書かないでください。
終わりの際は、絶対に「(終わり)」とだけ出力してください。できるだけ長いページでお願いします。"""


def random_id(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def split_by_chunk(s: str, chunk_size: int = 4096) -> List[str]:
    chunk_size -= 1
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


def trim_page_text(text: str) -> str:
    return text.rstrip("\n次のページ")


class Novel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.genai = genai.Client(api_key=os.getenv("gemini"))
        self.pool: asyncpg.Pool = None
        self.in_page: set[str] = set()

    async def cog_load(self):
        self.pool = await asyncpg.create_pool(os.getenv("dsn"), statement_cache_size=0)
        return await super().cog_load()

    async def cog_unload(self):
        async with asyncio.timeout(10.0):
            await self.pool.close()
        return await super().cog_unload()

    @commands.Cog.listener("on_interaction")
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id")
        if not custom_id:
            return

        try:
            direction, novel_id, raw_page = custom_id.split(":")
            page = int(raw_page)
        except Exception:
            return

        await interaction.response.defer()

        if direction == "prev":
            page -= 1
        elif direction == "next":
            page += 1

        row = await self.pool.fetchrow("SELECT * FROM novels WHERE id = $1", novel_id)
        if not row:
            await interaction.followup.send("小説が見つかりません。", ephemeral=True)
            return

        history: List[str] = row["data"]
        finished: bool = row["finished"]
        story: str = row["story"]

        # これ以上生成できない場合
        if page == len(history) and not finished:
            if novel_id in self.in_page:
                await interaction.followup.send("現在生成中です...", ephemeral=True)
                return

            self.in_page.add(novel_id)

            chat = self.genai.aio.chats.create(
                model="gemini-2.0-flash",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCT,
                    safety_settings=SAFETYSETTINGS,
                ),
            )

            for count, text in enumerate(history):
                prompt = story if count == 0 else "次のページ"
                chat.record_history(
                    user_input=types.Content(
                        parts=[types.Part(text=prompt)], role="user"
                    ),
                    model_output=[
                        types.Content(parts=[types.Part(text=text)], role="model")
                    ],
                    automatic_function_calling_history=[],
                    is_valid=True,
                )

            content = await chat.send_message("次のページ")
            text = trim_page_text(content.text)

            if "(終わり" in text:
                finished = True
            else:
                history += split_by_chunk(text)
                finished = False

            await self.pool.execute(
                "UPDATE novels SET data = $1, finished = $2 WHERE id = $3",
                history,
                finished,
                novel_id,
            )

            self.in_page.remove(novel_id)

        elif page >= len(history):
            await interaction.followup.send(
                f"これ以上ページはありません。", ephemeral=True
            )
            return

        # 正常にページ表示
        text = history[page]

        total_pages = len(history)
        current_page_number = page + 1

        # ページ数の表示は finished か否かで変化させる
        if not finished and page >= total_pages:
            display_total = current_page_number  # 未生成ページは表示しない
        else:
            display_total = total_pages

        embed = (
            discord.Embed(
                title=f"ページ {current_page_number} / {display_total}",
                description=text,
                color=discord.Color.blurple(),
            )
            .set_author(name=novel_id)
            .set_footer(text=story)
        )

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                emoji="⬅️", custom_id=f"prev:{novel_id}:{page}", disabled=page <= 0
            )
        )
        view.add_item(
            discord.ui.Button(
                emoji="➡️",
                custom_id=f"next:{novel_id}:{page}",
                disabled=page >= len(history) - 1 and finished,
            )
        )

        await interaction.edit_original_response(embed=embed, view=view)

    @app_commands.command(name="new", description="新しい小説を作成します。")
    async def new_novel(self, interaction: discord.Interaction, story: str):
        await interaction.response.defer()

        chat = self.genai.aio.chats.create(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCT,
                safety_settings=SAFETYSETTINGS,
            ),
        )

        content = await chat.send_message(story)
        text = trim_page_text(content.text)
        data = split_by_chunk(text)

        novel_id = random_id(12)

        embed = (
            discord.Embed(
                title="ページ 1 / 1",
                description=data[0],
                color=discord.Color.blurple(),
            )
            .set_author(name=novel_id)
            .set_footer(text=story)
        )

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(emoji="⬅️", custom_id=f"prev:{novel_id}:0", disabled=True)
        )
        view.add_item(discord.ui.Button(emoji="➡️", custom_id=f"next:{novel_id}:0"))

        await interaction.followup.send(embed=embed, view=view)

        await self.pool.execute(
            "INSERT INTO novels (id, data, owner, story, finished) VALUES ($1, $2, $3, $4, $5)",
            novel_id,
            data,
            interaction.user.id,
            story,
            "(終わり)" in text,
        )

    @app_commands.command(
        name="call", description="小説共有コードから小説を読み込みます。"
    )
    async def call_novel(self, interaction: discord.Interaction, novel_id: str):
        await interaction.response.defer()

        row = await self.pool.fetchrow("SELECT * FROM novels WHERE id = $1", novel_id)

        embed = (
            discord.Embed(
                title=f"ページ 1 / {len(row["data"])}",
                description=row["data"][0],
                color=discord.Color.blurple(),
            )
            .set_author(name=novel_id)
            .set_footer(text=row["story"])
        )

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(emoji="⬅️", custom_id=f"prev:{novel_id}:0", disabled=True)
        )
        view.add_item(discord.ui.Button(emoji="➡️", custom_id=f"next:{novel_id}:0"))

        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Novel(bot))
