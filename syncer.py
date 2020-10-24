import asyncio
import datetime
import logging
import time
import itertools
import math
import copy
import io

import aiohttp
import coc
import discord
import sentry_sdk

from discord.ext import commands, tasks
from collections import Counter
from matplotlib import pyplot as plt

import creds

from botlog import setup_logging
from cogs.utils.db import Table
from cogs.utils.donationtrophylogs import SlimDonationEvent2, SlimTrophyEvent, get_basic_log, get_detailed_log, format_trophy_log_message
from cogs.utils.db_objects import LogConfig

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
sentry_sdk.init(creds.SENTRY_KEY)


class CustomClanMember(coc.ClanMember):
    def _from_data(self, data: dict) -> None:
        data_get = data.get
        self.exp_level = data_get("expLevel")
        self.trophies = data_get("trophies")
        self.versus_trophies = data_get("versusTrophies")
        self.donations = data_get("donations")
        self.received = data_get("donationsReceived")
        self.league_id = data_get("league", {}).get("id", None)


class CustomClan(coc.Clan):
    def _from_data(self, data: dict) -> None:
        client = self._client
        self._members = {m['tag']: CustomClanMember(data=m, client=client) for m in data.get("memberList", [])}


coc_client = coc.login(creds.email, creds.password, client=coc.EventsClient, key_names="test2", throttle_limit=30, key_count=3, scopes=creds.scopes, cache_max_size=None)
coc_client.clan_cls = CustomClan
bot = commands.Bot(command_prefix="+")
bot.session = aiohttp.ClientSession()
pool = asyncio.get_event_loop().run_until_complete(Table.create_pool(creds.postgres))
setup_logging(bot)


