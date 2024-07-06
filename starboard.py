import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import logging

def calc_color(count:int) -> discord.Colour:
    return discord.Colour.from_rgb(255,255,max(0,min(255,1024//(count+2)-20)))

def build_message(count:int, msg:discord.Message) -> dict:
    return {"content": msg.jump_url,
            "embed": discord.Embed(colour=calc_color(count), description=msg.content)
                     .set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url) }

def ephemeral(c,*args,**kwargs): return c.response.send_message(*args,ephemeral=True,**kwargs)

class Starboard(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.db: aiosqlite.Connection = bot.db

    def partial_msg(self,channel,id): return self.bot.get_channel(channel).get_partial_message(id)
    
    async def db_fetchone(self, sql, parameters, default=None):  # avoids annoying double await
        a = await (await self.db.execute(sql, parameters)).fetchone()
        return a if a is not None else default # () is falsy :(

    @commands.hybrid_command(description="see some server-specific statistics for starboard")
    async def info(self, ctx:commands.Context):
        total_stars,starred_messages = await self.db_fetchone(
            "SELECT count(*),count(DISTINCT msg) FROM stars WHERE guild=?", (ctx.guild.id,))
        msg = f"Hi, i am asteroid ^_^\nI have seen {total_stars} stars and {starred_messages} starred messages.\n"
        match await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ctx.guild.id,)):
            case minimum,sb_id:
                awarded_messages, = await self.db_fetchone("SELECT count(*) FROM messages WHERE guild=?", (ctx.guild.id,))
                msg += (f"When messages reach {minimum} ⭐, they will be resent to <#{sb_id}>. "
                        f"Right now there are {awarded_messages} messages there.")
            case None:
                msg += f"The starboard is toggled off right now."
        await ctx.send(msg)

    @app_commands.command(description="change configuration like msg_sb channel or min stars")
    @app_commands.rename(sb="starboard-channel",minimum="minimum-star-count")
    @app_commands.default_permissions(manage_channels=True)
    async def starconfig(self, c:discord.Interaction, sb:discord.TextChannel|None=None, minimum:int|None=None):
        if sb is None and minimum is None:
            await self.db.execute("DELETE FROM guilds WHERE guild=?", (c.guild_id,))
            await ephemeral(c,"unconfigued")
        else:
            if sb is not None:
                if sb.guild != c.guild: return await ephemeral(c,"eat bricks")
                await self.db.execute("INSERT OR REPLACE INTO guilds(guild,sb) VALUES(?,?)", (c.guild_id, sb.id))
                await ephemeral(c,"ok")

            if minimum is not None:
                if minimum < 1: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("UPDATE guilds SET minimum=? WHERE guild=?", (minimum, c.guild_id))
                if cur.rowcount==0:
                    self.db.rollback()
                    return ephemeral(c, "no starboard channel set")

        await ephemeral(c, "ok")
        await self.db.commit()


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        await self.db.execute("INSERT INTO stars(starrer,msg,guild) VALUES(?,?,?)", (ev.user_id,ev.message_id,ev.guild_id))
        try:
            minimum,sb_id = await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ev.guild_id,))
        except TypeError:
            return await self.db.commit()
        count, = await self.db_fetchone("SELECT count(starrer) FROM stars WHERE msg=?", (ev.message_id,))
        if count<minimum: return await self.db.commit()
        new = build_message(count, await self.partial_msg(ev.channel_id,ev.message_id).fetch())
        if count==minimum:
            msg_sb = await self.bot.get_channel(sb_id).send(**new)
            await self.db.execute("INSERT OR REPLACE INTO messages(msg,msg_sb,guild,author) VALUES(?,?,?,?)",
                                  (ev.message_id, msg_sb.id, ev.guild_id, ev.message_author_id))
        else:
            (msg_sb_id,) = await self.db_fetchone("SELECT msg_sb FROM messages WHERE msg=?", (ev.message_id,))
            await self.partial_msg(sb_id,msg_sb_id).edit(**new)
        await self.db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        dlt = await self.db.execute("DELETE FROM stars WHERE starrer=? AND msg=?", (ev.user_id, ev.message_id))
        if dlt.rowcount == 0: return
        try:
            minimum,sb_id = await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ev.guild_id,))
        except TypeError:
            return await self.db.commit()
        count, = await self.db_fetchone("SELECT count(starrer) FROM stars WHERE msg=?", (ev.message_id,))
        if count<minimum:
            match await self.db_fetchone("DELETE FROM messages WHERE msg=? RETURNING msg_sb", (ev.message_id,)):
                case msg_sb_id,:
                    await self.partial_msg(sb_id,msg_sb_id).delete()
        else:
            match await self.db_fetchone("SELECT msg_sb FROM messages WHERE msg=?", (ev.message_id,)):
                case msg_sb_id,:
                    new = build_message(count, await self.partial_msg(ev.channel_id,ev.message_id).fetch())
                    await self.partial_msg(sb_id,msg_sb_id).edit(**new)
        await self.db.commit()

async def setup(bot):
    await bot.db.executescript(f"""
        BEGIN;
        CREATE TABLE IF NOT EXISTS guilds(
            guild   INTEGER PRIMARY KEY,
            minimum INTEGER NOT NULL DEFAULT 3,
            sb      INTEGER NOT NULL UNIQUE  -- starboard channel
        );
        CREATE TABLE IF NOT EXISTS messages( -- ONLY awarded messages
            msg     INTEGER PRIMARY KEY,     -- original message
            msg_sb  INTEGER NOT NULL UNIQUE, -- message in starboard
            guild   INTEGER NOT NULL,
            author  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stars(
            starrer INTEGER NOT NULL,
            msg     INTEGER NOT NULL,
            guild   INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_starred ON stars(msg); 
        COMMIT;""")
    await bot.add_cog(Starboard(bot))

