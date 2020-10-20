import discord
import math
import typing


from discord.ext import commands

from cogs.utils import fuzzy, checks, formatters, paginator
from cogs.utils.converters import ClanConverter, PlayerConverter


class AutoClaim(commands.Cog, name='\u200bAutoClaim'):
    def __init__(self, bot):
        self.bot = bot
        self.running_commands = {}

    async def match_player(self, player, guild: discord.Guild, prompt=False, ctx=None,
                           score_cutoff=20, claim=True):
        matches = fuzzy.extract_matches(player.name, [n.name for n in guild.members],
                                        score_cutoff=score_cutoff, scorer=fuzzy.partial_ratio,
                                        limit=9)
        if len(matches) == 0:
            return None
        if len(matches) == 1:
            user = guild.get_member_named(matches[0][0])
            if prompt:
                m = await ctx.prompt(f'[auto-claim]: {player.name} ({player.tag}) '
                                     f'to be claimed to {str(user)} ({user.id}). '
                                     f'If already claimed, this will do nothing.')
                if m is True and claim is True:
                    query = "UPDATE players SET user_id = $1 " \
                            "WHERE player_tag = $2 AND user_id IS NULL AND season_id = $3"
                    await self.bot.pool.execute(query, user.id, player.tag,
                                                await self.bot.seasonconfig.get_season_id())
                else:
                    return False
            return user
        return [guild.get_member_named(n[0]) for n in matches]

    async def match_member(self, member, clan, claim):
        matches = fuzzy.extract_matches(member.name, [n.name for n in clan.members],
                                        score_cutoff=60)
        if len(matches) == 0:
            return None
        for i, n in enumerate(matches):
            query = "SELECT user_id FROM players WHERE player_tag = $1 AND season_id = $2"
            m = clan.get_member(name=n[0])
            fetch = await self.bot.pool.fetchrow(query, m.tag,
                                                 await self.bot.seasonconfig.get_season_id())
            if fetch is None:
                continue
            del matches[i]

        if len(matches) == 1 and claim is True:
            player = clan.get_member(name=matches[0][0])
            query = "UPDATE players SET user_id = $1 WHERE player_tag = $2 " \
                    "AND user_id IS NULL AND season_id = $3"
            await self.bot.pool.execute(query, member.id, player.tag,
                                        await self.bot.seasonconfig.get_season_id())
            return player
        elif len(matches) == 1:
            return True

        return [clan.get_member(name=n) for n in matches]

    @staticmethod
    async def send(messageable, msg, colour):
        return await messageable.send(embed=discord.Embed(colour=colour,
                                                          description=msg))

    @commands.group(name='autoclaim')
    @checks.manage_guild()
    async def auto_claim(self, ctx):
        """[Group] Manage a currently running auto-claim command.

        Automatically claim all accounts in server, through an interactive process.

        It will go through all players in claimed clans in server, matching them to discord users where possible.
        The interactive process is easy to use, and will try to guide you through as easily as possible
        """
        if ctx.invoked_subcommand is None:
            return await ctx.send_help(ctx.command)

    @auto_claim.command(name='start')
    @checks.manage_guild()
    async def auto_claim_start(self, ctx, *, clan: ClanConverter = None):
        """Automatically claim all accounts in server, through an interactive process.

        It will go through all players in claimed clans in server, matching them to discord users where possible.
        The interactive process is easy to use, and will try to guide you through as easily as possible

        **Parameters**
        :key: Clan tag, name or `all` for all clans claimed.

        **Format**
        :information_source: `+autoclaim start #CLAN_TAG`
        :information_source: `+autoclaim start CLAN NAME`
        :information_source: `+autoclaim start all`

        **Example**
        :white_check_mark: `+autoclaim start #P0LYJC8C`
        :white_check_mark: `+autoclaim start Rock Throwers`
        :white_check_mark: `+autoclaim start all`

        **Required Permissions**
        :warning: Manage Server
        """
        if self.running_commands.get(ctx.guild.id, False):
            return await ctx.send('You already have an auto-claim command running. '
                                  'Please cancel (`+help autoclaim cancel`) it before starting a new one.')

        self.running_commands[ctx.guild.id] = True

        season_id = await self.bot.seasonconfig.get_season_id()
        failed_players = []

        if not clan:
            clan = await ctx.get_clans()

        prompt = await ctx.prompt(
            'Would you like to be asked to confirm before the bot claims matching accounts? '
            'Else you can un-claim and reclaim if there is an incorrect claim.'
        )
        if prompt is None:
            return

        await ctx.send('You can stop this command at any time by running `+autoclaim cancel`')

        match_player = self.match_player

        for c in clan:
            for member in c.members:
                if self.running_commands[ctx.guild.id] is False:
                    del self.running_commands[ctx.guild.id]
                    return await ctx.send('autoclaim command stopped.')

                query = """SELECT id 
                           FROM players 
                           WHERE player_tag = $1 
                           AND user_id IS NOT NULL 
                           AND season_id = $2;
                        """
                fetch = await ctx.db.fetchrow(query, member.tag, season_id)
                if fetch:
                    continue

                results = await match_player(member, ctx.guild, prompt, ctx)
                if not results:
                    msg = await self.send(ctx, f'[auto-claim]: No members found for {member.name} ({member.tag})',
                                          discord.Colour.red())
                    failed_players.append([member, msg])
                    continue
                    # no members found in guild with that player name
                if isinstance(results, discord.Member):
                    await self.send(ctx, f'[auto-claim]: {member.name} ({member.tag}) '
                                         f'has been claimed to {str(results)} ({results.id})',
                                    discord.Colour.green())
                    continue

                table = formatters.TabularData()
                table.set_columns(['Option', 'user#disrim'])
                table.add_rows([i + 1, str(n)] for i, n in enumerate(results))
                result = await ctx.prompt(f'[auto-claim]: For player {member.name} ({member.tag})\n'
                                          f'Corresponding members found:\n'
                                          f'```\n{table.render()}\n```', additional_options=len(results))
                if isinstance(result, int):
                    query = "UPDATE players SET user_id = $1 WHERE player_tag = $2 AND season_id = $3"
                    await self.bot.pool.execute(query, results[result].id, member.tag, season_id)
                if result is None or result is False:
                    msg = await self.send(ctx, f'[auto-claim]: For player {member.name} ({member.tag})\n'
                                               f'Corresponding members found, none claimed:\n'
                                               f'```\n{table.render()}\n```',
                                          colour=discord.Colour.gold()
                                          )
                    failed_players.append([member, msg])
                    continue

                await self.send(ctx, f'[auto-claim]: {member.name} ({member.tag}) '
                                     f'has been claimed to {str(results[result])} ({results[result].id})',
                                colour=discord.Colour.green())

        prompt = await ctx.prompt("Would you like to go through a list of players who weren't claimed and "
                                  "claim them now?\nI will walk you through it...")
        if not prompt:
            return await ctx.confirm()
        for player, fail_msg in failed_players:
            if self.running_commands[ctx.guild.id] is False:
                del self.running_commands[ctx.guild.id]
                return await ctx.send('autoclaim command stopped.')

            m = await ctx.send(f'Player: {player.name} ({player.tag}), Clan: {player.clan.name} ({player.clan.tag}).'
                               f'\nPlease send either a UserID, user#discrim combo, '
                               f'or mention of the person you wish to claim this account to.')

            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            msg = await self.bot.wait_for('message', check=check)
            try:
                member = await commands.MemberConverter().convert(ctx, msg.content)
            except commands.BadArgument:
                await ctx.send(
                    'Discord user not found. Moving on to next clan member. Please claim them manually.')
                continue

            query = "UPDATE players SET user_id = $1 WHERE player_tag = $2 AND season_id = $3"
            await self.bot.pool.execute(query, member.id, player.tag, season_id)
            try:
                await fail_msg.edit(embed=discord.Embed(colour=discord.Colour.green(),
                                                        description=f'[auto-claim]: {player.name} ({player.tag}) '
                                                        f'has been claimed to {str(member)} ({member.id})'
                                                        ))
                await m.delete()
                await msg.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        del self.running_commands[ctx.guild.id]
        await ctx.send('All done. Thanks!')

    @auto_claim.command(name='cancel', aliases=['stop'])
    @checks.manage_guild()
    async def auto_claim_cancel(self, ctx):
        """Cancel an on-going auto-claim command.

        **Required Permissions**
        :warning: Manage Server
        """
        self.running_commands[ctx.guild.id] = False


def setup(bot):
    bot.add_cog(AutoClaim(bot))