class Syncer:
    def __init__(self):
        loop = asyncio.get_event_loop()

        self.season_id = None
        loop.create_task(self.get_season_id())

        self.board_batch_lock = asyncio.Lock(loop=loop)
        self.board_batch_data = {}

        self.donationlog_batch_lock = asyncio.Lock(loop=loop)
        self.donationlog_batch_data = []

        self.trophylog_batch_lock = asyncio.Lock(loop=loop)
        self.trophylog_batch_data = []

        self.last_updated_batch_lock = asyncio.Lock(loop=loop)
        self.last_updated_tags = set()
        self.last_updated_counter = Counter()

        self.boards_counter = Counter()

    async def get_season_id(self):
        fetch = await pool.fetchrow("SELECT id FROM seasons WHERE start < now() ORDER BY start DESC;")
        self.season_id = fetch['id']

    def start(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self.fetch_webhooks())
        print("STARTING")

        listeners = (
            self.season_start,
            self.send_donationlog_events,
            self.on_clan_member_donation,
            self.on_clan_member_received,
            self.send_trophylog_events,
            self.on_clan_member_trophies_change,
            self.on_member_update,
            self.on_clan_member_join,
            self.on_clan_member_leave,
            self.maintenance_start,
            self.maintenance_completed,
        )
        coc_client.add_events(*listeners)

        self.tasks = (
            self.update_clan_tags,
            self.last_updated_loop,
            self.board_insert_loop,
            self.set_legend_trophies,
            # self.send_stats,
        )
        for task in self.tasks:
            task.error(self.task_error)
            task.start()

        try:
            loop.run_forever()
        finally:
            for task in self.tasks:
                task.cancel()
            coc_client.close()

    async def task_error(self, exception):
        await self.safe_send(594286547449282587, str(exception))
        for task in self.tasks:
            if task.failed():
                task.restart()

    # @coc_client.event
    @coc.ClientEvents.event_error()
    async def on_event_error(self, exception):
        log.exception("event failed", exc_info=exception)

    # @coc_client.event
    @coc.ClientEvents.new_season_start()
    async def season_start(self):
        await self.safe_send(594286547449282587, "New season has started!")

        fetch = await pool.fetchrow(
            "INSERT INTO seasons (start, finish) VALUES ($1, $2) RETURNING id",
            coc.utils.get_season_start(),
            coc.utils.get_season_end()
        )

        self.season_id = fetch['id']

        query = """INSERT INTO players (
                            player_tag,
                            donations,
                            received,
                            user_id,
                            season_id,
                            player_name,
                            start_trophies
                            )
                    SELECT player_tag,
                           0,
                           0,
                           user_id,
                           season_id + 1,
                           player_name,
                           trophies
                    FROM players
                    WHERE season_id = $1
                """
        await pool.execute(query, self.season_id - 1)
        await pool.execute("UPDATE players SET start_trophies = 5000 WHERE start_trophies > 5000 AND season_id = $1", self.season_id)
        await self.safe_send(594286547449282587, "Syncer has added players :ok_hand:")

    async def add_temp_events(self, log_type, channel_id, fmt):
        query = """INSERT INTO tempevents (channel_id, fmt, type) VALUES ($1, $2, $3)"""
        await pool.execute(query, channel_id, fmt, log_type)
        log.debug(f'Added a message for channel id {channel_id} to tempevents db')

    async def add_detailed_temp_events(self, channel_id, clan_tag, events):
        query = "INSERT INTO detailedtempevents (channel_id, clan_tag, exact, combo, unknown) VALUES ($1, $2, $3, $4, $5)"
        await pool.execute(
            query,
            channel_id,
            clan_tag,
            "\n".join(events["exact"]),
            "\n".join(events["combo"]),
            "\n".join(events["unknown"])
        )
        log.debug(f'Added detailed temp events for channel id {channel_id} clan tag {clan_tag} to detailedtempevents db\n{events}')

    async def safe_send(self, channel_id, content=None, embed=None):
        if content and len(content) > 2000:
            log.debug(f"{channel_id} content {content} is too long; didn't try to send")
            return
        try:
            log.debug(f'sending message to {channel_id}')
            return await bot.http.send_message(channel_id, content, embed=embed and embed.to_dict())
        except (discord.Forbidden, discord.NotFound):
            await pool.execute("UPDATE logs SET toggle = FALSE WHERE channel_id = $1", channel_id)
            return
        except:
            log.exception(f"{channel_id} failed to send {content} {embed}")

    # @coc_client.event
    @coc.ClientEvents.clan_loop_finish()
    async def send_donationlog_events(self, clan_tags):
        query = """SELECT logs.channel_id, 
                          clans.clan_tag,
                          logs.guild_id, 
                          "interval", 
                          toggle,
                          type,
                          detailed 
                   FROM logs 
                   INNER JOIN clans 
                   ON logs.channel_id = clans.channel_id 
                   WHERE clan_tag = ANY($1::TEXT[]) 
                   AND logs.toggle = TRUE
                   AND logs.type = 'donation'
                """
        clan_tags = list(set(n['clan_tag'] for n in self.donationlog_batch_data))
        log.info(f"clan tags {clan_tags}")
        fetch = await pool.fetch(query, clan_tags)

        clan_tag_to_channel_data = {}
        for row in fetch:
            try:
                clan_tag_to_channel_data[row['clan_tag']].append(LogConfig(bot=None, record=row))
            except KeyError:
                clan_tag_to_channel_data[row['clan_tag']] = [LogConfig(bot=None, record=row)]

        events = []
        data = copy.copy(self.donationlog_batch_data)
        self.donationlog_batch_data.clear()
        for event in data:
            for log_config in clan_tag_to_channel_data.get(event['clan_tag'], []):
                events.append(
                    SlimDonationEvent2(
                        event['donations'],
                        event['received'],
                        event['player_name'],
                        event['player_tag'],
                        event['clan_tag'],
                        event['clan_name'],
                        log_config
                    )
                )

        events.sort(key=lambda n: n.log_config.channel_id)

        for config, events in itertools.groupby(events, key=lambda n: n.log_config):
            events = list(events)
            channel_id = config.channel_id
            log.debug(f"running {channel_id}")

            if config.detailed:
                if config.seconds > 0:
                    responses = await get_detailed_log(coc_client, events, raw_events=True)
                    # in this case, responses will be in
                    # [(clan_tag, {"exact": [str], "combo": [str], "unknown": [str]})] form.

                    for clan_tag, items in responses:
                        await self.add_detailed_temp_events(channel_id, clan_tag, items)
                    continue

                embeds = await get_detailed_log(coc_client, events)
                for x in embeds:
                    log.debug(f'Dispatching a log to channel (ID {channel_id}), {x}')

                    await self.safe_send(channel_id, embed=x)

            else:
                messages = await get_basic_log(events)
                if config.seconds > 0 and channel_id:
                    for n in messages:
                        await self.add_temp_events('donation', channel_id, "\n".join(n))
                    continue

                for x in messages:
                    log.debug(f'Dispatching a detailed log to channel (ID {config.channel_id}), {x}')
                    await self.safe_send(channel_id, '\n'.join(x))

    @tasks.loop(seconds=60.0)
    async def board_insert_loop(self):
        log.info('starting board loop')
        async with self.board_batch_lock:
            await self.bulk_board_insert()

    @tasks.loop(seconds=60.0)
    async def set_legend_trophies(self):
        now = datetime.datetime.utcnow()
        if now.hour >= 5:
            tomorrow = (now + datetime.timedelta(days=1)).replace(hour=5, minute=0, second=0)
        else:
            tomorrow = now.replace(hour=5, minute=0, second=0)

        await asyncio.sleep((tomorrow - now).total_seconds())
        await pool.execute("UPDATE PLAYERS SET trophies = true_trophies WHERE season_id = $1 AND trophies > 4900", self.season_id)

    async def bulk_board_insert(self):
        query = """UPDATE players SET donations = public.get_don_rec_max(x.old_dons, x.new_dons, COALESCE(players.donations, 0)), 
                                      received  = public.get_don_rec_max(x.old_rec, x.new_rec, COALESCE(players.received, 0)), 
                                      trophies  = public.get_trophies(x.trophies, players.trophies),
                                      true_trophies = x.trophies,
                                      clan_tag  = x.clan_tag,
                                      player_name = x.player_name
                        FROM(
                            SELECT x.player_tag, x.old_dons, x.new_dons, x.old_rec, x.new_rec, x.trophies, x.clan_tag, x.player_name
                                FROM jsonb_to_recordset($1::jsonb)
                            AS x(player_tag TEXT, 
                                 old_dons INTEGER, 
                                 new_dons INTEGER,
                                 old_rec INTEGER, 
                                 new_rec INTEGER,
                                 trophies INTEGER,
                                 clan_tag TEXT,
                                 player_name TEXT)
                            )
                    AS x
                    WHERE players.player_tag = x.player_tag
                    AND players.season_id=$2
                """

        query2 = """UPDATE eventplayers SET donations = public.get_don_rec_max(x.old_dons, x.new_dons, eventplayers.donations), 
                                            received  = public.get_don_rec_max(x.old_rec, x.new_rec, eventplayers.received),
                                            trophies  = x.trophies   
                        FROM(
                            SELECT x.player_tag, x.old_dons, x.new_dons, x.old_rec, x.new_rec, x.trophies
                            FROM jsonb_to_recordset($1::jsonb)
                            AS x(player_tag TEXT, 
                                 old_dons INTEGER, 
                                 new_dons INTEGER,
                                 old_rec INTEGER,
                                 new_rec INTEGER, 
                                 trophies INTEGER,
                                 clan_tag TEXT,
                                 player_name TEXT)
                            )
                    AS x
                    WHERE eventplayers.player_tag = x.player_tag
                    AND eventplayers.live = true                    
                """
        query3 = """UPDATE boards 
                    SET need_to_update = TRUE 
                    FROM(
                        SELECT channel_id 
                        FROM clans 
                        WHERE clan_tag = ANY($1::TEXT[])
                    ) 
                    AS x 
                    WHERE boards.channel_id = x.channel_id
                """
        if self.board_batch_data:
            log.info('before first query')
            response = await pool.execute(query, list(self.board_batch_data.values()), self.season_id)
            log.info(f'Registered donations/received to the database. Status Code {response}.')
            response = await pool.execute(query2, list(self.board_batch_data.values()))
            log.info(f'Registered donations/received to the events database. Status Code {response}.')
            async with self.last_updated_batch_lock:
                tags = set(tag for (tag, counter) in self.boards_counter.items() if counter > 10)
                response = await pool.execute(query3, list(tags))
                for k in tags:
                    self.boards_counter.pop(k, None)

            log.info(f"updating boards for {response} channels")
            self.board_batch_data.clear()
        else:
            log.info('no new board stuff')

    # @coc_client.event
    @coc.ClanEvents.member_donations()
    async def on_clan_member_donation(self, old_player: CustomClanMember, player: CustomClanMember):
        log.debug(f'Received on_clan_member_donation event for player {player} of clan {player.clan}')
        if old_player.donations > player.donations:
            donations = player.donations
        else:
            donations = player.donations - old_player.donations

        async with self.donationlog_batch_lock:
            self.donationlog_batch_data.append({
                'player_tag': player.tag,
                'player_name': player.name,
                'clan_tag': player.clan and player.clan.tag,
                'clan_name': player.clan and player.clan.name,
                'donations': donations,
                'received': 0,
                'time': datetime.datetime.utcnow().isoformat(),
                'season_id': self.season_id,
            })

        async with self.board_batch_lock:
            try:
                self.board_batch_data[player.tag]['old_dons'] = old_player.donations
                self.board_batch_data[player.tag]['new_dons'] = player.donations
            except KeyError:
                self.board_batch_data[player.tag] = {
                    'player_tag': player.tag,
                    'old_dons': old_player.donations,
                    'new_dons': player.donations,
                    'old_rec': player.received,
                    'new_rec': player.received,
                    'trophies': player.trophies,
                    'clan_tag': player.clan and player.clan.tag,
                    'player_name': player.name,
                }
        # await update(player.tag, player.clan and player.clan.tag)

    # @coc_client.event
    @coc.ClanEvents.member_received()
    async def on_clan_member_received(self, old_player, player):
        old_received = old_player.received
        new_received = player.received

        log.debug(f'Received on_clan_member_received event for player {player} of clan {player.clan}')
        if old_received > new_received:
            received = new_received
        else:
            received = new_received - old_received

        async with self.donationlog_batch_lock:
            self.donationlog_batch_data.append({
                'player_tag': player.tag,
                'player_name': player.name,
                'clan_tag': player.clan and player.clan.tag,
                'clan_name': player.clan and player.clan.name,
                'donations': 0,
                'received': received,
                'time': datetime.datetime.utcnow().isoformat(),
                'season_id': self.season_id,
            })

        async with self.board_batch_lock:
            try:
                self.board_batch_data[player.tag]['old_rec'] = old_received
                self.board_batch_data[player.tag]['new_rec'] = new_received
            except KeyError:
                self.board_batch_data[player.tag] = {
                    'player_tag': player.tag,
                    'old_dons': player.donations,
                    'new_dons': player.donations,
                    'old_rec': old_received,
                    'new_rec': new_received,
                    'trophies': player.trophies,
                    'clan_tag': player.clan and player.clan.tag,
                    'player_name': player.name
                }

        # await update(player.tag, player.clan and player.clan.tag)

    # @coc_client.event
    @coc.ClientEvents.clan_loop_finish()
    async def send_trophylog_events(self, clan_tags):
        query = """SELECT logs.channel_id, 
                          clans.clan_tag,
                          logs.guild_id, 
                          "interval", 
                          toggle,
                          type,
                          detailed 
                   FROM logs 
                   INNER JOIN clans 
                   ON logs.channel_id = clans.channel_id 
                   WHERE clan_tag = ANY($1::TEXT[]) 
                   AND logs.toggle = TRUE
                   AND logs.type = 'trophy'
                """
        data = copy.copy(self.trophylog_batch_data)
        self.trophylog_batch_data.clear()
        clan_tags = list(set(n['clan_tag'] for n in data))
        fetch = await pool.fetch(query, clan_tags)

        clan_tag_to_channel_data = {r['clan_tag']: LogConfig(bot=None, record=r) for r in fetch}
        events = [
            SlimTrophyEvent(
                n['trophy_change'],
                n['league_id'],
                n['player_name'],
                n['clan_tag'],
                n['clan_name'],
                clan_tag_to_channel_data.get(n['clan_tag'])
            ) for n in data if clan_tag_to_channel_data.get(n['clan_tag'])
        ]
        events.sort(key=lambda n: n.log_config.channel_id)

        for config, events in itertools.groupby(events, key=lambda n: n.log_config):
            log.debug(f"running {config.channel_id}")
            events = list(events)
            messages = [format_trophy_log_message(x) for x in events]

            group_batch = []
            for i in range(math.ceil(len(messages) / 20)):
                group_batch.append(messages[i * 20:(i + 1) * 20])

            for x in group_batch:
                if config.seconds > 0:
                    await self.add_temp_events('trophy', config.channel_id, '\n'.join(x))
                else:
                    log.debug(f'Dispatching a log to channel '
                              f'(ID {config.channel_id} type={config.type})')

                    await self.safe_send(config.channel_id, '\n'.join(x))

    # @coc_client.event
    @coc.ClanEvents.member_trophies()
    async def on_clan_member_trophies_change(self, old_player, player):
        old_trophies = old_player.trophies
        new_trophies = player.trophies
        log.debug(f'Received on_clan_member_trophy_change event for player {player} of clan {player.clan}')
        change = player.trophies - old_player.trophies

        async with self.trophylog_batch_lock:
            self.trophylog_batch_data.append({
                'player_tag': player.tag,
                'player_name': player.name,
                'clan_tag': player.clan and player.clan.tag,
                'trophy_change': change,
                'league_id': player.league_id,
                'time': datetime.datetime.utcnow().isoformat(),
                'season_id': self.season_id,
                'clan_name': player.clan and player.clan.name
            })

        async with self.board_batch_lock:
            try:
                self.board_batch_data[player.tag]['trophies'] = new_trophies
            except KeyError:
                self.board_batch_data[player.tag] = {
                    'player_tag': player.tag,
                    'old_dons': player.donations,
                    'new_dons': player.donations,
                    'old_rec': player.received,
                    'new_rec': player.received,
                    'trophies': new_trophies,
                    'clan_tag': player.clan and player.clan.tag,
                    'player_name': player.name
                }

        if new_trophies > old_trophies:
            await self.update(player.tag, player.clan and player.clan.tag)

    @tasks.loop(hours=1)
    async def event_player_updater(self):
        query = "SELECT DISTINCT player_tag FROM eventplayers WHERE live = True;"
        fetch = await pool.fetch(query)

        query = """UPDATE eventplayers
                   SET donations             = x.end_fin + x.end_sic - eventplayers.start_friend_in_need - eventplayers.start_sharing_is_caring,
                       trophies              = x.trophies,
                       end_friend_in_need    = x.end_fin,
                       end_sharing_is_caring = x.end_sic,
                       end_attacks           = x.end_attacks,
                       end_defenses          = x.end_defenses,
                       end_best_trophies     = x.end_best_trophies
                       
                   FROM (
                       SELECT x.player_tag,
                              x.trophies,
                              x.end_fin,
                              x.end_sic,
                              x.end_attacks,
                              x.end_defenses,
                              x.end_best_trophies
                       FROM jsonb_to_recordset($1::jsonb)
                       AS x (
                           player_tag TEXT,
                           trophies INTEGER,
                           end_fin INTEGER,
                           end_sic INTEGER,
                           end_attacks INTEGER,
                           end_defenses INTEGER,
                           end_best_trophies INTEGER
                           )
                        )
                   AS x
                   WHERE eventplayers.player_tag = x.player_tag
                   AND eventplayers.live = True
                """

        to_insert = []

        log.info(f'Starting loop for event updates. {len(fetch)} players to update!')
        start = time.perf_counter()
        async for player in coc_client.get_players((n[0] for n in fetch), update_cache=False):
            to_insert.append(
                {
                    'player_tag': player.tag,
                    'trophies': player.trophies,
                    'end_fin': player.get_achievement('Friend in Need').value,
                    'end_sic': player.get_achievement('Sharing is caring').value,
                    'end_attacks': player.attack_wins,
                    'end_defenses': player.defense_wins,
                    'end_best_trophies': player.best_trophies
                }
            )
            await asyncio.sleep(0.01)
        await pool.execute(query, to_insert)
        log.info(f'Loop for event updates finished. Took {(time.perf_counter() - start)*1000}ms')

    #
    # async def on_clan_member_league_change(old_league, new_league, player, clan):
    #     if old_league.id > new_league.id:
    #         return  # they dropped a league
    #     if new_league.id == 29000000:
    #         return  # unranked - probably start of season.
    #
    #     query = """SELECT channel_id
    #                FROM events
    #                INNER JOIN eventplayers
    #                ON eventplayers.event_id = events.id
    #                WHERE start_best_trophies < $1
    #                AND player_tag = $2
    #             """
    #     fetch = await self.bot.pool.fetch(query, player.trophies, player.tag)
    #     if not fetch:
    #         return
    #
    #     msg = f"Breaking new heights! {player} just got promoted to {new_league} league!"
    #
    #     for record in fetch:
    #         await self.safe_send(self.bot.get_channel(record['channel_id']), msg)


    @tasks.loop(minutes=1.0)
    async def last_updated_loop(self):
        try:
            await self.update_db()
        except:
            log.exception("last updated loop")

    async def update_db(self):
        query = """UPDATE players 
                   SET last_updated = now()
                   WHERE player_tag = ANY($1::TEXT[])
                   AND players.season_id = $2
                """
        query2 = """
                 WITH cte AS (
                    SELECT DISTINCT clan_tag, activity_sync FROM clans INNER JOIN guilds ON clans.guild_id = guilds.guild_id
                 )
                 INSERT INTO activity_query (player_tag, clan_tag, counter, hour_digit, hour_time)
                 SELECT x.player_tag, x.clan_tag, x.counter, date_part('HOUR', now()), date_trunc('HOUR', now())
                 FROM jsonb_to_recordset($1::jsonb)
                 AS x(player_tag TEXT, clan_tag TEXT, counter INTEGER)
                 INNER JOIN cte ON cte.clan_tag = x.clan_tag
                 WHERE cte.activity_sync = TRUE
                 ON CONFLICT (player_tag, clan_tag, hour_time)
                 DO UPDATE SET counter = activity_query.counter + excluded.counter
                 """
        async with self.last_updated_batch_lock:
            await pool.execute(
                query, list(self.last_updated_tags), self.season_id
            )
            nice = [{"player_tag": player_tag, "clan_tag": clan_tag, "counter": counter} for ((player_tag, clan_tag), counter) in self.last_updated_counter.most_common()]
            await pool.execute(query2, nice)
            self.last_updated_tags.clear()
            self.last_updated_counter.clear()

    async def update(self, player_tag, clan_tag):
        async with self.last_updated_batch_lock:
            self.boards_counter[clan_tag] += 1

            if clan_tag:
                self.last_updated_counter[(player_tag, clan_tag)] += 1
            self.last_updated_tags.add(player_tag)

    # @coc_client.event
    @coc.ClanEvents.member_name()
    @coc.ClanEvents.member_donations()
    @coc.ClanEvents.member_versus_trophies()
    @coc.ClanEvents.member_exp_level()
    @coc.ClanEvents.member_donations()
    @coc.ClanEvents.member_received()
    async def on_member_update(self, old_player, player):
        log.debug("received update for clan members.")
        await self.update(player.tag, player.clan and player.clan.tag)

    # @coc_client.event
    @coc.ClanEvents.member_join()
    async def on_clan_member_join(self, member, clan):
        player_query = """INSERT INTO players (
                                            player_tag, 
                                            donations, 
                                            received, 
                                            trophies, 
                                            start_trophies, 
                                            season_id,
                                            start_update,
                                            clan_tag,
                                            player_name
                                            ) 
                        VALUES ($1,$2,$3,$4,$4,$5,True, $6, $7) 
                        ON CONFLICT (player_tag, season_id) 
                        DO UPDATE SET clan_tag = $6
                    """

        response = await pool.execute(
            player_query,
            member.tag,
            member.donations,
            member.received,
            member.trophies,
            self.season_id,
            clan.tag,
            member.name
        )
        log.debug(f"ran player joined for player {member} of clan {clan}")
        return
        player = await coc_client.get_player(member.tag)
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
                                        start_best_trophies,
                                        start_update,
                                        clan_tag,
                                        player_name
                                        ) 
                    VALUES ($1,$2,$3,$4,$4,$5,$6,$7,$8,$9,$10,True, $11, $12) 
                    ON CONFLICT (player_tag, season_id) 
                    DO UPDATE SET clan_tag = $11
                """

        response = await pool.execute(
            player_query,
            player.tag,
            player.donations,
            player.received,
            player.trophies,
            SEASON_ID,
            player.get_achievement('Friend in Need').value,
            player.get_achievement('Sharing is caring').value,
            player.attack_wins,
            player.defense_wins,
            player.best_trophies,
            clan.tag,
            player.name
        )
        log.debug(f'New member {member} joined clan {clan}. Performed a query to insert them into players. '
                  f'Status Code: {response}')
        return

        query = """SELECT events.id 
                   FROM events 
                   INNER JOIN clans 
                   ON clans.guild_id = events.guild_id 
                   WHERE clans.clan_tag = $1
                   AND events.start <= now()
                   AND events.finish >= now()
                """
        fetch = await pool.fetch(query, clan.tag)
        if not fetch:
            return

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
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, True, True)
                            ON CONFLICT (player_tag, event_id)
                            DO UPDATE 
                            SET live=True
                            WHERE eventplayers.player_tag = $1
                            AND eventplayers.event_id = $2
                        """

        for n in fetch:
            response = await pool.execute(
                event_query,
                player.tag,
                player.trophies,
                n['id'],
                player.get_achievement('Friend in Need').value,
                player.get_achievement('Sharing is caring').value,
                player.attack_wins,
                player.defense_wins,
                player.trophies,
                player.best_trophies
              )

            log.debug(f'New member {member} joined clan {clan}. '
                      f'Performed a query to insert them into eventplayers. Status Code: {response}')

    # @coc_client.event
    @coc.ClanEvents.member_leave()
    async def on_clan_member_leave(self, member, clan):
        query = "UPDATE players SET clan_tag = null where player_tag = $1 AND season_id = $2"
        await pool.execute(query, member.tag, self.season_id)

    @tasks.loop(seconds=60.0)
    async def update_clan_tags(self):
        try:
            query = "SELECT DISTINCT(clan_tag) FROM clans"
            fetch = await pool.fetch(query)
            log.info(f"Setting {len(fetch)} tags to update")
            coc_client._clan_updates = [n[0] for n in fetch]
        except:
            log.exception("task failed?")
            pass

    # @coc_client.event
    @coc.ClientEvents.maintenance_start()
    async def maintenance_start(self):
        await self.safe_send(594286547449282587, "Maintenance has started!")

    # @coc_client.event
    @coc.ClientEvents.maintenance_completion()
    async def maintenance_completed(self, start_time):
        await self.safe_send(594286547449282587, f"Maintenance has finished, started at {start_time}!")

    async def fetch_webhooks(self):
        bot.error_webhooks = itertools.cycle([await bot.fetch_webhook(id_) for id_ in (749580949968388126, 749580957362946089, 749580961477296138, 749580975511568554, 749580988530556978, 749581056184942603)])

    @tasks.loop(seconds=120.0)
    async def send_stats(self):
        try:
            stats = coc_client.http.stats.items()
            if len(stats) > 2:
                columns = 2
                rows = math.ceil(len(stats) / 2)
            else:
                columns = 1
                rows = len(stats)

            if len(stats) == 1:
                fig, axs = plt.subplots(rows, columns)
                axs = [axs]
            else:
                fig, (*axs, ) = plt.subplots(rows, columns)

            for i, (key, values) in enumerate(stats):
                axs[i].bar(range(len(values)), list(values), color="blue")
                axs[i].set_ylabel(key)
            fig.suptitle(f"Latency for last minute to {datetime.datetime.utcnow().strftime('%H:%M %d/%m')}")
            b = io.BytesIO()
            plt.savefig(b, format='png')
            b.seek(0)
            await next(bot.error_webhooks).send(file=discord.File(b, f'cocapi.png'))
            plt.close()
        except Exception:
            log.exception("sending stats")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(bot.login(creds.bot_token))
    Syncer().start()
