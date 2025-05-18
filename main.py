import os

import discord
import dotenv
from discord.ext import commands

dotenv.load_dotenv()

bot = commands.Bot([], intents=discord.Intents.default())


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.novel")
    await bot.tree.sync()


bot.run(os.getenv("discord"))
