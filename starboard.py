# implements a starboard. each server has a minimum star count to publish a message to the starboard channel (sb),
#   or as i call it in this document "be awarded".
# this message also has to go away whenever the star count goes below the minimum again
# 
# there are currently three ways (medium) of starring a message:
#  0. reacting to the original message (msg)
#  1. reacting to the message in the starboard (msg_sb) once it has been awarded
#  2. using a context menu on the message (msg) or starboard (msg_sb)
# this means we have to make sure every user can't star any message more than once (hence the UNIQUE(starrer,msg) below)
# also, to not get strange behaviour like fake stars or double counts, when a star is removed, it has to be removed in
#   the same medium as it was added.
#
# timeout_d (interval in days) is optional. after timeout_d passes from the message being sent, the message will be
#   locked in its awarded/unawarded state. however, star counts are still updated.

SCHEMA = f"""BEGIN;
CREATE TABLE IF NOT EXISTS guilds(
    guild   INTEGER PRIMARY KEY,
    minimum INTEGER NOT NULL DEFAULT 3,
    sb      INTEGER NOT NULL UNIQUE, -- starboard channel
    timeout INTEGER                  -- timeout in days (NULL means no timeout)
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
    medium  INTEGER NOT NULL, -- 0 for msg react, 1 for msg_sb react, 2 for other
    UNIQUE(starrer, msg)
);
CREATE INDEX IF NOT EXISTS idx_starred ON stars(msg); 
COMMIT;"""

import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import datetime
import re

