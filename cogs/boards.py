import asyncio
import asyncpg
import coc
import discord
import itertools
import io
import logging
import math
import time

from collections import namedtuple
from datetime import datetime
from discord.ext import commands, tasks
from PIL import Image, UnidentifiedImageError

from cogs.utils.db_objects import DatabaseMessage, BoardPlayer, BoardConfig
from cogs.utils.formatters import CLYTable, get_render_type
from cogs.utils.images import DonationBoardImage, TrophyBoardImage
from cogs.utils.opencv_boards import DonationBoardTable, TrophyBoardTable
from cogs.utils import checks


log = logging.getLogger(__name__)

MockPlayer = namedtuple('MockPlayer', 'clan name')
mock = MockPlayer('Unknown', 'Unknown')

REFRESH_EMOJI = discord.PartialEmoji(name="refresh", id=694395354841350254, animated=False)
LEFT_EMOJI = discord.PartialEmoji(name="\N{BLACK LEFT-POINTING TRIANGLE}\ufe0f", id=None, animated=False)    # [:arrow_left:]
RIGHT_EMOJI = discord.PartialEmoji(name="\N{BLACK RIGHT-POINTING TRIANGLE}\ufe0f", id=None, animated=False)   # [:arrow_right:]
PERCENTAGE_EMOJI = discord.PartialEmoji(name="percent", id=694463772135260169, animated=False)
GAIN_EMOJI = discord.PartialEmoji(name="gain", id=696280508933472256, animated=False)
LAST_ONLINE_EMOJI = discord.PartialEmoji(name="lastonline", id=696292732599271434, animated=False)
HISTORICAL_EMOJI = discord.PartialEmoji(name="historical", id=694812540290465832, animated=False)

GLOBAL_BOARDS_CHANNEL_ID = 663683345108172830


