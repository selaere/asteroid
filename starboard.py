# implements a starboard. each server has a minimum star count to publish a message to the starboard channel (sb),
#   or as i call it in this document "be awarded".
# this message also has to go away whenever the star count goes below the minimum again
#
# there are currently three ways (medium) of starring a message:
#  0. reacting to the original message (msg)
#  1. reacting to the message in the starboard (msg_sb) once it has been awarded
#  2. using a context menu on the message (msg) or starboard (msg_sb)
FROM_REACT = 0
FROM_REACT_SB = 1
FROM_MENU = 2
# this means we have to make sure every user can't star any message more than once (hence the UNIQUE(starrer,msg) below)
# also, to not get strange behaviour like fake stars or double counts, when a star is removed, it has to be removed in
#   the same medium as it was added.
#
# timeout_d (interval in days) is optional. after timeout_d passes from the message being sent, the message will be
#   locked in its awarded/unawarded state. however, star counts are still updated.

SCHEMA = f"""
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
    author  INTEGER NOT NULL         -- original message's author
);
CREATE TABLE IF NOT EXISTS stars(
    starrer INTEGER NOT NULL,
    msg     INTEGER NOT NULL,
    guild   INTEGER NOT NULL,
    medium  INTEGER NOT NULL, -- 0 for msg react, 1 for msg_sb react, 2 for other
    UNIQUE(starrer, msg)
);
CREATE INDEX IF NOT EXISTS idx_starred ON stars(msg);
"""

import discord
import discord.app_commands as app_commands
import discord.ext.commands as commands
import aiosqlite
import asyncio
import datetime
import re
import logging
from dataclasses import dataclass

