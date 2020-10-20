import asyncio
import asyncpg
import coc
import discord
import logging
import math

from collections import namedtuple
from datetime import datetime
from discord.ext import commands, tasks

from cogs.utils.db_objects import DatabaseMessage
from cogs.utils.formatters import CLYTable, get_render_type
from cogs.utils import checks


log = logging.getLogger(__name__)

MockPlayer = namedtuple('MockPlayer', 'clan name')
mock = MockPlayer('Unknown', 'Unknown')


class DonationBoard(commands.Cog):
    """Contains all DonationBoard Configurations.
    """
    def __init__(self, bot):
        self.bot = bot

        self.clan_updates = []

        self._to_be_deleted = set()

        self.bot.coc.add_events(
            self.on_clan_member_donation,
            self.on_clan_member_received,
            self.on_clan_member_trophies_change,
            self.on_clan_member_join
                                )
        self.bot.coc._clan_retry_interval = 60
        self.bot.coc.start_updates('clan')

        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._data_batch = {}
        self._clan_events = set()
        self.bulk_insert_loop.add_exception_type(asyncpg.PostgresConnectionError)
        self.bulk_insert_loop.start()

        self.update_board_loops.add_exception_type(asyncpg.PostgresConnectionError)
        self.update_board_loops.add_exception_type(coc.ClashOfClansException)
        self.update_board_loops.start()

    def cog_unload(self):
        self.bulk_insert_loop.cancel()
        self.update_board_loops.cancel()
        self.bot.coc.remove_events(
            self.on_clan_member_donation,
            self.on_clan_member_received,
            self.on_clan_member_trophies_change,
            self.on_clan_member_join
        )

    @tasks.loop(seconds=30.0)
    async def bulk_insert_loop(self):
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=60.0)
    async def update_board_loops(self):
        async with self._batch_lock:
            clan_tags = list(self._clan_events)
            self._clan_events.clear()

        query = """SELECT DISTINCT boards.channel_id
                    FROM boards
                    INNER JOIN clans
                    ON clans.guild_id = boards.guild_id
                    WHERE clans.clan_tag = ANY($1::TEXT[])
                """
        fetch = await self.bot.pool.fetch(query, clan_tags)

        for n in fetch:
            await self.update_board(n['channel_id'])

    async def bulk_insert(self):
        query = """UPDATE players SET donations = players.donations + x.donations, 
                                      received  = players.received  + x.received, 
                                      trophies  = x.trophies
                        FROM(
                            SELECT x.player_tag, x.donations, x.received, x.trophies
                                FROM jsonb_to_recordset($1::jsonb)
                            AS x(player_tag TEXT, 
                                 donations INTEGER, 
                                 received INTEGER, 
                                 trophies INTEGER)
                            )
                    AS x
                    WHERE players.player_tag = x.player_tag
                    AND players.season_id=$2
                """

        query2 = """UPDATE eventplayers SET donations = eventplayers.donations + x.donations, 
                                            received  = eventplayers.received  + x.received,
                                            trophies  = x.trophies   
                        FROM(
                            SELECT x.player_tag, x.donations, x.received, x.trophies
                            FROM jsonb_to_recordset($1::jsonb)
                            AS x(player_tag TEXT, 
                                 donations INTEGER, 
                                 received INTEGER, 
                                 trophies INTEGER)
                            )
                    AS x
                    WHERE eventplayers.player_tag = x.player_tag
                    AND eventplayers.live = true                    
                """
        if self._data_batch:
            response = await self.bot.pool.execute(query, list(self._data_batch.values()),
                                                   await self.bot.seasonconfig.get_season_id())
            log.debug(f'Registered donations/received to the database. Status Code {response}.')

            response = await self.bot.pool.execute(query2, list(self._data_batch.values()))
            log.debug(f'Registered donations/received to the events database. Status Code {response}.')
            self._data_batch.clear()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if not isinstance(channel, discord.TextChannel):
            return

        query = """DELETE FROM messages WHERE channel_id = $1;
                   UPDATE boards
                   SET channel_id = NULL,
                       toggle     = False
                   WHERE channel_id = $1;
                """
        await self.bot.pool.execute(query, channel.id)
        self.bot.utils.board_config.invalidate(self.bot.utils, channel.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        config = await self.bot.utils.board_config(payload.channel_id)

        if not config:
            return
        if config.channel_id != payload.channel_id:
            return
        if payload.message_id in self._to_be_deleted:
            self._to_be_deleted.discard(payload.message_id)
            return

        self.bot.utils.get_message.invalidate(self.bot.utils, payload.message_id)

        message = await self.safe_delete(message_id=payload.message_id, delete_message=False)
        if message:
            await self.new_board_message(payload.channel_id, config.type)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        config = await self.bot.utils.board_config(payload.channel_id)

        if not config:
            return
        if config.channel_id != payload.channel_id:
            return

        for n in payload.message_ids:
            if n in self._to_be_deleted:
                self._to_be_deleted.discard(n)
                continue

            self.bot.utils.get_message.invalidate(self, n)

            message = await self.safe_delete(message_id=n, delete_message=False)
            if message:
                await self.new_board_message(payload.channel_id, config.type)

    async def on_clan_member_donation(self, old_donations, new_donations, player, clan):
        log.debug(f'Received on_clan_member_donation event for player {player} of clan {clan}')
        if old_donations > new_donations:
            donations = new_donations
        else:
            donations = new_donations - old_donations

        async with self._batch_lock:
            try:
                self._data_batch[player.tag]['donations'] = donations
            except KeyError:
                self._data_batch[player.tag] = {
                    'player_tag': player.tag,
                    'donations': donations,
                    'received': 0,
                    'trophies': player.trophies
                }
            self._clan_events.add(clan.tag)

    async def on_clan_member_received(self, old_received, new_received, player, clan):
        log.debug(f'Received on_clan_member_received event for player {player} of clan {clan}')
        if old_received > new_received:
            received = new_received
        else:
            received = new_received - old_received

        async with self._batch_lock:
            try:
                self._data_batch[player.tag]['received'] = received
            except KeyError:
                self._data_batch[player.tag] = {
                    'player_tag': player.tag,
                    'donations': 0,
                    'received': received,
                    'trophies': player.trophies
                }
            self._clan_events.add(clan.tag)

    async def on_clan_member_trophies_change(self, _, new_trophies, player, clan):
        log.debug(f'Received on_clan_member_trophy_change event for player {player} of clan {clan}.')

        async with self._batch_lock:
            try:
                self._data_batch[player.tag]['trophies'] = new_trophies
            except KeyError:
                self._data_batch[player.tag] = {
                    'player_tag': player.tag,
                    'donations': 0,
                    'received': 0,
                    'trophies': new_trophies
                }
            self._clan_events.add(clan.tag)

    async def on_clan_member_join(self, member, clan):
        player = await self.bot.coc.get_player(member.tag)
        player_query = """INSERT INTO players (
                                        player_tag, 
                                        donations, 
                                        received, 
                                        trophies, 
                                        start_trophies, 
                                        season_id,
                                        start_friend_in_need,
                                        start_sharing_is_caring,
                                        start_attacks,
                                        start_defenses,
                                        start_trophies,
                                        start_best_trophies,
                                        start_update
                                        ) 
                    VALUES ($1,$2,$3,$4,$4,$5,$6,$7,$8,$9,$10,$11,$12) 
                    ON CONFLICT (player_tag, season_id) 
                    DO NOTHING
                """

        response = await self.bot.pool.execute(
            player_query,
            player.tag,
            player.donations,
            player.received,
            player.trophies,
            await self.bot.seasonconfig.get_season_id(),
            player.achievements_dict['Friend in Need'].value,
            player.achievements_dict['Sharing is caring'].value,
            player.attack_wins,
            player.defense_wins,
            player.trophies,
            player.best_trophies
        )
        log.debug(f'New member {member} joined clan {clan}. Performed a query to insert them into players. '
                  f'Status Code: {response}')

        event_query = """INSERT INTO eventplayers (
                                            player_tag,
                                            trophies,
                                            event_id,
                                            start_friend_in_need,
                                            start_sharing_is_caring,
                                            start_attacks,
                                            start_defenses,
                                            start_trophies,
                                            start_best_trophies,
                                            start_update,
                                            live
                                            )
                            SELECT $1, $2, events.id, $3, $4, $5, $6, $7, $8, True, True
                            FROM events
                            WHERE finish >= now()
                            AND start <= now()
                            ON CONFLICT (player_tag, event_id)
                            DO NOTHING;
                        """

        response = await self.bot.pool.execute(
            event_query,
            player.tag,
            player.trophies,
            player.achievements_dict['Friend in Need'].value,
            player.achievements_dict['Sharing is caring'].value,
            player.attack_wins,
            player.defense_wins,
            player.trophies,
            player.best_trophies
          )

        log.debug(f'New member {member} joined clan {clan}. '
                  f'Performed a query to insert them into eventplayers. Status Code: {response}')

    async def new_board_message(self, channel, board_type):
        if not channel:
            return

        try:
            new_msg = await channel.send('Placeholder')
        except (discord.NotFound, discord.Forbidden):
            return

        query = "INSERT INTO messages (guild_id, message_id, channel_id) VALUES ($1, $2, $3)"
        await self.bot.pool.execute(query, new_msg.guild.id, new_msg.id, new_msg.channel.id)

        event_config = await self.bot.utils.event_config(channel.id)
        if event_config:
            await self.bot.background.remove_event_msg(event_config.id, channel, board_type)
            await self.bot.background.new_event_message(event_config, channel.guild.id, channel.id, board_type)

        return new_msg

    async def safe_delete(self, message_id, delete_message=True):
        query = "DELETE FROM messages WHERE message_id = $1 RETURNING id, guild_id, message_id, channel_id"
        fetch = await self.bot.pool.fetchrow(query, message_id)
        if not fetch:
            return None

        message = DatabaseMessage(bot=self.bot, record=fetch)
        if not delete_message:
            return message

        self._to_be_deleted.add(message_id)
        m = await message.get_message()
        if not m:
            return

        await m.delete()

    async def get_board_messages(self, channel_id, number_of_msg=None):
        config = await self.bot.utils.board_config(channel_id)
        if not (config.channel or config.toggle):
            return

        fetch = await config.messages()

        messages = [await n.get_message() for n in fetch if await n.get_message()]
        size_of = len(messages)

        if not number_of_msg or size_of == number_of_msg:
            return messages

        if size_of > number_of_msg:
            for n in messages[number_of_msg:]:
                await self.safe_delete(n.id)
            return messages[:number_of_msg]

        if not config.channel:
            return

        for _ in range(number_of_msg - size_of):
            m = await self.new_board_message(config.channel, config.type)
            if not m:
                return
            messages.append(m)

        return messages

    async def get_top_players(self, players, board_type, in_event):
        if board_type == 'donation':
            column_1 = 'donations'
            column_2 = 'received'
        elif board_type == 'trophy':
            column_1 = 'trophies'
            column_2 = 'trophies - start_trophies'
        else:
            return

        # this should be ok since columns can only be a choice of 4 defined names
        if in_event:
            query = f"""SELECT player_tag, {column_1}, {column_2} 
                        FROM eventplayers 
                        WHERE player_tag=ANY($1::TEXT[])
                        AND live=true
                        ORDER BY {column_1} DESC
                        LIMIT 100;
                    """
            fetch = await self.bot.pool.fetch(query, [n.tag for n in players])

        else:
            query = f"""SELECT player_tag, {column_1}, {column_2}
                        FROM players 
                        WHERE player_tag=ANY($1::TEXT[])
                        AND season_id=$2
                        ORDER BY {column_1} DESC
                        LIMIT 100;
                    """
            fetch = await self.bot.pool.fetch(query, [n.tag for n in players],
                                              await self.bot.seasonconfig.get_season_id())
        return fetch

    async def update_board(self, channel_id):
        config = await self.bot.utils.board_config(channel_id)

        if not config:
            return
        if not config.toggle:
            return
        if not config.channel:
            return

        if config.in_event:
            query = """SELECT DISTINCT clan_tag FROM clans WHERE guild_id=$1 AND in_event=$2"""
            fetch = await self.bot.pool.fetch(query, config.guild_id, config.in_event)
        else:
            query = "SELECT DISTINCT clan_tag FROM clans WHERE guild_id=$1"
            fetch = await self.bot.pool.fetch(query, config.guild_id)

        clans = await self.bot.coc.get_clans((n[0] for n in fetch)).flatten()

        players = []
        for n in clans:
            players.extend(p for p in n.itermembers)

        top_players = await self.get_top_players(players, config.type, config.in_event)
        players = {n.tag: n for n in players if n.tag in set(x['player_tag'] for x in top_players)}

        message_count = math.ceil(len(top_players) / 20)

        messages = await self.get_board_messages(channel_id, number_of_msg=message_count)
        if not messages:
            return

        for i, v in enumerate(messages):
            player_data = top_players[i*20:(i+1)*20]
            table = CLYTable()

            for x, y in enumerate(player_data):
                index = i*20 + x
                if config.render == 2:
                    table.add_row([index,
                                   y[1],
                                   players.get(y['player_tag'], mock).name])
                else:
                    table.add_row([index,
                                   y[1],
                                   y[2],
                                   players.get(y['player_tag'], mock).name])

            render = get_render_type(config, table)
            fmt = render()

            e = discord.Embed(colour=self.get_colour(config.type, config.in_event),
                              description=fmt,
                              timestamp=datetime.utcnow()
                              )
            e.set_author(name=f'Event in Progress!' if config.in_event
                              else config.title,
                         icon_url=config.icon_url or 'https://cdn.discordapp.com/'
                                                     'emojis/592028799768592405.png?v=1')
            e.set_footer(text='Last Updated')
            await v.edit(embed=e, content=None)

    @staticmethod
    def get_colour(board_type, in_event):
        if board_type == 'donation':
            if in_event:
                return discord.Colour.gold()
            return discord.Colour.blue()
        if in_event:
            return discord.Colour.purple()
        return discord.Colour.green()

    @commands.command(hidden=True)
    @commands.is_owner()
    async def forceboard(self, ctx, channel_id: int = None):
        await self.update_board(channel_id or ctx.channel.id)
        await ctx.confirm()


def setup(bot):
    bot.add_cog(DonationBoard(bot))