class HTMLImages:
    def __init__(self, players, title=None, image=None, sort_by=None, footer=None):
        self.players = players

        self.title = title or "Donation Leaderboard"
        self.image = image or "https://cdn.discordapp.com/attachments/641594147374891029/767306860306759680/dc0f83c3eba7fad4cbe8de3799708e93.jpg"
        self.footer = footer

        self.html = ""
        if sort_by:
            columns = ("#", "Player Name", "donations", "received", "ratio", "last_online")
            self.selected_index = [1, columns.index(sort_by)]
        else:
            self.selected_index = []

    def get_readable(self, delta):
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)

        if delta.days:
            return f"{days}d {hours}h"
        else:
            return f"{hours}h {minutes}m"

    def add_style(self):
        if len(self.players) >= 30:
            body = """
body {
width: 2500px;
}
"""
            width = "width: 50%;"
        else:
            body = """
body {
width: 1200px;
}
"""
            width = "width: 100%;"

        self.html += """
<!DOCTYPE html>
<html>
<head>
<style>
""" + body + """
img {
  position: fixed;
  top: 0;
  left: 0;
  height: 100%;
  width: 100%;
  z-index:-1;
  opacity:0.9;
}
table {
  font-family: Helvetica, Verdana,courier,arial,helvetica;
  border-collapse: seperate;
  border-spacing: 0 12px;
""" + width + """
  padding-bottom: 30px;
  padding-left: 30px;
  padding-right: 30px;
  float: left
}

td, th {
  text-align: center;
  letter-spacing: 1px;
  font-size: 42px;
  padding: 7px;
  box-shadow: 0 4px 8px 0 rgba(0, 0, 0, 0.2), 0 6px 20px 0 rgba(0, 0, 0, 0.19);
}

th {
  border: 1px solid #404040;
  background-color: rgba(185, 147, 108, 0.6);
}
.selected {
  background-color: #ace;
}
.footer {
  float: left;
  text-align: left;
  font-size: 30px;
  font-style: bold;
  padding: 2px;
  top: 0;
  margin-top:0;
  margin-bottom:0;
}

tr:nth-child(even) {
  background-color: rgba(166, 179, 196, 0.8);
}
tr:nth-child(odd) {
  background-color: rgba(196, 186, 133, 0.8);
}

header {
  background:-webkit-gradient(linear,left bottom,left top,color-stop(20%,rgb(196, 183, 166)),color-stop(80%,rgb(220, 207, 186)));
  font-size: 70px;
  margin-left: auto;
  margin-right: auto;
  text-align: center;
  font-style: bold;
  font-weight: 200;
  letter-spacing: 1.5px;
  opacity: 1;
}
</style>
        """

    def add_body(self):
        self.html += '<body>'

    def add_title(self):
        self.html += f"<header>{self.title}</header>"

    def add_image(self):
        self.html += f'<img src="{self.image}" alt="Test"></img>'

    def add_table(self, players):
        to_add = "<table>"

        headers = ("#", "Player Name", "Dons", "Rec", "Ratio", "Last On")
        to_add += "<tr>" + "".join(
            f"<th{' class=selected' if i in self.selected_index else ''}>{column}</th>"
            for i, column in enumerate(headers)
        ) + "</tr>"

        for player in players:
            to_add += "<tr>" + "".join(
                f"<td{' class=selected' if i in self.selected_index else ''}>{cell}</td>"
                for i, cell in enumerate(player)
            ) + "</tr>"

        to_add += "</table>"
        self.html += to_add

    def add_footer(self):
        if self.footer:
            self.html += f'<h6 class="footer">{self.footer}</h6>'

    def end_html(self):
        self.html += "</body></html>"

    def parse_players(self):
        self.players = [(str(i) + ".", p['player_name'], p['donations'], p['received'], round(p['donations'] / (p['received'] or 1), 2),
                        self.get_readable(p['last_online'])) for i, p in enumerate(self.players, start=1)]

    async def make(self):
        s = time.perf_counter()
        self.parse_players()
        self.add_style()
        self.add_body()
        self.add_title()
        self.add_image()
        if len(self.players) >= 30:
            self.add_table(self.players[:int(len(self.players)/2)])
            self.add_table(self.players[int(len(self.players)/2):])
        else:
            self.add_table(self.players)

        self.add_footer()
        self.end_html()
        log.info((time.perf_counter() - s)*1000)

        s = time.perf_counter()
        proc = await asyncio.create_subprocess_shell(
            "wkhtmltoimage - -", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        log.info((time.perf_counter() - s)*1000)
        s = time.perf_counter()
        stdout, stderr = await proc.communicate(input=self.html.encode('utf-8'))
        log.info((time.perf_counter() - s)*1000)
        b = io.BytesIO(stdout)
        b.seek(0)
        return b


class DonationBoard(commands.Cog):
    """Contains all DonationBoard Configurations.
    """
    def __init__(self, bot):
        self.bot = bot

        self.clan_updates = []

        self._to_be_deleted = set()

        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._data_batch = {}

        self.update_board_loops.add_exception_type(asyncpg.PostgresConnectionError, coc.ClashOfClansException)
        self.update_board_loops.start()

        self.update_global_board.add_exception_type(asyncpg.PostgresConnectionError, coc.ClashOfClansException)
        self.update_global_board.start()

        self.tags_to_update = set()
        self.last_updated_tags = {}
        self.last_updated_channels = {}
        self._board_channels = []
        self.season_meta = {}

    def cog_unload(self):
        self.update_board_loops.cancel()
        self.update_global_board.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        self.webhooks = itertools.cycle(n for n in await self.bot.get_guild(691779140059267084).webhooks())

    @property
    def board_channels(self):
        if not self._board_channels:
            self._board_channels = itertools.cycle(n for n in self.bot.get_guild(691779140059267084).text_channels)
        return self._board_channels

    async def get_season_meta(self, season_id):
        try:
            return self.season_meta[season_id]
        except KeyError:
            fetch = await self.bot.pool.fetchrow("SELECT start, finish FROM seasons WHERE id = $1", season_id)
            season_start, season_finish = fetch[0].strftime('%d-%b-%Y'), fetch[1].strftime('%d-%b-%Y')
            self.season_meta[season_id] = (season_start, season_finish)
            return (season_start, season_finish)

    @tasks.loop(seconds=60.0)
    async def update_board_loops(self):
        await self.bot.wait_until_ready()
        if not hasattr(self, "webhooks"):
            await asyncio.sleep(10)

        query = """SELECT channel_id, type FROM boards WHERE need_to_update = TRUE"""
        fetch = await self.bot.pool.fetch(query)

        for n in fetch:
            try:
                await self.update_board(n['channel_id'], n['type'])
                self.last_updated_channels[n['channel_id']] = datetime.utcnow()
            except:
                log.exception(f"old board failed...\nChannel ID: {n['channel_id']}")

    @tasks.loop(hours=1)
    async def update_global_board(self):
        query = "SELECT * FROM boards WHERE channel_id = $1"
        fetch = await self.bot.pool.fetchrow(query, GLOBAL_BOARDS_CHANNEL_ID)
        config = BoardConfig(bot=self.bot, record=fetch)
        await self.new_donationboard_updater(config, 0, season_offset=0, reset=True, update_global_board=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if not isinstance(channel, discord.TextChannel):
            return

        query = "DELETE FROM messages WHERE channel_id = $1;"
        query2 = "DELETE FROM boards WHERE channel_id = $1"
        query3 = "DELETE FROM logs WHERE channel_id = $1"
        query4 = "DELETE FROM clans WHERE channel_id = $1"

        for q in (query, query2, query3, query4):
            await self.bot.pool.execute(q, channel.id)

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
            await self.new_board_message(self.bot.get_channel(payload.channel_id), config.type)

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
                await self.new_board_message(self.bot.get_channel(payload.channel_id), config.type)

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

    async def update_board(self, channel_id=None, board_type=None, message_id=None):
        if not self.bot.utils:
            return

        if message_id:
            config = await self.bot.utils.board_config(message_id)
        else:
            config = await self.bot.utils.board_config_from_channel(channel_id, board_type)

        if not config:
            return
        if not config.toggle:
            return
        if not config.channel:
            return
        if config.in_event:
            return

        return await self.new_donationboard_updater(config)

    @staticmethod
    def get_next_per_page(page_no, config_per_page):
        if config_per_page == 0:
            lookup = {
                1: 15,
                2: 15,
                3: 20,
                4: 25,
                5: 25
            }
            if page_no > 5:
                return 50
            return lookup[page_no]

        return config_per_page

    async def new_donationboard_updater(self, config, add_pages=0, season_offset=0, reset=False, update_global_board=False):
        if config.channel_id == GLOBAL_BOARDS_CHANNEL_ID and not update_global_board:
            return

        donationboard = config.type == 'donation'
        start = time.perf_counter()
        message = await self.bot.utils.get_message(config.channel, config.message_id)
        if not message:
            try:
                message = await config.channel.send("Placeholder.... do not delete me!")
            except (discord.Forbidden, discord.NotFound):
                await self.bot.pool.execute("UPDATE boards SET toggle = FALSE WHERE channel_id = $1", config.channel_id)
                return

            await message.add_reaction(REFRESH_EMOJI)
            await message.add_reaction(LEFT_EMOJI)
            await message.add_reaction(RIGHT_EMOJI)
            if donationboard:
                await message.add_reaction(PERCENTAGE_EMOJI)
            else:
                await message.add_reaction(GAIN_EMOJI)

            await message.add_reaction(LAST_ONLINE_EMOJI)
            await message.add_reaction(HISTORICAL_EMOJI)
            await self.bot.pool.execute("UPDATE boards SET message_id = $1 WHERE channel_id = $2 AND type = $3", message.id, config.channel_id, config.type)

        try:
            page = int(message.embeds[0]._footer['text'].split(";")[0].split(" ")[1])
            season_id = int(message.embeds[0]._footer['text'].split(";")[1].split(" ")[1])
        except (AttributeError, KeyError, ValueError, IndexError):
            page = 1
            season_id = await self.bot.seasonconfig.get_season_id()

        if page + add_pages < 1:
            return  # don't bother about page 0's

        offset = 0

        if reset:
            offset = 0
            page = 1
            season_id = await self.bot.seasonconfig.get_season_id()
        else:
            for i in range(1, page + add_pages):
                offset += self.get_next_per_page(i, config.per_page)
            season_id += season_offset

        if season_id < 1:
            season_id = await self.bot.seasonconfig.get_season_id()
        if offset < 0:
            offset = 0

        if config.channel_id == GLOBAL_BOARDS_CHANNEL_ID:
            query = f"""SELECT DISTINCT player_name,
                                        donations,
                                        received,
                                        trophies,
                                        now() - last_updated AS "last_online",
                                        donations / NULLIF(received, 0) AS "ratio",
                                        trophies - start_trophies AS "gain"
                       FROM players
                       INNER JOIN clans
                       ON clans.clan_tag = players.clan_tag
                       WHERE season_id = $1
                       ORDER BY {'donations' if config.sort_by == 'donation' else config.sort_by} DESC
                       NULLS LAST
                       LIMIT $2
                       OFFSET $3
                    """
            fetch = await self.bot.pool.fetch(
                query,
                season_id,
                self.get_next_per_page(page + add_pages, config.per_page),
                offset
            )
        else:
            query = f"""SELECT DISTINCT player_name,
                                        donations,
                                        received,
                                        trophies,
                                        now() - last_updated AS "last_online",
                                        donations / NULLIF(received, 0) AS "ratio",
                                        trophies - start_trophies AS "gain"
                       FROM players
                       INNER JOIN clans
                       ON clans.clan_tag = players.clan_tag
                       WHERE clans.channel_id = $1
                       AND season_id = $2
                       ORDER BY {'donations' if config.sort_by == 'donation' else config.sort_by} DESC
                       NULLS LAST
                       LIMIT $3
                       OFFSET $4
                    """
            fetch = await self.bot.pool.fetch(
                query,
                config.channel_id,
                season_id,
                self.get_next_per_page(page + add_pages, config.per_page),
                offset
            )

        players = [BoardPlayer(n[0], n[1], n[2], n[3], n[4], n[6], i + offset + 1) for i, n in enumerate(fetch)]

        if not players:
            return  # they scrolled too far

        season_start, season_finish = await self.get_season_meta(season_id)

        if donationboard:
            table = HTMLImages(
                players=fetch,
                title=config.title,
                image=config.icon_url,
                sort_by=config.sort_by,
                footer=f"Season: {season_start} - {season_finish}."
            )
            render = await table.make()
        else:
            if config.icon_url:
                try:
                    icon_bytes = await self.bot.http.get_from_cdn(config.icon_url)
                    icon = Image.open(io.BytesIO(icon_bytes)).resize((180, 180))
                except (discord.Forbidden, UnidentifiedImageError):
                    await self.bot.pool.execute("UPDATE boards SET icon_url = NULL WHERE message_id = $1", message.id)
                    icon = None
            else:
                icon = None

            image = TrophyBoardImage(config.title, icon, season_start, season_finish)
            await self.bot.loop.run_in_executor(None, image.add_players, players)
            render = await self.bot.loop.run_in_executor(None, image.render)

        logged_board_message = await next(self.webhooks).send(
            f"Perf: {(time.perf_counter() - start) * 1000}ms\n"
            f"Channel: {config.channel_id}\n"
            f"Guild: {config.guild_id}",
            file=discord.File(render, f'{config.type}board.png'),
            wait=True
        )
        await self.bot.background.log_message_send(config.message_id, config.channel_id,  config.guild_id, config.type + 'board')

        if donationboard:
            await message.edit(content=f"{logged_board_message.attachments[0].url}\nPage {page + add_pages};Season {season_id};")
        else:
            e = discord.Embed(colour=discord.Colour.blue() if donationboard else discord.Colour.green())
            e.set_image(url=logged_board_message.attachments[0].url)
            e.set_footer(text=f"Page {page + add_pages};Season {season_id};").timestamp = datetime.utcnow()
            await message.edit(content=None, embed=e)

    async def mpl_boards(self, config, add_pages=0, season_offset=0, reset=False, update_global_board=False):
        if config.channel_id == GLOBAL_BOARDS_CHANNEL_ID and not update_global_board:
            return

        donationboard = config.type == 'donation'
        start = time.perf_counter()
        message = await self.bot.utils.get_message(config.channel, config.message_id)
        if not message:
            try:
                message = await config.channel.send("Placeholder.... do not delete me!")
            except (discord.Forbidden, discord.NotFound):
                await self.bot.pool.execute("UPDATE boards SET toggle = FALSE WHERE channel_id = $1", config.channel_id)
                return

            await message.add_reaction(REFRESH_EMOJI)
            await message.add_reaction(LEFT_EMOJI)
            await message.add_reaction(RIGHT_EMOJI)
            if donationboard:
                await message.add_reaction(PERCENTAGE_EMOJI)
            else:
                await message.add_reaction(GAIN_EMOJI)

            await message.add_reaction(LAST_ONLINE_EMOJI)
            await message.add_reaction(HISTORICAL_EMOJI)
            await self.bot.pool.execute("UPDATE boards SET message_id = $1 WHERE channel_id = $2 AND type = $3", message.id, config.channel_id, config.type)

        try:
            page = int(message.content.split(";")[0].split(" ")[1])
            season_id = int(message.content.split(";")[1].split(" ")[1])
        except (AttributeError, KeyError, ValueError, IndexError):
            page = 1
            season_id = await self.bot.seasonconfig.get_season_id()

        if page + add_pages < 1:
            return  # don't bother about page 0's

        offset = 0

        if reset:
            offset = 0
            page = 1
            season_id = await self.bot.seasonconfig.get_season_id()
        else:
            for i in range(1, page + add_pages):
                offset += self.get_next_per_page(i, config.per_page)
            season_id += season_offset

        if season_id < 1:
            season_id = await self.bot.seasonconfig.get_season_id()
        if offset < 0:
            offset = 0

        if config.channel_id == GLOBAL_BOARDS_CHANNEL_ID:
            query = f"""SELECT DISTINCT player_name,
                                        donations,
                                        received,
                                        trophies,
                                        cast(donations as decimal) / NULLIF(received, 0) AS "ratio",
                                        now() - last_updated AS "last_online",
                                        trophies - start_trophies AS "gain"
                       FROM players
                       INNER JOIN clans
                       ON clans.clan_tag = players.clan_tag
                       WHERE season_id = $1
                       ORDER BY {'donations' if config.sort_by == 'donation' else config.sort_by} DESC
                       NULLS LAST
                       LIMIT $2
                       OFFSET $3
                    """
            fetch = await self.bot.pool.fetch(
                query,
                season_id,
                self.get_next_per_page(page + add_pages, config.per_page),
                offset
            )
        else:
            query = f"""SELECT DISTINCT player_name,
                                        donations,
                                        received,
                                        trophies,
                                        CAST(donations as decimal) / NULLIF(received, 0) AS "ratio",
                                        now() - last_updated AS "last_online",
                                        trophies - start_trophies AS "gain"
                       FROM players
                       INNER JOIN clans
                       ON clans.clan_tag = players.clan_tag
                       WHERE clans.channel_id = $1
                       AND season_id = $2
                       ORDER BY {'donations' if config.sort_by == 'donation' else config.sort_by} DESC
                       NULLS LAST
                       LIMIT $3
                       OFFSET $4
                    """
            fetch = await self.bot.pool.fetch(
                query,
                config.channel_id,
                season_id,
                self.get_next_per_page(page + add_pages, config.per_page),
                offset
            )

        if not fetch:
            return  # they scrolled too far

        # fetch = await self.bot.pool.fetchrow("SELECT start, finish FROM seasons WHERE id = $1", season_id)
        # season_start, season_finish = fetch[0].strftime('%d-%b-%Y'), fetch[1].strftime('%d-%b-%Y')

        if donationboard:
            table = HTMLImages(players=fetch, title=config.title, image=config.icon_url, sort_by=config.sort_by)
            render = await table.make()
        else:
            table = TrophyBoardTable(config.title, offset)
            table.add_rows(fetch)
            render = await self.bot.loop.run_in_executor(None, table.render)

        logged_board_message = await next(self.webhooks).send(
            f"Perf: {(time.perf_counter() - start) * 1000}ms\n"
            f"Channel: {config.channel_id}\n"
            f"Guild: {config.guild_id}",
            file=discord.File(render, f'{config.type}board.png'),
            wait=True
        )
        await self.bot.background.log_message_send(config.message_id, config.channel_id,  config.guild_id, config.type + 'board')

        if donationboard:
            await message.edit(content=f"{logged_board_message.attachments[0].url}\nPage {page + add_pages};Season {season_id};")
        else:
            e = discord.Embed(colour=discord.Colour.blue() if donationboard else discord.Colour.green())
            e.set_image(url=logged_board_message.attachments[0].url)
            e.set_footer(text=f"Page {page + add_pages};Season {season_id};").timestamp = datetime.utcnow()
            await message.edit(content=None, embed=e)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def forceboard(self, ctx, message_id: int = None):
        await self.update_board(message_id=message_id)
        await ctx.confirm()

    @commands.command(hidden=True)
    @commands.is_owner()
    async def random_board(self, ctx, offset=0):
        query = "SELECT * FROM boards WHERE toggle=True AND type='donation' ORDER BY random() LIMIT 1"
        fetch = await self.bot.pool.fetchrow(query)
        if not fetch:
            return

        message = await ctx.send("Placeholder")

        config = BoardConfig(bot=self.bot, record=fetch)
        config.guild_id = message.guild.id
        config.channel_id = message.channel.id
        config.message_id = message.id
        await self.mpl_boards(config, offset)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self.reaction_action(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self.reaction_action(payload)

    async def reaction_action(self, payload):
        await self.bot.wait_until_ready()
        if payload.user_id == self.bot.user.id:
            return
        if payload.emoji not in (REFRESH_EMOJI, LEFT_EMOJI, RIGHT_EMOJI, PERCENTAGE_EMOJI, GAIN_EMOJI, LAST_ONLINE_EMOJI, HISTORICAL_EMOJI):
            return

        message = await self.bot.utils.get_message(self.bot.get_channel(payload.channel_id), payload.message_id)
        if not message:
            return
        if not message.author.id == self.bot.user.id:
            return

        query = "SELECT * FROM boards WHERE message_id = $1"
        fetch = await self.bot.pool.fetchrow(query, payload.message_id)
        if not fetch:
            return

        hard_reset = False
        offset = 0
        season_offset = 0
        update_globalboards = False

        if payload.emoji == RIGHT_EMOJI:
            offset = 1
            update_globalboards = True

        elif payload.emoji == LEFT_EMOJI:
            offset = -1
            update_globalboards = True

        elif payload.emoji == REFRESH_EMOJI:
            original_sort = 'donations' if fetch['type'] == 'donation' else 'trophies'
            query = "UPDATE boards SET sort_by = $1 WHERE message_id = $2 RETURNING *"
            fetch = await self.bot.pool.fetchrow(query, original_sort, payload.message_id)
            hard_reset = True
            update_globalboards = True

        elif payload.emoji == PERCENTAGE_EMOJI:
            query = "UPDATE boards SET sort_by = 'ratio' WHERE message_id = $1 RETURNING *"
            fetch = await self.bot.pool.fetchrow(query, payload.message_id)
            update_globalboards = True

        elif payload.emoji == GAIN_EMOJI:
            query = "UPDATE boards SET sort_by = 'gain' WHERE message_id = $1 RETURNING *"
            fetch = await self.bot.pool.fetchrow(query, payload.message_id)
            update_globalboards = True

        elif payload.emoji == LAST_ONLINE_EMOJI:
            query = "UPDATE boards SET sort_by = 'last_online ASC, player_name' WHERE message_id = $1 RETURNING *"
            fetch = await self.bot.pool.fetchrow(query, payload.message_id)
            update_globalboards = True

        elif payload.emoji == HISTORICAL_EMOJI:
            season_offset = -1
            update_globalboards = True

        config = BoardConfig(bot=self.bot, record=fetch)
        await self.new_donationboard_updater(config, offset, season_offset=season_offset, reset=hard_reset, update_global_board=update_globalboards)


def setup(bot):
    bot.add_cog(DonationBoard(bot))
