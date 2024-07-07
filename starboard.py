# implements a starboard. each server has a minimum star count to publish a message to the starboard channel (sb),
#   or as i call it in this document "be awarded".
# this message also has to go away whenever the star count 
# 
# there are currently two ways of starring a message:
#  - reacting to the original message (msg)
#  - reacting to the message in the starboard (msg_sb) once it has been awarded
# possibly in the future this will include commands or context menus.
# this means we have to make sure every user can't star any message more than once (hence the UNIQUE(starrer,msg) below)

SCHEMA = f"""BEGIN;
CREATE TABLE IF NOT EXISTS guilds(
    guild   INTEGER PRIMARY KEY,
    minimum INTEGER NOT NULL DEFAULT 3,
    sb      INTEGER NOT NULL UNIQUE  -- starboard channel
);
CREATE TABLE IF NOT EXISTS awarded(
    msg     INTEGER PRIMARY KEY,     -- original message
    msg_ch  INTEGER NOT NULL,        -- original message's channel
    msg_sb  INTEGER NOT NULL UNIQUE, -- message in starboard
    guild   INTEGER NOT NULL,
    author  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stars(
    starrer INTEGER NOT NULL,
    msg     INTEGER NOT NULL,
    guild   INTEGER NOT NULL,
    UNIQUE(starrer, msg)
);
CREATE INDEX IF NOT EXISTS idx_starred ON stars(msg); 
COMMIT;"""

import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import logging

