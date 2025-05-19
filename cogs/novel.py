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

SYSTEM_INSTRUCT = """あなたは小説家です。以下の条件に従って小説を書いてください。
* 最初に提示された内容（別途与えます）を小説の設定として使用してください。
* 内容にはエロティック（性的描写）およびグロテスク（暴力や残酷描写）な要素が含まれても構いません。
* 小説は一度に「1ページ分」だけ出力してください。ただし、1ページはできるだけ長く、充実した内容にしてください。
* 私が「次のページ」と指示するまで、続きは書かないでください。
* 小説が完結した際は、最後に「(終わり)」とだけ書いてください。
小説の本文以外のメタ的な説明やコメントは不要です。小説本文のみ出力してください。"""


def random_id(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def split_by_chunk(s: str, chunk_size: int = 2048) -> List[str]:
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


def trim_page_text(text: str) -> str:
    return text.rstrip("\n(次のページ)")


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
            history.append(text)

            if "(終わり" in text:
                finished = True
            else:
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
                "これ以上ページはありません。", ephemeral=True
            )
            return

        text = history[page]
        chunks = split_by_chunk(text)

        total_pages = len(history)
        current_page_number = page + 1

        if not finished and page >= total_pages:
            display_total = current_page_number
        else:
            display_total = total_pages

        embeds = [
            discord.Embed(
                title=f"{'[完結済み]' if finished else ''} {story}",
                description=chunk,
                color=discord.Color.blurple(),
            )
            .set_author(name=f"Novel ID: {novel_id}")
            .set_footer(
                text=f"ページ {current_page_number} / {display_total} (Part {i + 1}/{len(chunks)})"
            )
            for i, chunk in enumerate(chunks)
        ]

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

        await interaction.edit_original_response(embeds=embeds, view=view)

    @app_commands.command(name="new", description="新しい小説を作成します。")
    @app_commands.rename(story="ストーリー")
    @app_commands.describe(
        story="小説のストーリー。できるだけ長く書くことを推奨します。"
    )
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
        data = [text]

        novel_id = random_id(12)
        chunks = split_by_chunk(text)

        embeds = [
            discord.Embed(
                title=f"ページ 1 / 1 (Part {i + 1}/{len(chunks)})",
                description=chunk,
                color=discord.Color.blurple(),
            )
            .set_author(name=novel_id)
            .set_footer(text=story)
            for i, chunk in enumerate(chunks)
        ]

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(emoji="⬅️", custom_id=f"prev:{novel_id}:0", disabled=True)
        )
        view.add_item(discord.ui.Button(emoji="➡️", custom_id=f"next:{novel_id}:0"))

        await interaction.followup.send(embeds=embeds, view=view)

        await self.pool.execute(
            "INSERT INTO novels (id, data, owner, story, finished) VALUES ($1, $2, $3, $4, $5)",
            novel_id,
            data,
            interaction.user.id,
            story,
            "(終わり)" in text,
        )

    async def callAutoComplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        rows = await self.pool.fetch(
            "SELECT * FROM novels WHERE owner = $1", interaction.user.id
        )
        return [
            app_commands.Choice(
                name=f"{row['story'][0:60]} (ID: {row['id']})", value=current
            )
            for row in rows
            if row["name"].startswith(current)
        ]

    @app_commands.command(
        name="call", description="小説共有コードから小説を読み込みます。"
    )
    @app_commands.rename(novel_id="小説id")
    @app_commands.describe(novel_id="埋め込みから確認できる12桁の英数字。")
    @app_commands.autocomplete(novel_id=callAutoComplete)
    async def call_novel(self, interaction: discord.Interaction, novel_id: str):
        await interaction.response.defer()
        if len(novel_id) > 12:
            novel_id = novel_id.split("ID: ")[1].replace(")", "")

        row = await self.pool.fetchrow("SELECT * FROM novels WHERE id = $1", novel_id)
        if not row:
            await interaction.followup.send("小説が見つかりません。", ephemeral=True)
            return

        chunks = split_by_chunk(row["data"][0])

        embeds = [
            discord.Embed(
                title=f"ページ 1 / {len(row['data'])} (Part {i + 1}/{len(chunks)})",
                description=chunk,
                color=discord.Color.blurple(),
            )
            .set_author(name=novel_id)
            .set_footer(text=row["story"])
            for i, chunk in enumerate(chunks)
        ]

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(emoji="⬅️", custom_id=f"prev:{novel_id}:0", disabled=True)
        )
        view.add_item(discord.ui.Button(emoji="➡️", custom_id=f"next:{novel_id}:0"))

        await interaction.followup.send(embeds=embeds, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Novel(bot))
