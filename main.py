import discord, discord.app_commands as app_commands
import aiosqlite
import asyncio, logging

intents=discord.Intents().default()
intents.message_content=True

discord.utils.setup_logging(level=logging.DEBUG)

bot=discord.Client(intents=intents)
tree=app_commands.CommandTree(bot)

def ephemeral(c,*args,**kwargs): return c.response.send_message(*args,ephemeral=True,**kwargs)

@tree.command(description="change configuration like starboard channel or min stars")
@app_commands.rename(channel="starboard-channel",minimum="minimum-star-count")
@app_commands.default_permissions(manage_channels=True)
async def starconfig(c:discord.Interaction, channel:discord.TextChannel|None=None, minimum:int|None=None):
    if channel is None and minimum is None:
        await bot.db.execute("DELETE FROM guilds WHERE id=?", (c.guild_id,))
        await ephemeral(c,"unconfigued")
    else:
        if channel is not None:
            if channel.guild != c.guild: return await ephemeral(c,"eat bricks")
            await bot.db.execute("INSERT OR REPLACE INTO guilds(id,channel) VALUES(?,?)", (c.guild_id, channel.id))
            await ephemeral(c,"ok")

        if minimum is not None:
            if minimum < 1: return await ephemeral(c,"eat bricks")
            cur = await bot.db.execute("UPDATE guilds SET minimum=? WHERE id=?", (minimum, c.guild_id))
            if cur.rowcount==0:
                bot.db.rollback()
                return ephemeral(c, "no channel set")

    await ephemeral(c, "ok")
    await bot.db.commit()

@bot.event
async def on_message(msg:discord.Message):
    msg.content=="sussy" and await msg.channel.send("baka")
    msg.content=="sync"  and await tree.sync(guild=bot.guilds[0])
    if msg.content.startswith("sql ") and msg.author == bot.owner:
        await msg.channel.send(str(await bot.db.execute_fetchall(msg.content[4:])))

@bot.event
async def on_ready():
    print("i'm in "+", ".join(x.name for x in bot.guilds))

@bot.event
async def on_raw_reaction_add(ev:discord.RawReactionActionEvent):
    if ev.emoji.name != "⭐": return
    await bot.db.execute("INSERT INTO stars(starrer,starred,guild) VALUES(?,?,?)",
                         (ev.user_id, ev.message_id, ev.guild_id))
    minimum,channel = await (await bot.db.execute("SELECT minimum,channel FROM guilds WHERE id=?", (ev.guild_id,))).fetchone()
    (count,) = await (await bot.db.execute("SELECT count(starrer) FROM stars WHERE starred=?", (ev.message_id,))).fetchone()
    if count==minimum:
        original = await bot.get_channel(ev.channel_id).fetch_message(ev.message_id)
        starboard = await bot.get_channel(channel).send(
            embed=discord.Embed(colour=discord.Color.yellow(), description=original.content)
                .set_author(name=original.author.display_name, icon_url=original.author.display_avatar.url),
            content=original.jump_url
        )
        await bot.db.execute("INSERT OR REPLACE INTO messages(original,starboard,guild) VALUES(?,?,?)",
                             (original.id, starboard.id, ev.guild_id))
    await bot.db.commit()

@bot.event
async def on_raw_reaction_remove(ev:discord.RawReactionActionEvent):
    if ev.emoji.name != "⭐": return
    if (await bot.db.execute("DELETE FROM stars WHERE starrer=? AND starred=?",
                             (ev.user_id, ev.message_id))).rowcount == 0: return
    (minimum,channel) = await (await bot.db.execute("SELECT minimum,channel FROM guilds WHERE id=?", (ev.guild_id,))).fetchone()
    (count,) = await (await bot.db.execute("SELECT count(starrer) FROM stars WHERE starred=?", (ev.message_id,))).fetchone()
    print(count,minimum)
    if count<minimum:
        match await (await bot.db.execute("DELETE FROM messages WHERE original=? RETURNING starboard", (ev.message_id,))).fetchone():
            case (starboard,):
                await bot.get_channel(channel).get_partial_message(starboard).delete()
        await bot.db.commit()

async def do():
    bot.db=await aiosqlite.connect("bees.db")
    await bot.db.executescript("""
    BEGIN;
    CREATE TABLE IF NOT EXISTS guilds(
        id INTEGER PRIMARY KEY,
        minimum INTEGER NOT NULL DEFAULT 3,
        channel INTEGER NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS messages(
        original  INTEGER PRIMARY KEY,
        starboard INTEGER NOT NULL UNIQUE,
        guild INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stars(
        starrer INTEGER NOT NULL,
        starred INTEGER NOT NULL,
        guild INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_starred ON stars(starred); 
    COMMIT;""")
    with open("token") as f: tok=f.read().strip()
    try:
        await bot.start(tok)
    finally:
        print("bye")
        await bot.db.close()

asyncio.run(do())
