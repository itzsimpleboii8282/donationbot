import aioredis
import asyncio
import datetime
import coc
import discord
import aiohttp
import traceback
import creds

from discord.ext import commands

from botlog import setup_logging, add_hooks
from cogs.utils import context
from cogs.utils.db import Table
from cogs.utils.error_handler import error_handler, discord_event_error, clash_event_error


initial_extensions = [
    'cogs.admin',
    'cogs.aliases',
    'cogs.auto_claim',
    'cogs.botutils',
    'cogs.deprecated',
    'cogs.donations',
    'cogs.events',
    'cogs.eventstats',
    'cogs.guildsetup',
    'cogs.info',
    'cogs.reset_season',
    'cogs.seasonstats',
    'cogs.stats',
    'cogs.trophies',
]
if creds.live:
    initial_extensions.extend(
        (
            'cogs.background_management',
            'cogs.boards',
            'cogs.donationlogs',
            'cogs.trophylog'
        )
    )
    command_prefix = '+'
    key_names = 'test'
else:
    command_prefix = '//'
    key_names = 'windows'


class COCClient(coc.EventsClient):
    async def on_event_error(self, event_name, exception, *args, **kwargs):
        await clash_event_error(self, event_name, exception, *args, **kwargs)


coc_client = coc.login(creds.email, creds.password, client=COCClient,
                       key_names=key_names, throttle_limit=30, key_count=3)


description = "A simple discord bot to track donations of clan families in clash of clans."


class DonationBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or(command_prefix), case_insensitive=True,
                         description=description, pm_help=None, help_attrs=dict(hidden=True),
                         fetch_offline_members=True)

        self.colour = discord.Colour.blurple()

        self.coc = coc_client

        self.client_id = creds.client_id
        self.dbl_token = creds.dbl_token
        self.owner_ids = {230214242618441728, 251150854571163648}  # maths, tuba
        self.session = aiohttp.ClientSession(loop=self.loop)

        add_hooks(self)

        self.uptime = datetime.datetime.utcnow()

        for e in initial_extensions:
            try:
                self.load_extension(e)  # load cogs
            except Exception:
                traceback.print_exc()

    @property
    def donationboard(self):
        return self.get_cog('DonationBoard')

    @property
    def donationlogs(self):
        return self.get_cog('DonationLogs')

    @property
    def trophylogs(self):
        return self.get_cog('TrophyLogs')

    @property
    def seasonconfig(self):
        return self.get_cog('SeasonConfig')

    @property
    def utils(self):
        return self.get_cog('Utils')

    @property
    def background(self):
        return self.get_cog('BackgroundManagement')

    async def on_message(self, message):
        if message.author.bot:
            return  # ignore bot messages

        await self.process_commands(message)

    async def process_commands(self, message):
        # we have a couple attributes to add to context, lets add them now (easy db connection etc.)
        ctx = await self.get_context(message, cls=context.Context)

        if ctx.command is None:
            return  # if there's no command invoked return

        async with ctx.acquire():
            await self.invoke(ctx)

    async def on_ready(self):
        await self.utils.update_clan_tags()
        await self.change_presence(activity=discord.Game('+help for commands'))

    async def on_resumed(self):
        await self.change_presence(activity=discord.Game('+help for commands'))

    async def get_clans(self, guild_id, in_event=False):
        if in_event:
            query = "SELECT DISTINCT clan_tag FROM clans WHERE guild_id = $1 AND in_event = $2"
            fetch = await self.pool.fetch(query, guild_id, in_event)
        else:
            query = "SELECT DISTINCT clan_tag FROM clans WHERE guild_id = $1"
            fetch = await self.pool.fetch(query, guild_id)
        return await self.coc.get_clans(n[0].strip() for n in fetch).flatten()

    async def on_command_error(self, context, exception):
        return await error_handler(context, exception)

    async def on_error(self, event_method, *args, **kwargs):
        return await discord_event_error(self, event_method, *args, **kwargs)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()

    try:
        # configure the database connection
        pool = loop.run_until_complete(Table.create_pool(creds.postgres, command_timeout=60))
        redis = loop.run_until_complete(aioredis.create_redis('redis://localhost'))

        bot = DonationBot()
        bot.pool = pool  # add db as attribute
        bot.redis = redis
        setup_logging(bot)
        bot.run(creds.bot_token)  # run bot

    except Exception:
        traceback.print_exc()