def calc_color(count:int) -> discord.Colour:
    return discord.Colour.from_rgb(255, 255, max(0,min(255,1024//(count+3)-20)))

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

def msg_fields(msg: discord.Message) -> dict:
    return { "msg_id":msg.id, "msg_ch_id":msg.channel.id, "author_id":msg.author.id, "msg":msg }

class NotConfigured(Exception): pass

class Starboard(commands.Cog):

    def __init__(self, bot:commands.Bot) -> None:
        self.bot: commands.Bot = bot
        self.db: aiosqlite.Connection = bot.db  # shortcut :3
        # yes you need to register these manually
        self.bot.tree.add_command(app_commands.ContextMenu(name="⭐ Star",  callback=self.star_menu  ), override=True)
        self.bot.tree.add_command(app_commands.ContextMenu(name="⭐ Unstar",callback=self.unstar_menu), override=True)

    ### HELPERS

    # get the channel properly (because archived threads are not kept in cache)
    async def get_channel(self, guild_id:int, channel_id:int) -> discord.TextChannel:
        match self.bot.get_channel(channel_id):
            case None: return await self.bot.get_guild(guild).fetch_channel(channel_id)
            case x:    return x

    def partial_msg(self, channel:int, id:int) -> discord.PartialMessage:
        return self.bot.get_partial_messageable(channel).get_partial_message(id)
    async def fetch_msg(self, channel:int, id:int) -> discord.Message:
        return await self.partial_msg(channel,id).fetch()

    # only intended for starred messages, to handle message disappearance. but it will do nothing to other messages
    async def fetch_msg_opt(self, msg_ch_id:int, msg_id:int):
        try:
            return await self.fetch_msg(msg_ch_id,msg_id)
        except (discord.NotFound, discord.Forbidden):
            await self.forget_message(msg_id)
            return None

    async def db_fetchone(self, sql, parameters) -> tuple|None:  # avoids annoying double await
        return await (await self.db.execute(sql, parameters)).fetchone()

    async def resolve_ref(self, ref:discord.MessageReference) -> discord.Message|None:
        try:
            return ref.cached_message or await self.fetch_msg(ref.channel_id,ref.message_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def channel_allowed(self, guild_id:int, ch_id:int) -> bool:
        ch = await self.get_channel(guild_id, ch_id)
        if isinstance(ch, discord.Thread):
            ch = ch.parent
        return re.search(r"\bcw\b", ch.name) is None

    # builds a message for starboard. given in this funny way so it can be unpacked into edit/send
    async def build_message(self, count:int, msg:discord.Message) -> dict:
        embed = discord.Embed(colour=calc_color(count), description=msg.content, timestamp=msg.created_at)
        att_no = len(msg.attachments)
        if att_no>0: embed.set_image(url=msg.attachments[0].url)
        if att_no>1: embed.set_footer(text=f"{att_no-1} attachment{'s are' if att_no!=2 else ' is'} not being shown")
        embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        if msg.reference is not None:
            match await self.resolve_ref(msg.reference):
                case None: embed.add_field(name="replying to some message",value="sorry")
                case reply:
                    embed.add_field(name=f"replying to {reply.author.display_name}", value=short_disp(reply), inline=False)
                    if att_no==0 and len(reply.attachments)>0:
                        embed.set_image(url=reply.attachments[0].url).set_footer(text="attachment shown is from reply")
        return { "content":"⭐🌟💫🤩🌌"[min(4,count//5)]+" "+msg.jump_url, "embed":embed }

    async def forget_message(self, msg_id:int, **r):
        if (await self.db.execute("DELETE FROM stars WHERE msg=?", (msg_id,))).rowcount != 0:
            await self.unaward(msg_id, **r)

    # does nothing if the message wasn't awarded
    async def unaward(self, msg_id:int, sb_id:int|None=None, **_):
        match await self.db_fetchone("SELECT msg_sb,guild FROM awarded WHERE msg=?", (msg_id,)):
            case msg_sb_id, guild_id:
                await self.db.execute("DELETE FROM awarded WHERE msg=?", (msg_id,))
                try:
                    if sb_id is not None:
                        sb_id, = await self.db_fetchone("SELECT sb FROM guilds WHERE guild=?", (guild_id,))
                    # huh. this is problematic if sb changes
                    await self.partial_msg(sb_id,msg_sb_id).delete()
                except discord.Forbidden: pass

    ### STARRING
    # can work with reactions (`on_raw_reaction_{add,remove}`, "raw" in case someone stars older messages)
    #     or the pop up menu (`{,un}star_menu`, added in `__main__`).
    # these are quite different.interactions and reaction events and command contexts (if i want to add those later)
    #     all have slightly different interfaces to get the different IDs (the info we actually want),
    #     and raw reaction events give only a channel/message ID, but menus give you the full message already.
    # so these four↓ functions don't do much other than glue together `get_guild_info`, `find_msg` and
    #     `{add,remove}_star`, that do the actual work. we pass around the data in a big dict `r` because it looks nicer
    #     (i think), and we use the same variable names as the db with an occasional `_id` added to the end
    # note that we delay the ms fetching until we know the star went through, as we only need it to post the message in
    #     the starboard. we also don't use the message cache explicitly because there seems to not be an easy way to do
    #     it and also LyricLy called it "uselesscore"

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        r  = await self.get_guild_info(ev.guild_id)
        r |= await self.find_msg(msg_id=ev.message_id, msg_ch_id=ev.channel_id, author_id=ev.message_author_id, **r)
        if "ok" != await self.add_star(user_id=ev.user_id, **r):
            # something happened. delete it and forget
            await self.partial_msg(ev.channel_id,ev.message_id).remove_reaction("⭐", discord.Object(ev.user_id))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, ev:discord.RawReactionActionEvent):
        if ev.emoji.name != "⭐": return
        r  = await self.get_guild_info(ev.guild_id)
        r |= await self.find_msg(msg_id=ev.message_id, msg_ch_id=ev.channel_id, author_id=ev.message_author_id, **r)
        await self.remove_star(user_id=ev.user_id, **r)

    async def star_menu(self, c:discord.Interaction, msg:discord.Message):
        r  = await self.get_guild_info(c.guild_id)
        r |= await self.find_msg(**msg_fields(msg), **r) | {"medium":FROM_MENU}
        txt = await self.add_star(user_id=c.user.id, **r)
        await c.response.send_message(txt, ephemeral=True)

    async def unstar_menu(self, c:discord.Interaction, msg:discord.Message):
        r  = await self.get_guild_info(c.guild_id)
        r |= await self.find_msg(**msg_fields(msg), **r) | {"medium":FROM_MENU}
        txt = await self.remove_star(user_id=c.user.id, **r)
        await c.response.send_message(txt, ephemeral=True)

    async def get_guild_info(self, guild_id:int) -> dict:
        match await self.db_fetchone("SELECT minimum,sb,timeout FROM guilds WHERE guild=?", (guild_id,)):
            case None: raise NotConfigured()
            case minimum,sb_id,timeout_d:
                return {"minimum":minimum, "sb_id":sb_id, "timeout_d":timeout_d, "guild_id":guild_id}

    # if the message was a starboard message, we want to star the original instead.
    # set medium to FROM_REACT_SB if this happened. the menu functions ignore this, overriding it with medium=FROM_MENU,
    #   because we don't need to keep track where the user right clicked.
    async def find_msg(self, minimum:int, sb_id:int, msg_id:int, msg_ch_id:int, guild_id:int, author_id:int,
                       msg:discord.Message|None=None, **_) -> dict:  # <- useless type annotation
        medium = FROM_REACT
        if msg_ch_id == sb_id:
            try:
                msg_id, msg_ch_id, author_id = await self.db_fetchone(
                    "SELECT msg,msg_ch,author FROM awarded WHERE msg_sb=?", (msg_id,))
                medium = FROM_REACT_SB
                msg = None  # the message from the menu is NO LONGER the right message
            except TypeError: pass  # message in starboard but not managed by this bot. i'll allow starring it
        return {"msg_id":msg_id, "msg_ch_id":msg_ch_id, "author_id":author_id, "medium":medium, "msg":msg}

    async def add_star(self, minimum:int, sb_id:int, timeout_d:int|None, msg_id:int, msg_ch_id:int, guild_id:int,
                       author_id:int, user_id:int, medium:int, msg:discord.Message|None=None) -> str:
        if user_id == author_id: return "rule 11"
        if not await self.channel_allowed(guild_id, msg_ch_id): return "no starring in cw channels. sorry!"
        if (await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,?)",
                                  (user_id,msg_id,guild_id,medium))).rowcount == 0:  # try to add star
            return "you already starred that, bozo!"  # if the star was there already (when above query fails UNIQUE)
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
        if count >= minimum:
            msg = msg or await self.fetch_msg_opt(msg_ch_id,msg_id)
            if msg is None: return "this message never existed. no clue what you are talking about"
            match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
                case msg_sb_id,:  # already in starboard, edit the message
                    try: await self.partial_msg(sb_id,msg_sb_id).edit(**await self.build_message(count, msg))
                    except discord.Forbidden: pass  # if the message was deleted, or on migration
                case None if on_time(msg_id,timeout_d):
                    # not in starboard yet (usually bc count==minimum, or minimum was higher back then)
                    msg_sb = await self.bot.get_partial_messageable(sb_id).send(**await self.build_message(count, msg))
                    await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                        (msg_id, msg_sb.id, msg_ch_id, guild_id, msg.author.id))
        await self.db.commit()
        return "ok"

    async def remove_star(self, minimum:int, sb_id:int, timeout_d:int|None, msg_id:int, msg_ch_id:int, guild_id:int,
                          author_id:int, user_id:int, medium:int, msg:discord.Message|None=None) -> str:
        dlt = await self.db.execute("DELETE FROM stars WHERE starrer=? AND msg=? AND medium=?", (user_id,msg_id,medium))
        if dlt.rowcount == 0:
            # don't continue if the star wasn't recorded or in a different medium.
            # the error message isn't used if this is called from a reaction_remove, but it's cheap and pretty unlikely
            # (something has to get out of sync with the reactions)
            match await self.db_fetchone("SELECT medium FROM stars WHERE starrer=? AND msg=?", (user_id,msg_id)):
                case None: return "you haven't starred that yet, bozo!"
                case 0,:   return "you already reacted with a ⭐ to this message. remove this reaction to proceed."
                case 1,:   return ("you already reacted with a ⭐ to the message in the starboard. remove this reaction"
                                   " to proceed.")
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
        if count<minimum and on_time(msg_id,timeout_d):  # message unawarded, or it wasn't awarded to begin with
            await self.unaward(msg_id, sb_id)
        else:  # unstarred, but the message can stay in starboard
            msg = msg or await self.fetch_msg_opt(msg_ch_id,msg_id)
            if msg is None: return True
            match await self.db_fetchone("SELECT msg_sb FROM awarded WHERE msg=?", (msg_id,)):
                case msg_sb_id,:
                    try: await self.partial_msg(sb_id,msg_sb_id).edit(**await self.build_message(count, msg))
                    except discord.Forbidden: pass  # if the message was deleted, or on migration
                case None if on_time(msg_id,timeout_d):
                    # edge case: the message was and still is award-worthy, but it wasn't sent (maybe because minimum
                    # was higher), and the timeout hasn't passed. we add it anyways, to be consistent with star add
                    msg_sb = await self.bot.get_partial_messageable(sb_id).send(**await self.build_message(count, msg))
                    await self.db.execute("INSERT INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                          (msg_id, msg_sb.id, msg_ch_id, guild_id, msg.author.id))
        await self.db.commit()
        return "ok"

    @commands.Cog.listener()
    async def on_raw_message_delete(self, ev:discord.RawMessageDeleteEvent):
        await self.forget_message(ev.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, ev:discord.RawBulkMessageDeleteEvent):
        r = await self.get_guild_info(ev.guild_id)
        for msg_id in ev.message_ids:
            await self.forget_message(msg_id, r["sb_id"])

    ### USER COMMANDS

    @commands.hybrid_command()
    async def info(self, ctx:commands.Context):
        """see some server-specific statistics for starboard."""
        total_stars,starred_messages = await self.db_fetchone(
            "SELECT count(*),count(DISTINCT msg) FROM stars WHERE guild=?", (ctx.guild.id,))
        txt = f"Hi, i am asteroid ^_^\nI have seen {total_stars} stars and {starred_messages} starred messages.\n"
        match await self.db_fetchone("SELECT minimum,sb FROM guilds WHERE guild=?", (ctx.guild.id,)):
            case minimum,sb_id:
                awarded_messages,= await self.db_fetchone("SELECT count(*) FROM awarded WHERE guild=?", (ctx.guild.id,))
                txt += (f"When messages reach {minimum} ⭐, they will be resent to <#{sb_id}>. "
                        f"Right now there are {awarded_messages} messages there.")
            case None:
                txt += f"The starboard is toggled off right now."
        await ctx.send(txt)

    @commands.hybrid_command()
    async def top(self, ctx:commands.Context):
        """see the top starred messages in the current guild."""
        async with ctx.typing():
            messages = await asyncio.gather(*[self.fetch_msg_opt(msg_ch_id,msg_id) async for msg_ch_id,msg_id in
                await self.db.execute("SELECT msg_ch,msg FROM awarded WHERE guild=? "
                                      "ORDER BY (SELECT count(*) FROM stars WHERE msg=awarded.msg) DESC "
                                      "LIMIT 10", (ctx.guild.id,))])
            def shorten(x:str) -> str: return x[:400] + (x[400:] and "…")
            await ctx.send(allowed_mentions=discord.AllowedMentions.none(),embed=discord.Embed(
                title="Top Messages in Starboard",
                colour=discord.Colour.from_rgb(255,255,127),
                description="\n".join(
                    shorten(f"1. {msg.jump_url} **{msg.author.display_name}**: " + short_disp(msg, escape=True))
                for msg in messages if msg),
            ))

    @commands.hybrid_command()
    async def random(self, ctx:commands.Context, user:discord.User|None=None):
        """see a random starred message

        :param user: optional. filter posts from a certain user"""
        if user is None:
            out = await self.db_fetchone(
                "SELECT msg,msg_ch FROM awarded WHERE guild=? ORDER BY random() LIMIT 1", (ctx.guild.id,))
        else:
            out = await self.db_fetchone(
                "SELECT msg,msg_ch FROM awarded WHERE guild=? AND author=? ORDER BY random() LIMIT 1",
                (ctx.guild.id, user.id))
        match out:
            case None: await ctx.send("no starred messages :(")
            case msg_id, msg_ch_id:
                count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
                await ctx.send(**await self.build_message(count, await self.fetch_msg(msg_ch_id,msg_id)))

    @commands.command(description="show a certain starred message")
    async def show(self, ctx:commands.Context, msg:discord.Message|None):
        """show a certain starred message

        :param msg: the message to show. may be given as a reply, or as an ID if in the same channel, or as a jump link.
        """
        match msg, ctx.message.reference:
            case None, None: return await ctx.send("wdym")
            case None, ref:  msg = await self.resolve_ref(ref)  # this COULD fail but realistically it won't
        count, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg.id,))
        await ctx.send(**await self.build_message(count, msg))

    ### ADMIN COMMANDS

    async def printout(self, guild_id):  # brief output of all the settings if no args are given
        match await self.db_fetchone("SELECT minimum,sb,timeout FROM guilds WHERE guild=?", (guild_id,)):
            case minimum, sb_id, None:
                msg = f"starboard channel: <#{sb_id}>\nminimum stars: {minimum}\ntimeout: never"
            case minimum, sb_id, timeout_d:
                msg = f"starboard channel: <#{sb_id}>\nminimum stars: {minimum}\ntimeout: {timeout_d} days"
            case None:
                msg = "unconfigured"
        return msg

    async def set_sb(self, sb: discord.TextChannel, guild_id: int) -> None:
        if sb.guild.id != guild_id: raise ValueError("eat bricks")
        await self.db.execute("INSERT OR REPLACE INTO guilds(sb,guild) VALUES(?,?)", (sb.id, guild_id))
    async def set_minimum(self, minimum: int, guild_id: int) -> None:
        cur = await self.db.execute("UPDATE guilds SET minimum=? WHERE guild=?", (minimum, guild_id))
        if cur.rowcount==0: raise ValueError("no starboard channel set")
    async def set_timeout(self, timeout_d: int, guild_id: int) -> None:
        if timeout_d==0: timeout_d = None
        elif timeout_d<0: raise ValueError("eat bricks")
        cur = await self.db.execute("UPDATE guilds SET timeout=? WHERE guild=?", (timeout_d, guild_id))
        if cur.rowcount==0: raise ValueError("no starboard channel set")

    # here we provide a slash command and a text command with slightly different APIs.

    @app_commands.command(name="starconfig")
    @app_commands.rename(sb="starboard-channel", minimum="minimum-stars", timeout_d="timeout")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_starconfig(self, c:discord.Interaction,
                               sb:discord.TextChannel|None=None, minimum:int|None=None, timeout_d:int|None=None):
        """change starboard configuration like starboard channel or minimum stars.
        if no arguments are given, shows the current configuration.
        if not all arguments are given, the rest will not be modified.

        :param sb: the starboard channel to set. required if configuring for the first time.
        :param minimum: the minimum star count to reach starboard.
        :param timeout_d: timeout period in days. after this period, messages cannot be added to or removed
            from the starboard.
        """
        await self.db.commit()  # just in case, we don't want to rollback earlier stuff
        if sb is None and minimum is None and timeout_d is None:
            return await c.response.send_message(await self.printout(c.guild_id))
        try:
            if sb        is not None: await self.set_sb     (sb,        c.guild_id)
            if minimum   is not None: await self.set_minimum(minimum,   c.guild_id)
            if timeout_d is not None: await self.set_timeout(timeout_d, c.guild_id)
        except ValueError as e:
            await self.db.rollback()
            return await c.response.send_message(e.args[0])

        await self.db.commit()
        await c.response.send_message("ok. new settings:\n" + await self.printout(c.guild_id))

    @commands.command()
    @commands.check_any(commands.has_permissions(manage_channels=True), commands.is_owner())
    async def starconfig(self, ctx:commands.Context, *args):
        """change starboard configuration like starboard channel or minimum stars.

        USAGE: *starconfig [starboard <channel>] [minimum <minimum starcount>] [timeout <days>]

        if no arguments are given, shows the current configuration.
        if not all arguments are given, the rest will not be modified.

        starboard: the starboard channel to set. required if configuring for the first time.
        minimum: the minimum star count to reach starboard.
        timeout: timeout period in days. after this period, messages cannot be added to or removed from the starboard.
        """
        await self.db.commit()  # just in case, we don't want to rollback earlier stuff
        args = [*args]
        if len(args) == 0:
            return await ctx.send(await self.printout(ctx.guild.id))
        try:
            while len(args) > 0:
                match args.pop(0):
                    case "sb" | "starboard" | "starboard-channel":
                        try:
                            sb = await commands.TextChannelConverter().convert(ctx, args.pop(0))
                        except commands.BadArgument: raise ValueError("not a channel")
                        await self.set_sb(sb, ctx.guild.id)
                    case "minimum": await self.set_minimum(int(args.pop(0)), ctx.guild.id)
                    case "timeout": await self.set_timeout(int(args.pop(0)), ctx.guild.id)
                    case x: raise ValueError("what is a "+x)
        except ValueError as e:  # also triggered by the int() conversions
            await self.db.rollback()
            return await ctx.send(e.args[0])
        await self.db.commit()
        await ctx.send("ok. new settings:\n" + await self.printout(ctx.guild.id))

    @commands.command()
    @commands.check_any(commands.has_permissions(manage_channels=True), commands.is_owner())
    async def import_rdanny(self, ctx:commands.Context, sb:discord.TextChannel):
        """imports messages from an R. Danny starboard channel.
        :param sb: the starboard channel in question
        """
        scanned = 0
        mismatches: list[discord.Message] = []
        unparsable: list[discord.Message] = []
        unfindable: list[discord.Message] = []
        async for msg_sb in sb.history(limit=None):
            if msg_sb.author.id != 80528701850124288: continue
            if not (m := re.fullmatch(r".(?: \*\*(\d+)\*\*)? <#(\d+)> ID: (\d+)", msg_sb.content)):
                unparsable.append(msg_sb)
                continue
            count, msg_ch_id, msg_id = int(m[1] or "1"), int(m[2]), int(m[3])
            try:
                msg = await self.fetch_msg(msg_ch_id,msg_id)
            except (discord.NotFound, discord.Forbidden):
                unfindable.append(msg_sb)
                continue
            changes_before = self.db.total_changes
            # get original stars
            if (stars := discord.utils.get(msg.reactions, emoji="⭐")):
                async for starrer in stars.users():
                    if starrer.id == msg.author.id: continue  # cheeky self-star
                    await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,?)",
                        (starrer.id, msg_id, ctx.guild.id, FROM_REACT))
            # get msg_sb stars
            if (stars := discord.utils.get(msg_sb.reactions, emoji="⭐")):
                async for starrer in stars.users():
                    if starrer == msg.author.id: continue
                    await self.db.execute("INSERT OR IGNORE INTO stars(starrer,msg,guild,medium) VALUES(?,?,?,?)",
                        (starrer.id, msg_id, ctx.guild.id, FROM_REACT_SB))
            changes_after = self.db.total_changes
            # ignore stars added by command (hopefully no one did that)
            count_computed, = await self.db_fetchone("SELECT count(*) FROM stars WHERE msg=?", (msg_id,))
            if count != count_computed: mismatches.append(msg_sb)
            logging.warn(f"{count=}, {count_computed=}, {changes_after - changes_before=}")
            # add awarded
            await self.db.execute("INSERT OR IGNORE INTO awarded(msg,msg_sb,msg_ch,guild,author) VALUES(?,?,?,?,?)",
                                  (msg_id, msg_sb.id, msg_ch_id, ctx.guild.id, msg.author.id))
            scanned += 1
        await self.db.commit()
        await ctx.send(f"{scanned} messages added" +
            "\nstar count mismatches: "       *(len(mismatches)!=0) + ", ".join(i.jump_url for i in mismatches) +
            "\nmessages i didn't understand: "*(len(unparsable)!=0) + ", ".join(i.jump_url for i in unparsable) +
            "\nmessages i didn't find: "      *(len(unfindable)!=0) + ", ".join(i.jump_url for i in unfindable) +
            "\nnow you need to unconfigure r.danny and configure asteroid, i think")

    ### ERRORS

    @commands.Cog.listener()
    async def on_command_error(self, ctx:commands.Context, exc:Exception) -> None:
        if   isinstance(exc, commands.MissingRequiredArgument): await ctx.send("can you elaborate")
        elif isinstance(exc, commands.BadArgument):             await ctx.send("wdym")
        elif isinstance(exc, commands.CommandNotFound):         pass
        elif isinstance(exc, commands.CheckFailure):            await ctx.send("wrong alt. bozo")
        else:
            await ctx.send(f"{exc} :(")
            logging.exception(":(", exc_info=exc)

async def setup(bot):
    await bot.db.executescript(SCHEMA)
    await bot.add_cog(Starboard(bot))

if __name__ == "__main__": print("you ran the wrong file. BOZO")
