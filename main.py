import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import logging

discord.utils.setup_logging(level=logging.DEBUG)

intents=discord.Intents().default()
intents.message_content=True
bot=commands.Bot(intents=intents, command_prefix=commands.when_mentioned_or("*"))

@bot.event
async def on_message(msg:discord.Message):
    msg.content=="sussy" and await msg.channel.send("baka")
    await bot.process_commands(msg)

@bot.event
async def on_ready():
    print("i'm in "+", ".join(x.name for x in bot.guilds))

@bot.command()
@commands.is_owner()
async def reload(ctx:commands.Context, x:str): await bot.reload_extension(x); await ctx.send("ok")

@bot.command()
@commands.is_owner()
async def unload(ctx:commands.Context, x:str): await bot.unload_extension(x); await ctx.send("ok")

@bot.command()
@commands.is_owner()
async def load  (ctx:commands.Context, x:str): await bot.load_extension(x); await ctx.send("ok")

@bot.command()
@commands.is_owner()
async def sql(ctx:commands.Context, *, query:str):
    await ctx.send(str(await bot.db.execute_fetchall(query)))

@bot.command()
@commands.is_owner()
async def sync(ctx:commands.Context):
    await bot.tree.sync()

async def do():
    bot.db=aiosqlite.connect("bees.db")
    async with bot, bot.db:
        await bot.load_extension("starboard")
        with open("token") as f: tok=f.read().strip()
        await bot.start(tok)

asyncio.run(do())
