asteroid is a starboard bot. It is very cool

It posts messages that go over an amount of star reactions to a channel called the starboard. It then listens to more
stars being added to the message has and updates that message with a slightly yellower shade of yellow.

Commands can be used to query information from the starboard. See a list with `*help`

## details

The bot tracks all stars that happen in the server. Stars may be added through reactions or the context menu item 
"☆ Star", either to a message or to its repost in the starboard (if any). Stars will be ignored, and perhaps even
deleted, if the starrer sent the original message (self-star), the starrer starred the message already or the channel
is in a channel or thread with the word _cw_ in the name. Stars can also be removed by deleting the reaction or using
the context menu item "☆ Unstar". Note that a star added by reaction must be removed by unreacting, not the menu item.

This bot is built to work in multiple guilds. As such, each guild has its own configuration, configured with
`*starconfig`: the _starboard_ channel (#starboard), the _minimum_ star count (default: 3), and the _timeout_ (default:
7 days).

After a message reaches _minimum_ stars, provided the message was posted sooner than _timeout_ days, the message will
be sent in the starboard. The reposted message can also receive star reactions, which are redirected to the original
message. The reposted message is updated when stars are added or removed. If the star count goes below _minimum_, and
the message was posted sooner than _timeout_ days, the message will be removed from the starboard. This means that a
message's starboardhood is forever solidified after the timeout passes. And after the timeout the star count can go
below the minimum (dubious)

Not quite forever. When the original message is deleted, the stored stars and the repost go with it. When the repost is
deleted (by a mod presumably), the message is banished from ever appearing again in the starboard. When a message is
edited its edit doesn't immediately go through, but it's enough with a star/unstar to update it. The bot doesn't store
any contents of messages, only relationships between user/message IDs.

The reposted message has a jump link to the original message and an embed. The bot tries to add embeds and replies to
the message as well. Notably it doesn't have a star count. But it has a star that changes shape! and even an embed color
that enyellows.

The bot was developed to be integrated into a server that already had a starboard bot. So it has a command to import
stars from R.Danny starred messages. These messages and their stars are still managed by asteroid, though the starboard
messages will not replaced or updated. The method for importing isn't perfect: it relies on user reactions on the
messages for the stars, often different from the number shown in the message (I think it doesn't track reacttions after
a certain time).

The bot requires Python 3.10 or higher (NOT TRUE!!! todo replace autocommit=False with whatever the alternative used to
be). It uses [discord.py](https://github.com/Rapptz/discord.py/) and [aiosqlite](https://github.com/omnilib/aiosqlite)
(see requirements.txt). The database is stored at `bees.db`, and the token in a text file called `token`.