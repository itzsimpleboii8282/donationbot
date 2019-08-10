import asyncio
import asyncpg
import coc
import discord
import logging
import math
import time
import typing

from datetime import datetime
from discord.ext import commands, tasks
from cogs.utils.converters import ClanConverter, PlayerConverter
from cogs.utils import formatters, emoji_lookup, checks
from cogs.utils.db_objects import DatabaseEvent, DatabaseClan

log = logging.getLogger(__name__)


class Events(commands.Cog):
    """Find historical clan donation data for your clan, or setup logging and events.
    """
    def __init__(self, bot):
        self.bot = bot
        self._batch_data = []
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self.batch_insert_loop.add_exception_type(asyncpg.PostgresConnectionError)
        self.batch_insert_loop.start()
        self.report_task.add_exception_type(asyncpg.PostgresConnectionError)
        self.report_task.start()
        self.check_for_timers_task = self.bot.loop.create_task(self.check_for_timers())

        self.bot.coc.add_events(
            self.on_clan_member_donation,
            self.on_clan_member_received
        )
        self.bot.coc._clan_retry_interval = 60
        self.bot.coc.start_updates('clan')

        self.channel_config_cache = {}

    async def cog_command_error(self, ctx, error):
        await ctx.send(str(error))

    def cog_unload(self):
        self.report_task.cancel()
        self.batch_insert_loop.cancel()
        self.check_for_timers_task.cancel()
        self.bot.coc.remove_events(
            self.on_clan_member_donation,
            self.on_clan_member_received
        )

    @tasks.loop(seconds=30.0)
    async def batch_insert_loop(self):
        async with self._batch_lock:
            await self.bulk_insert()

    async def bulk_insert(self):
        query = """INSERT INTO events (player_tag, player_name, clan_tag, donations, received, time, season_id)
                        SELECT x.player_tag, x.player_name, x.clan_tag, x.donations, x.received, x.time, x.season_id
                           FROM jsonb_to_recordset($1::jsonb) 
                        AS x(player_tag TEXT, player_name TEXT, clan_tag TEXT, 
                             donations INTEGER, received INTEGER, time TIMESTAMP, season_id INTEGER
                             )
                """

        if self._batch_data:
            await self.bot.pool.execute(query, self._batch_data)
            total = len(self._batch_data)
            if total > 1:
                log.info('Registered %s events to the database.', total)
            self._batch_data.clear()

    def dispatch_log(self, channel_id, interval, fmt):
        seconds = interval.total_seconds()
        if seconds > 0:
            if seconds < 600:
                self.bot.loop.create_task(self.short_timer(interval.total_seconds(),
                                                           channel_id, fmt
                                                           )
                                          )
            else:
                asyncio.ensure_future(self.create_new_timer(channel_id, fmt,
                                      datetime.utcnow() + interval
                                                            )
                                      )
        else:
            log.info('Dispatching a log to channel ID %s', channel_id)
            asyncio.ensure_future(self.bot.channel_log(channel_id, fmt, embed=False))

    @tasks.loop(seconds=30.0)
    async def report_task(self):
        log.info('Starting bulk report loop.')
        start = time.perf_counter()
        async with self._batch_lock:
            await self.bulk_report()
        log.info('Time taken: %s ms', (time.perf_counter() - start)*1000)

    async def bulk_report(self):
        query = """SELECT DISTINCT clans.channel_id 
                   FROM clans 
                        INNER JOIN events 
                        ON clans.clan_tag = events.clan_tag 
                    WHERE events.reported=False
                """
        fetch = await self.bot.pool.fetch(query)

        query = """SELECT events.clan_tag, events.donations, events.received, 
                          events.player_name, clans.log_interval
                    FROM events 
                        INNER JOIN clans 
                        ON clans.clan_tag = events.clan_tag 
                    WHERE clans.channel_id=$1 
                    AND events.reported=False
                    ORDER BY events.clan_tag, 
                             time DESC;
                """
        for n in fetch:
            channel_config = await self.bot.get_channel_config(n[0])
            if not channel_config:
                continue
            if not channel_config.log_toggle:
                continue

            events = [DatabaseEvent(bot=self.bot, record=n) for
                      n in await self.bot.pool.fetch(query, n[0])
                      ]

            messages = []
            for x in events:
                clan_name = await self.bot.donationboard.get_clan_name(channel_config.guild_id,
                                                                       x.clan_tag)
                messages.append(formatters.format_event_log_message(x, clan_name))

            group_batch = []
            for i in range(math.ceil(len(messages) / 20)):
                group_batch.append(messages[i*20:(i+1)*20])

            for x in group_batch:
                interval = channel_config.log_interval - events[0].delta_since
                self.dispatch_log(channel_config.channel_id, interval, '\n'.join(x))

            log.info('Dispatched logs for %s (guild %s)', channel_config.channel or 'Not found',
                     channel_config.guild or 'No guild')

        query = """UPDATE events
                        SET reported=True
                   WHERE reported=False
                """
        removed = await self.bot.pool.execute(query)
        log.info('Removed events from the database. Status Code %s', removed)

    async def short_timer(self, seconds, channel_id, fmt):
        await asyncio.sleep(seconds)
        await self.bot.channel_log(channel_id, fmt, embed=False)
        log.info('Sent a log to channel ID: %s after sleeping for %s', channel_id, seconds)

    async def check_for_timers(self):
        try:
            while not self.bot.is_closed():
                query = "SELECT id, expires, channel_id, fmt FROM log_timers ORDER BY expires LIMIT 1;"
                fetch = await self.bot.pool.fetchrow(query)
                if not fetch:
                    continue

                now = datetime.utcnow()
                if fetch['expires'] >= now:
                    to_sleep = (fetch['expires'] - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.bot.channel_log(fetch['channel_id'], fetch['fmt'], embed=False)
                log.info('Sent a log to channel ID: %s which had been saved to DB.',
                         fetch['channel_id'])

                query = "DELETE FROM log_timers WHERE id=$1;"
                await self.bot.pool.execute(query, fetch['id'])
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self.create_new_timer_task.cancel()
            self.create_new_timer_task = self.bot.loop.create_task(self.check_for_timers())

    async def create_new_timer(self, channel_id, fmt, expires):
        query = "INSERT INTO log_timers (channel_id, fmt, expires) VALUES ($1, $2, $3)"
        await self.bot.pool.execute(query, channel_id, fmt, expires)

    async def on_clan_member_donation(self, old_donations, new_donations, player, clan):
        if old_donations > new_donations:
            donations = new_donations
        else:
            donations = new_donations - old_donations

        async with self._batch_lock:
            self._batch_data.append({
                'player_tag': player.tag,
                'player_name': player.name,
                'clan_tag': clan.tag,
                'donations': donations,
                'received': 0,
                'time': datetime.utcnow().isoformat(),
                'season_id': await self.bot.seasonconfig.get_season_id()
            })

    async def on_clan_member_received(self, old_received, new_received, player, clan):
        if old_received > new_received:
            received = new_received
        else:
            received = new_received - old_received

        async with self._batch_lock:
            self._batch_data.append({
                'player_tag': player.tag,
                'player_name': player.name,
                'clan_tag': clan.tag,
                'donations': 0,
                'received': received,
                'time': datetime.utcnow().isoformat(),
                'season_id': await self.bot.seasonconfig.get_season_id()
            })

    async def get_channel_config(self, channel_id):
        config = self.channel_config_cache.get(channel_id)
        if config:
            return config

        query = """SELECT id, guild_id, clan_tag, clan_name, 
                          channel_id, log_interval, log_toggle 
                    FROM clans WHERE channel_id=$1
                """
        fetch = await self.bot.pool.fetchrow(query, channel_id)

        if not fetch:
            return None

        clan = DatabaseClan(bot=self.bot, record=fetch)
        self.channel_config_cache[channel_id] = clan

        return clan

    def invalidate_channel_config(self, channel_id):
        self.channel_config_cache.pop(channel_id, None)

    @commands.group(invoke_without_subcommand=True)
    @checks.manage_guild()
    async def log(self, ctx):
        """Manage the donation log for the server.

        Required Perimssions
        ----------------------------
        • `manage_server` permissions
        """
        if ctx.invoked_subcommand is None:
            return await ctx.send_help(ctx.command)

    @log.command(name='info')
    async def log_info(self, ctx, *, channel: discord.TextChannel = None):
        """Get information about log channels for the guild.

        Parameters
        ----------------
            • Channel: Optional, the channel to get log info for.
                       Defaults to all channels in the server.

        Example
        -----------
        • `+log info #channel`
        • `+log info`

        Required Perimssions
        ----------------------------
        • `manage_server` permissions
        """
        if channel:
            query = """SELECT clan_tag, channel_id, log_toggle, 
                              log_interval, clan_name 
                       FROM clans 
                       WHERE channel_id=$1
                    """
            fetch = await ctx.db.fetch(query, channel.id)
        else:
            query = """SELECT clan_tag, channel_id, log_toggle, 
                              log_interval, clan_name 
                       FROM clans 
                       WHERE guild_id=$1
                    """
            fetch = await ctx.db.fetch(query, ctx.guild.id)

        if channel:
            fmt = channel.mention
        else:
            fmt = ctx.guild.name

        e = discord.Embed(color=self.bot.colour,
                          description=f'Log info for {fmt}')

        for n in fetch:
            config = DatabaseClan(bot=self.bot, record=n)
            fmt = f"Tag: {config.clan_tag}\n"
            fmt += f"Channel: {config.channel.mention if config.channel else 'None'}\n"
            fmt += f"Log Toggle: {'enabled' if config.log_toggle else 'disabled'}\n"
            fmt += f"Log Interval: {config.interval_seconds} seconds\n"
            e.add_field(name=n['clan_name'],
                        value=fmt)
        await ctx.send(embed=e)

    @log.command(name='interval')
    async def log_interval(self, ctx, channel: typing.Optional[discord.TextChannel] = None,
                           minutes: int = 1):
        """Update the interval (in minutes) for which the bot will log your donations.

        Parameters
        ----------------
            • Channel: Optional, the channel to change log interval for.
                       Defaults to the one you're in.
            • Minutes: the number of minutes between logs. Defaults to 1.

        Example
        -----------
        • `+log interval #channel 2`
        • `+log interval 1440`

        Required Perimssions
        ----------------------------
        • `manage_server` permissions
        """
        if not channel:
            channel = ctx.channel

        query = """UPDATE clans SET log_interval = ($1 ||' minutes')::interval
                    WHERE channel_id=$2
                    RETURNING clan_name
                """
        fetch = await ctx.db.fetch(query, str(minutes), channel.id)

        if not fetch:
            return await ctx.send('Please add a clan.')

        await ctx.confirm()
        fmt = '\n'.join(n[0] for n in fetch)
        await ctx.send(f'Set log interval to {minutes} minutes for {fmt}.')
        self.invalidate_channel_config(channel.id)

    @log.command(name='create')
    async def log_create(self, ctx, channel: typing.Optional[discord.TextChannel] = None, *,
                         clan: ClanConverter):
        """Create a donation log for your server.

        Parameters
        ----------------

            • Channel: #channel or a channel id. This defaults to the channel you are in.
            • Clan: clan tag or name to set logs for.

        Example
        -----------
        • `+log create #CHANNEL #P0LYJC8C`
        • `+log create #P0LYJC8C`

        Required Perimssions
        ----------------------------
        • `manage_server` permissions
        """
        if not channel:
            channel = ctx.channel
        if not (channel.permissions_for(ctx.me).send_messages or channel.permissions_for(
                ctx.me).read_messages):
            return await ctx.send('I need permission to send and read messages here!')

        query = "UPDATE clans SET channel_id=$1, log_toggle=True WHERE clan_tag=$2 AND guild_id=$3"
        await ctx.db.execute(query, channel.id, clan[0].tag, ctx.guild.id)
        await ctx.send(f'Events log channel has been set to {channel.mention} for {clan[0].name} '
                       f'and logging is enabled.')
        await ctx.confirm()
        self.invalidate_channel_config(channel.id)

    @log.command(name='toggle')
    async def log_toggle(self, ctx, channel: discord.TextChannel = None):
        """Toggle the donation log on/off for your server.

        Parameters
        ----------------
        Pass in any of the following:

            • Channel: #channel or a channel id. This defaults to the channel you are in.

        Example
        -----------
        • `+log toggle #CHANNEL`
        • `+log toggle`

        Required Perimssions
        ----------------------------
        • `manage_server` permissions
        """
        if not channel:
            channel = ctx.channel
        config = await self.get_channel_config(channel.id)
        if not config:
            return await ctx.send('Please setup a log channel with `+help log create` first.')

        toggle = not config.log_toggle  # false if true else false - opposite to what it is now.

        query = "UPDATE clans SET log_toggle=$1 WHERE channel_id=$2 RETURNING clan_name"
        fetch = await ctx.db.fetch(query, toggle, channel.id)

        if not fetch:
            return await ctx.send('Please add a clan.')

        fmt = '\n'.join(n[0] for n in fetch)
        await ctx.send(f'Events logging has been {"enabled" if toggle else "disabled"} for {fmt}')
        await ctx.confirm()
        self.invalidate_channel_config(channel.id)

    @commands.group(invoke_without_command=True)
    async def events(self, ctx, limit: typing.Optional[int] = 20, *,
                     arg: typing.Union[discord.Member, ClanConverter, PlayerConverter] = None):
        """Check recent donation events for a player, user, clan or guild.

        Parameters
        ----------------
        • Optional: Pass in a limit (number of events) to get. Defaults to 20.

        Then pass in any of the following:

            • A clan tag
            • A clan name (clan must be claimed to the server)
            • A discord @mention, user#discrim or user id
            • A player tag
            • A player name (must be in clan claimed to server)
            • `all`, `server`, `guild` for all clans in guild
            • None passed will divert to donations for your discord account

        Example
        -----------
        • `+events 20 #CLAN_TAG`
        • `+events @mention`
        • `+events #PLAYER_TAG`
        • `+events player name`
        • `+events 1000 all`
        • `+events`
        """
        if ctx.invoked_subcommand is not None:
            return

        if not arg:
            arg = limit

        if isinstance(arg, int):
            await ctx.invoke(self.events_recent, limit=arg)
        elif isinstance(arg, coc.BasicPlayer):
            await ctx.invoke(self.events_player, player=arg, limit=limit)
        elif isinstance(arg, discord.Member):
            await ctx.invoke(self.events_user, user=arg, limit=limit)
        elif isinstance(arg, list):
            if isinstance(arg[0], coc.Clan):
                await ctx.invoke(self.events_clan, clans=arg, limit=limit)

    @events.command(name='recent', hidden=True)
    async def events_recent(self, ctx, limit: int = None):
        query = """SELECT player_tag, donations, received, time, player_name
                    FROM events
                    WHERE events.clan_tag = ANY(
                                SELECT DISTINCT clan_tag FROM clans
                                WHERE guild_id=$1
                                )
                    ORDER BY events.time DESC
                    LIMIT $2
                """
        fetch = await ctx.db.fetch(query, ctx.guild.id, limit)
        if not fetch:
            return await ctx.send('No events found. Please ensure you have '
                                  'enabled logging and have claimed a clan.')

        no_pages = math.ceil(len(fetch) / 20)
        title = f"Recent Events for Guild {ctx.guild.name}"

        p = formatters.EventsPaginator(ctx, fetch, page_count=no_pages, title=title)
        await p.paginate()

    @events.command(name='user', hidden=True)
    async def events_user(self, ctx, limit: typing.Optional[int] = 20, *,
                          user: discord.Member = None):
        """Get donation history/events for a discord user.

        Parameters
        ----------------
        Pass in any of the following:

            • A discord @mention, user#discrim or user id
            • None passed will divert to donations for your discord account

        Example
        ------------
        • `+events user @mention`
        • `+events user USER_ID`
        • `+events user`

        By default, you shouldn't need to call these sub-commands as the bot will
        parse your argument and direct it to the correct sub-command automatically.
        """
        if not user:
            user = ctx.author

        query = """SELECT events.player_tag, events.donations, events.received, events.time, events.player_name
                    FROM events 
                        INNER JOIN players
                        ON events.player_tag = players.player_tag 
                    WHERE players.user_id = $1 
                    ORDER BY time DESC 
                    LIMIT $2;
                """
        fetch = await ctx.db.fetch(query, user.id, limit)
        if not fetch:
            return await ctx.send(f'No events found.')

        title = f'Recent Events for {str(user)}'
        no_pages = math.ceil(len(fetch) / 20)

        p = formatters.EventsPaginator(ctx, data=fetch, title=title, page_count=no_pages)
        await p.paginate()

    @events.command(name='player', hidden=True)
    async def events_player(self, ctx, limit: typing.Optional[int] = 20,
                            *, player: PlayerConverter):
        """Get donation history/events for a player.

        Parameters
        -----------------
        Pass in any of the following:

            • A player tag
            • A player name (must be in a clan claimed to server)

        Example
        ------------
        • `+events player #PLAYER_TAG`
        • `+events player player name`

        Aliases
        -----------
        • `+events player` (primary)
        • `+events player`

        By default, you shouldn't need to call these sub-commands as the bot will
        parse your argument and direct it to the correct sub-command automatically.
        """
        query = """SELECT player_tag, donations, received, time, player_name
                    FROM events 
                    WHERE player_tag = $1 
                    ORDER BY time DESC 
                    LIMIT $2
                """
        fetch = await ctx.db.fetch(query, player.tag, limit)
        if not fetch:
            return await ctx.send('Account has not been added/claimed.')

        title = f'Recent Events for {player.name}'

        no_pages = math.ceil(len(fetch) / 20)

        p = formatters.EventsPaginator(ctx, data=fetch, title=title, page_count=no_pages)
        await p.paginate()

    @events.command(name='clan', hidden=True)
    async def events_clan(self, ctx, limit: typing.Optional[int] = 20, *, clans: ClanConverter):
        """Get donation history/events for a clan.

        Parameters
        ----------------
        Pass in any of the following:

            • A clan tag
            • A clan name (must be claimed to server)
            • `all`, `server`, `guild`: all clans claimed to server

        Example
        ------------
        • `+events clan #CLAN_TAG`
        • `+events clan clan name`
        • `+events clan all`

        Aliases
        -----------
        • `+events clan` (primary)
        • `+events clan`

        By default, you shouldn't need to call these sub-commands as the bot will
        parse your argument and direct it to the correct sub-command automatically.
        """

        query = """SELECT player_tag, donations, received, time, player_name
                        FROM events
                    WHERE clan_tag = ANY($1::TEXT[])
                    ORDER BY time DESC 
                    LIMIT $2
                """
        fetch = await ctx.db.fetch(query, list(set(n.tag for n in clans)), limit)
        if not fetch:
            return await ctx.send('No events found.')

        title = f"Recent Events for {', '.join(n.name for n in clans)}"
        no_pages = math.ceil(len(fetch) / 20)

        p = formatters.EventsPaginator(ctx, data=fetch, title=title, page_count=no_pages)
        await p.paginate()


def setup(bot):
    bot.add_cog(Events(bot))