def calc_color(count:int) -> discord.Colour:
    return discord.Colour.from_rgb(255, 255, max(0,min(255,1024//(count+4)-20)))

def ephemeral(c, *args, **kwargs): return c.response.send_message(*args, ephemeral=True, **kwargs)

# True if the timeout hasn't passed yet. used as a precondition to adding/removing a message from the starboard
def on_time(msg_id:int, timeout_d:int|None) -> bool:
    if timeout_d is None: return True
    send_time = discord.utils.snowflake_time(msg_id)
    return datetime.datetime.now(datetime.UTC) < send_time + datetime.timedelta(days=timeout_d)

def short_disp(msg:discord.Message, escape=False) -> str:  # used for the *top messages and also replies in starboard
    return ( "[replying] "*(msg.reference is not None)
           + (discord.utils.escape_markdown(msg.content.replace("\n","")) if escape else msg.content)
           + " [attachment]"*len(msg.attachments)
           + " [sticker]"*len(msg.stickers)
           + " [poll]"*(msg.poll is not None)
           + " [edited]"*(msg.edited_at is not None))
class NotConfigured(Exception): pass

class Starboard(commands.Cog):

    def __init__(self, bot:commands.Bot) -> None:
        self.bot = bot
        self.db: aiosqlite.Connection = bot.db  # shortcut :3
        # yes you need to register these manually
        self.bot.tree.add_command(app_commands.ContextMenu(name="⭐ Star",  callback=self.star_menu  ), override=True)
        self.bot.tree.add_command(app_commands.ContextMenu(name="⭐ Unstar",callback=self.unstar_menu), override=True)

    ### HELPERS

    # these aren't that annoying i'm just a professional coper
    def partial_msg(self, channel:int, id:int) -> discord.PartialMessage:
        return self.bot.get_channel(channel).get_partial_message(id)
    def fecth_msg(self, channel:int, id:int) -> discord.Message:
        return self.bot.get_channel(channel).fetch_message(id)
    
    async def db_fetchone(self, sql, parameters) -> tuple|None:  # avoids annoying double await
        return await (await self.db.execute(sql, parameters)).fetchone()

    async def resolve_ref(self, ref:discord.MessageReference) -> discord.Message:
        return ref.cached_message or await self.fetch_msg(ref.channel_id,ref.message_id)
    
    # builds a message for starboard. given in this funny way so it can be unpacked into edit/send
    async def build_message(self, count:int, msg:discord.Message) -> dict:
        embed = discord.Embed(colour=calc_color(count), description=msg.content, timestamp=msg.created_at)
        att_no = len(msg.attachments)
        if att_no>0: embed.set_image(url=msg.attachments[0].url)
        if att_no>1: embed.set_footer(text=f"{att_no-1} attachment{'s are' if att_no!=2 else ' is'} not being shown")
        embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        if msg.reference is not None:
            try: reply = await self.resolve_ref(msg.reference)
            except (discord.NotFound, discord.Forbidden): embed.add_field(name="replying to some message",value="sorry")
            else:
                embed.add_field(name=f"replying to {reply.author.display_name}", value=short_disp(reply), inline=False)
                if att_no==0 and len(reply.attachments)>0:
                    embed.set_image(url=reply.attachments[0].url).set_footer(text="attachment shown is from reply")
        return { "content":"⭐🌟💫🤩🌌"[min(4,count//5)]+" "+msg.jump_url, "embed":embed }

    ### COMMANDS

    @commands.hybrid_command()
    async def info(self, ctx:commands.Context):
        """see some server-specific statistics for starboard."""
        total_stars,starred_messages = await self.db_fetchone(
            "SELECT count(*),count(DISTINCT msg) FROM stars WHERE guild=?", (ctx.guild.id,))
        msg = f"Hi, i am asteroid ^_^\nI have seen {total_stars} stars and {starred_messages} starred messages.\n"
        match await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ctx.guild.id,)):
            case minimum,sb_id:
                awarded_messages,= await self.db_fetchone("SELECT count(*) FROM awarded WHERE guild=?", (ctx.guild.id,))
                msg += (f"When messages reach {minimum} ⭐, they will be resent to <#{sb_id}>. "
                        f"Right now there are {awarded_messages} messages there.")
            case None:
                msg += f"The starboard is toggled off right now."
        await ctx.send(msg)

    @commands.hybrid_command()
    async def top(self, ctx:commands.Context):
        """see the top starred messages in the current guild."""
        async with ctx.typing():
            messages = await asyncio.gather(*[self.fetch_msg async for msg,msg_ch in
                await self.db.execute("SELECT msg,msg_ch FROM awarded WHERE guild=? "
                                      "ORDER BY (SELECT count(*) FROM stars WHERE msg=awarded.msg) DESC "
                                      "LIMIT 10", (ctx.guild.id,))])
            def shorten(x:str) -> str: return x[:400] + (x[400:] and "…")
            await ctx.send(allowed_mentions=discord.AllowedMentions.none(),embed=discord.Embed(
                title="Top Messages in Starboard",
                colour=discord.Colour.from_rgb(255,255,127),
                description="\n".join(shorten(
                    f"1. {msg.jump_url} **{msg.author.display_name}**: " + short_disp(msg, escape=True))
                for msg in messages),
            ))

    @commands.hybrid_command()
    async def random(self, ctx:commands.Context):
        """see a random starred message"""
        msg_id,msg_ch_id = await self.db_fetchone(
            "SELECT msg,msg_ch FROM awarded WHERE guild=? ORDER BY random() LIMIT 1", (ctx.guild.id,))
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
        await ctx.send(**await self.build_message(count, await self.fetch_msg(msg_ch_id,msg_id)))

    @commands.command(description="show a certain starred message")
    async def show(self, ctx:commands.Context, msg:discord.Message|None):
        """show a certain starred message
        :param msg: the message to show. may be given as a reply, or as an ID if in the same channel, or as a jump link.
        """
        match msg, ctx.message.reference:
            case None, None: raise commands.MissingRequiredArgument("msg")
            case None, ref:  msg = await self.resolve_ref(ref)
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg.id,))
        await ctx.send(**await self.build_message(count, msg))

    @app_commands.command()
    @app_commands.rename(sb="starboard-channel", minimum="minimum-stars", timeout_d="timeout")
    @app_commands.default_permissions(manage_channels=True)
    async def starconfig(self, c:discord.Interaction,
                         sb:discord.TextChannel|None=None, minimum:int|None=None, timeout_d:int|None=None):
        """change starboard configuration like starboard channel or minimum stars.
        if no arguments are given, shows the current configuration.
        if not all arguments are given, the rest will not be modified.
        
        :param sb: the starboard channel to set. required if configuring for the first time.
        :param minimum: the minimum star count to reach starboard.
        :param timeout: timeout period in days. after this period, messages cannot be added to or removed from the starboard.
        """
        if sb is None and minimum is None and timeout_d is None:
            match await self.db_fetchone("SELECT minimum,sb,timeout FROM guilds WHERE guild=?", (c.guild_id,)):
                case minimum,sb_id,None:
                    msg = f"minimum stars: {minimum}\nstarboard channel: <#{sb_id}>\ntimeout: never"
                case minimum,sb_id,timeout_d:
                    msg = f"minimum stars: {minimum}\nstarboard channel: <#{sb_id}>\ntimeout: {timeout_d} days"
                case None:
                    msg = "unconfigured"
            return await ephemeral(c, msg)
        else:
            if sb is not None:  # do this first in case minimum isn't set, bc that has a minimum value
                if sb.guild != c.guild: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("INSERT OR REPLACE INTO guilds(guild,sb) VALUES(?,?)", (c.guild_id, sb.id))
            if minimum is not None:
                if minimum < 1: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("UPDATE guilds SET minimum=? WHERE guild=?", (minimum, c.guild_id))
                if cur.rowcount==0:
                    await self.db.rollback()
                    return await ephemeral(c, "no starboard channel set")
            if timeout_d is not None:
                if timeout_d == 0: timeout_d = None
                elif timeout_d < 0: return await ephemeral(c,"eat bricks")
                cur = await self.db.execute("UPDATE guilds SET timeout=? WHERE guild=?", (timeout_d, c.guild_id))
                if cur.rowcount==0:
                    await self.db.rollback()
                    return await ephemeral(c, "no starboard channel set")

        await ephemeral(c, "ok")
        await self.db.commit()
    
    @commands.command()
    @commands.has_permissions(manage_channels=True)
    async def import_rdanny(self, ctx:commands.Context, sb:discord.TextChannel):
        """imports messages from an R. Danny starboard channel.
        :param sb: the starboard channel in question
        """
        scanned = 0
        mismatches:list[discord.Message] = []
        unparsable:list[discord.Message] = []
        async for msg_sb in sb.history(limit=None):
            if msg_sb.author.id != 80528701850124288: continue 
            if not (m := re.fullmatch(r".(?: \*\*(\d+)\*\*)? <#(\d+)> ID: (\d+)", msg_sb.content)):
                unparsable.append(msg_sb)
                continue
            count,msg_ch_id,msg_id = int(m[1] or "1"), int(m[2]), int(m[3])
            msg:discord.Message = await self.fetch_msg(msg_ch_id,msg_id)
            cnt_before = self.db.total_changes
            # get original stars
            if (stars := discord.utils.get(msg.reactions, emoji="⭐")):
                async for starrer in stars.users():
                    if starrer.id == msg.author.id: continue  # cheeky self-star
                    await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,0)",
                        (starrer.id,msg_id,ctx.guild.id))
            # get msg_sb stars
            if (stars := discord.utils.get(msg_sb.reactions, emoji="⭐")):
                async for starrer in stars.users():
                    if starrer == msg.author.id: continue
                    await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,1)",
                        (starrer.id,msg_id,ctx.guild.id))
            # ignore stars added by command (hopefully no one did that)
            cnt_computed = self.db.total_changes - cnt_before
            if cnt_computed != count: mismatches.append(msg_sb)
            # add awarded
            await self.db.execute("INSERT OR IGNORE INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                    (msg_id, msg_sb.id, msg_ch_id, ctx.guild.id, msg.author.id))
            scanned += 1
        await self.db.commit()
        await ctx.send(f"{scanned} messages added" +
            '\nstar count mismatches: '       *(len(mismatches)!=0) + ", ".join(i.jump_url for i in mismatches) +
            "\nmessages i didn't understand: "*(len(unparsable)!=0) + ", ".join(i.jump_url for i in unparsable) +
            "\nnow you need to unconfigure r.danny and configure asteroid, i think")

    ### STARRING
    # can work with reactions (`on_raw_reaction_{add,remove}`, "raw" in case someone stars older messages)
    #     or the pop up menu (`{,un}star_menu`, added in `__main__`).
    # these are quite different.interactions and reaction events and command contexts (if i want to add those later)
    #     all have slightly different interfaces to get the different IDs (the info we actually want),
    #     and raw reaction events give only a channel/message ID, but menus give you the full message already.
    # so these four↓ functions actually do nothing except glue together `get_guild_info`, `find_msg` and
    #     `{add,remove}_star` that do the actual work. we pass around the data in a big dict `r` because it looks nicer
    #     (i think), and we use the same variable names as the db with an occasional `_id` added to the end 
    # note that we delay the ms fetching until we know the star went through, as we only need it to post the message in
    #     the starboard. we also don't use the message cache explicitly because there seems to not be an easy way to do
    #     it and also LyricLy called it "uselesscore"

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        r  = await self.get_guild_info(ev.guild_id)
        r |= await self.find_msg(msg_id=ev.message_id, msg_ch_id=ev.channel_id, **r)
        if not await self.add_star(user_id=ev.user_id, **r):
            # the star was added in a different medium. delete it and forget
            await self.partial_msg(ev.channel_id,ev.message_id).remove_reaction("⭐", discord.Object(ev.user_id))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        r  = await self.get_guild_info(ev.guild_id)
        r |= await self.find_msg(msg_id=ev.message_id, msg_ch_id=ev.channel_id, **r)
        await self.remove_star(user_id=ev.user_id, **r)
    
    async def star_menu(self, c:discord.Interaction, msg:discord.Message):
        r  = await self.get_guild_info(c.guild_id)
        r |= await self.find_msg(msg_id=msg.id, msg_ch_id=msg.channel.id, msg=msg, **r) | {"medium":2}
        success = await self.add_star(user_id=c.user.id, **r)
        await ephemeral(c, "ok" if success else "you already starred that, bozo!")

    async def unstar_menu(self, c:discord.Interaction, msg:discord.Message):
        r  = await self.get_guild_info(c.guild_id)
        r |= await self.find_msg(msg_id=msg.id, msg_ch_id=msg.channel.id, msg=msg, **r) | {"medium":2}
        success = await self.remove_star(user_id=c.user.id, **r)
        await ephemeral(c, "ok" if success else "couldn't remove star")

    async def get_guild_info(self, guild_id:int) -> dict:
        match await self.db_fetchone("SELECT minimum,sb,timeout FROM guilds WHERE guild=?", (guild_id,)):
            case None: raise NotConfigured()
            case minimum,sb_id,timeout_d:
                return {"minimum":minimum, "sb_id":sb_id, "timeout_d":timeout_d, "guild_id":guild_id}

    # if the message was a starboard message, we want to star the original instead.
    # this sets medium to 1 if this happened. the menu functions ignore this, overriding it with medium=2, because we
    #     don't need to keep track where the user right clicked.
    async def find_msg(self, minimum:int, sb_id:int, msg_id:int, msg_ch_id:int, guild_id:int,
                       msg:discord.Message|None=None) -> dict:  # <- useless type annotation 
        medium = 0
        if msg_ch_id == sb_id:
            try:
                msg_id,msg_ch_id = await self.db_fetchone("SELECT msg,msg_ch FROM awarded WHERE msg_sb=?", (msg_id,))
                medium = 1
                msg = None  # the message from the menu is NO LONGER the right message
            except TypeError: pass  # message in starboard but not managed by this bot. i'll allow starring it
        return {"msg_id":msg_id, "msg_ch_id":msg_ch_id, "medium":medium, "msg":msg}
    
    async def add_star(self, minimum:int, sb_id:int, timeout_d:int|None, msg_id:int, msg_ch_id:int, guild_id:int,
                       user_id:int, medium:int, msg:discord.Message|None=None) -> bool:
        if (await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,?)",
                                  (user_id,msg_id,guild_id,medium))).rowcount == 0:
            return False  # if the star was there already (when above query fails uniqueness constraint)
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
        if count<minimum:
            await self.db.commit()  # make sure to commit to add the star
            return True
        msg = msg or await self.fetch_msg(msg_ch_id,msg_id)
        match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
            case msg_sb_id,:  # already in starboard, edit the message
                try: await self.partial_msg(sb_id,msg_sb_id).edit(**await self.build_message(count, msg))
                except discord.Forbidden: pass  # if the message was deleted, or on migration
            case None if on_time(msg_id,timeout_d):
                # not in starboard yet (usually bc count==minimum, or minimum was higher back then)
                msg_sb = await self.bot.get_channel(sb_id).send(**await self.build_message(count, msg))
                await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                      (msg_id, msg_sb.id, msg_ch_id, guild_id, msg.author.id))
        await self.db.commit()
        return True
    
    async def remove_star(self, minimum:int, sb_id:int, timeout_d:int|None, msg_id:int, msg_ch_id:int, guild_id:int,
                          user_id:int, medium:int, msg:discord.Message|None=None) -> bool:
        dlt = await self.db.execute("DELETE FROM stars WHERE starrer=? AND msg=? AND medium=?", (user_id,msg_id,medium))
        if dlt.rowcount == 0:
            return False # don't bother continuing if the star wasn't recorded or in a different medium
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
        if count<minimum and on_time(msg_id,timeout_d):  # message unawarded, or it wasn't awarded to begin with
            match await self.db_fetchone("DELETE FROM awarded WHERE msg=? RETURNING msg_sb", (msg_id,)):
                case msg_sb_id,: await self.partial_msg(sb_id,msg_sb_id).delete()
        else:  # unstarred, but the message can stay in starboard
            msg = msg or await self.fetch_msg(msg_ch_id,msg_id)
            match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
                case msg_sb_id,:
                    try: await self.partial_msg(sb_id,msg_sb_id).edit(**await self.build_message(count, msg))
                    except discord.Forbidden: pass  # if the message was deleted, or on migration
                case None if on_time(msg_id,timeout_d):
                    # edge case: the message was and still is award-worthy, but it wasn't sent (maybe because minimum
                    # was higher), and the timeout hasn't passed. we add it anyways, to be consistent with star add
                    msg_sb = await self.bot.get_channel(sb_id).send(**await self.build_message(count, msg))
                    await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                          (msg_id, msg_sb.id, msg_ch_id, guild_id, msg.author.id))
        await self.db.commit()
        return True

async def setup(bot):
    await bot.db.executescript(SCHEMA)
    await bot.add_cog(Starboard(bot))

if __name__ == "__main__": print("you ran the wrong file. BOZO")