def calc_color(count:int) -> discord.Colour:
    return discord.Colour.from_rgb(255,255,max(0,min(255,1024//(count+4)-20)))

def build_message(count:int, msg:discord.Message) -> dict:
    return {"content": msg.jump_url,
            "embed": discord.Embed(colour=calc_color(count), description=msg.content)
                     .set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url) }

def ephemeral(c,*args,**kwargs): return c.response.send_message(*args,ephemeral=True,**kwargs)

class Starboard(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.db: aiosqlite.Connection = bot.db

    def partial_msg(self, channel:int, id:int) -> discord.PartialMessage:
        return self.bot.get_channel(channel).get_partial_message(id)
    
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
                awarded_messages, = await self.db_fetchone("SELECT count(*) FROM awarded WHERE guild=?", (ctx.guild.id,))
                msg += (f"When messages reach {minimum} ⭐, they will be resent to <#{sb_id}>. "
                        f"Right now there are {awarded_messages} messages there.")
            case None:
                msg += f"The starboard is toggled off right now."
        await ctx.send(msg)

    @commands.hybrid_command(description="see a random starred message")
    async def random(self, ctx:commands.Context):
        msg_id,msg_ch_id = await self.db_fetchone(
            "SELECT msg,msg_ch FROM awarded WHERE guild=? ORDER BY random() LIMIT 1", (ctx.guild.id,))
        count, = await self.db_fetchone("SELECT count(starrer) FROM stars WHERE msg=?", (msg_id,))
        await ctx.send(**build_message(count, await self.partial_msg(msg_ch_id,msg_id).fetch()))

    @app_commands.command(description="change configuration like msg_sb channel or min stars")
    @app_commands.rename(sb="starboard-channel",minimum="minimum-star-count")
    @app_commands.default_permissions(manage_channels=True)
    async def starconfig(self, c:discord.Interaction, sb:discord.TextChannel|None=None, minimum:int|None=None):
        if sb is None and minimum is None:
            await self.db.execute("DELETE FROM guilds WHERE guild=?", (c.guild_id,))
            await ephemeral(c,"unconfigured")
        else:
            if sb is not None:  # do this first in case minimum isn¡t set, bc that has a minimum value
                if sb.guild != c.guild: return await ephemeral(c,"eat bricks")
                await self.db.execute("INSERT OR REPLACE INTO guilds(guild,sb) VALUES(?,?)", (c.guild_id, sb.id))
            if minimum is not None:
                if minimum < 1: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("UPDATE guilds SET minimum=? WHERE guild=?", (minimum, c.guild_id))
                if cur.rowcount==0:
                    await self.db.rollback()
                    return await ephemeral(c, "no starboard channel set")
        await ephemeral(c, "ok")
        await self.db.commit()


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        try:
            minimum,sb_id = await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ev.guild_id,))
        except TypeError: return  # !!! not tracking stars when starboard isn't enabled isn't ideal
        msg_id, msg_ch_id = ev.message_id, ev.channel_id
        if msg_ch_id == sb_id: # if starring a starboard message, star the original instead
                               # (if starboard channel changes, those messages will be starrable again. prob fine)
            try: msg_id,msg_ch_id = await self.db_fetchone("SELECT msg,msg_ch FROM awarded WHERE msg_sb=?", (msg_id,))
            except TypeError: pass  # message in starboard but not managed by this bot. i'll allow it

        if (await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild) VALUES(?,?,?)",
                                  (ev.user_id,msg_id,ev.guild_id))).rowcount == 0:
            return  # can happen if bot downtime or star added from different sources (i.e. react to msg_sb and msg)
        count, = await self.db_fetchone("SELECT count(starrer) FROM stars WHERE msg=?", (msg_id,))
        if count<minimum: return await self.db.commit()  # make sure to commit to add the star
        new = build_message(count, await self.partial_msg(msg_ch_id,msg_id).fetch())
        match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
            case msg_sb_id,:  # already in starboard, edit the message
                await self.partial_msg(sb_id,msg_sb_id).edit(**new)
            case None:        # not in starboard yet (usually bc count==minimum, or minimum was higher back then)
                msg_sb = await self.bot.get_channel(sb_id).send(**new)
                await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                      (msg_id, msg_sb.id, msg_ch_id, ev.guild_id, ev.message_author_id))
        await self.db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        try:
            minimum,sb_id = await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ev.guild_id,))
        except TypeError: return  # !!! not tracking stars when starboard isn't enabled isn't ideal
        msg_id,msg_ch_id = ev.message_id,ev.channel_id
        if msg_ch_id == sb_id: # if unstarring a starboard message, unstar the original instead
                               # (this check fails if starboard channel changes. prob fine)
            try: msg_id,msg_ch_id = await self.db_fetchone("SELECT msg,msg_ch FROM awarded WHERE msg_sb=?", (msg_id,))
            except TypeError: pass  # message in starboard but not managed by this bot. i'll allow it
            
        if (await self.db.execute("DELETE FROM stars WHERE starrer=? AND msg=?", (ev.user_id, msg_id))
           ).rowcount == 0: return  # don't bother continuing if the star wasn't recorded to begin with
        count, = await self.db_fetchone("SELECT count(starrer) FROM stars WHERE msg=?", (msg_id,))
        if count<minimum:  # message unawarded, or it wasn't awarded to begin with
            match await self.db_fetchone("DELETE FROM awarded WHERE msg=? RETURNING msg_sb", (msg_id,)):
                case msg_sb_id,: await self.partial_msg(sb_id,msg_sb_id).delete()
        else:  # unstarred, but the message can stay in starboard
            new = build_message(count, await self.partial_msg(msg_ch_id,msg_id).fetch())
            match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
                case msg_sb_id,:
                    await self.partial_msg(sb_id,msg_sb_id).edit(**new)
                case None:  # edge case: the message was and still is award-worthy, but it wasn't sent (probably because
                            # minimum was higher). we add it anyways, to be consistent with star add
                    msg_sb = await self.bot.get_channel(sb_id).send(**new)
                    await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                        (msg_id, msg_sb.id, msg_ch_id, ev.guild_id, ev.message_author_id))
        
        await self.db.commit()

async def setup(bot):
    await bot.db.executescript(SCHEMA)
    await bot.add_cog(Starboard(bot))

