import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import logging


def ephemeral(c,*args,**kwargs): return c.response.send_message(*args,ephemeral=True,**kwargs)

class Starboard(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    @app_commands.command(description="change configuration like starboard channel or min stars")
    @app_commands.rename(channel="starboard-channel",minimum="minimum-star-count")
    @app_commands.default_permissions(manage_channels=True)
    async def starconfig(self, c:discord.Interaction, channel:discord.TextChannel|None=None, minimum:int|None=None):
        if channel is None and minimum is None:
            await self.db.execute("DELETE FROM guilds WHERE id=?", (c.guild_id,))
            await ephemeral(c,"unconfigued")
        else:
            if channel is not None:
                if channel.guild != c.guild: return await ephemeral(c,"eat bricks")
                await self.db.execute("INSERT OR REPLACE INTO guilds(id,channel) VALUES(?,?)", (c.guild_id, channel.id))
                await ephemeral(c,"ok")

            if minimum is not None:
                if minimum < 1: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("UPDATE guilds SET minimum=? WHERE id=?", (minimum, c.guild_id))
                if cur.rowcount==0:
                    self.db.rollback()
                    return ephemeral(c, "no channel set")

        await ephemeral(c, "ok")
        await self.db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        await self.db.execute("INSERT INTO stars(starrer,starred,guild) VALUES(?,?,?)",
                            (ev.user_id, ev.message_id, ev.guild_id))
        minimum,channel = await (await self.db.execute("SELECT minimum,channel FROM guilds WHERE id=?", (ev.guild_id,))).fetchone()
        (count,) = await (await self.db.execute("SELECT count(starrer) FROM stars WHERE starred=?", (ev.message_id,))).fetchone()
        if count==minimum:
            original = await self.bot.get_channel(ev.channel_id).fetch_message(ev.message_id)
            starboard = await self.bot.get_channel(channel).send(
                embed=discord.Embed(colour=discord.Color.yellow(), description=original.content)
                    .set_author(name=original.author.display_name, icon_url=original.author.display_avatar.url),
                content=original.jump_url
            )
            await self.db.execute("INSERT OR REPLACE INTO messages(original,starboard,guild) VALUES(?,?,?)",
                                (original.id, starboard.id, ev.guild_id))
        await self.db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        if (await self.db.execute("DELETE FROM stars WHERE starrer=? AND starred=?",
                                (ev.user_id, ev.message_id))).rowcount == 0: return
        (minimum,channel) = await (await self.db.execute("SELECT minimum,channel FROM guilds WHERE id=?", (ev.guild_id,))).fetchone()
        (count,) = await (await self.db.execute("SELECT count(starrer) FROM stars WHERE starred=?", (ev.message_id,))).fetchone()
        print(count,minimum)
        if count<minimum:
            match await (await self.db.execute("DELETE FROM messages WHERE original=? RETURNING starboard", (ev.message_id,))).fetchone():
                case (starboard,):
                    await self.bot.get_channel(channel).get_partial_message(starboard).delete()
            await self.db.commit()

async def setup(bot):
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
    await bot.add_cog(Starboard(bot))

