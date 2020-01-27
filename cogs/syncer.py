from discord.ext import commands, tasks


class Syncer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.syncer.start()
        self.add_new_players.start()

    def cog_unload(self):
        self.syncer.cancel()
        self.add_new_players.cancel()

    @tasks.loop(minutes=1)
    async def syncer(self):
        await self.bot.wait_until_ready()
        query = "SELECT DISTINCT clan_tag FROM clans"
        fetch = await self.bot.pool.fetch(query)

        clans = await self.bot.coc.get_clans((n[0] for n in fetch), cache=False, update_cache=False).flatten()

        players = list()

        for clan in clans:
            players.extend(
                [
                    {
                        "tag": n.tag,
                        "name": n.name,
                        "donations": n.donations,
                        "received": n.received,
                        "trophies": n.trophies,
                        "versus_trophies": n.versus_trophies,
                        "level": n.exp_level,
                        "clan_tag": n.clan and n.clan.tag,
                        "league_id": n.league_id
                    }
                    for n in clan.itermembers
                ]
            )

        query = """
                INSERT INTO players (player_tag, donations, received, trophies, name, versus_trophies, level, clan_tag, league_id) 
                SELECT x.player_tag, x.donations, x.received, x.trophies, x.name, x.versus_trophies, x.level, x.clan_tag, x.league_id
                                FROM jsonb_to_recordset($1::jsonb)
                                AS x(
                                    player_tag TEXT, 
                                    donations INTEGER, 
                                    received INTEGER, 
                                    trophies INTEGER,
                                    name TEXT,
                                    versus_trophies INTEGER,
                                    level INTEGER,
                                    clan_tag TEXT,
                                    league_id INTEGER
                                )
                ON CONFLICT (player_tag, season_id)
                DO UPDATE SET donations = players.donations + x.donations, 
                              received  = players.received  + x.received, 
                              trophies  = x.trophies,
                              name      = x.name,
                              versus_trophies = x.versus_trophies,
                              level     = x.level,
                              clan_tag  = x.clan_tag,
                              league_id = x.league_id                                     
                """
        await self.bot.pool.execute(query, players)

        query = """
                INSERT INTO eventplayers (player_tag, donations, received, trophies, name, versus_trophies, level, clan_tag, league_id, event_id, live) 
                SELECT x.player_tag, x.donations, x.received, x.trophies, x.name, x.versus_trophies, x.level, x.clan_tag, x.league_id, events.id, TRUE
                    FROM jsonb_to_recordset($1::jsonb)
                    AS x(
                        player_tag TEXT, 
                        donations INTEGER, 
                        received INTEGER, 
                        trophies INTEGER,
                        name TEXT,
                        versus_trophies INTEGER,
                        level INTEGER,
                        clan_tag TEXT,
                        league_id INTEGER
                    )
                INNER JOIN clans ON clans.clan_tag = x.player_tag
                INNER JOIN events ON events.guild_id = clans.guild_id
                WHERE events.start <= now()
                AND events.finish >= now()
                ON CONFLICT (player_tag, event_id)
                DO UPDATE SET donations = eventplayers.donations + x.donations, 
                              received  = eventplayers.received  + x.received, 
                              trophies  = x.trophies,
                              name      = x.name,
                              versus_trophies = x.versus_trophies,
                              level     = x.level,
                              clan_tag  = x.clan_tag,
                              league_id = x.league_id                                     
                """
        await self.bot.pool.execute(query, players)

    @tasks.loop(minutes=5)
    async def add_new_players(self):
        await self.bot.wait_until_ready()
        season_id = await self.bot.seasonconfig.get_season_id()
        query = "SELECT player_tag FROM players WHERE start_update = False AND season_id = $1"
        fetch = await self.bot.pool.fetch(query, season_id)

        if not fetch:
            return

        async for player in self.bot.coc.get_players((n[0] for n in fetch), cache=False, update_cache=False):
            query = """UPDATE players SET donations = $1, 
                                          received = $2, 
                                          trophies = $3, 
                                          start_trophies = $3,
                                          start_friend_in_need = $4,
                                          start_sharing_is_caring = $5,
                                          start_attacks = $6,
                                          start_defenses = $7,
                                          start_best_trophies = $8,
                                          start_update = True
                        WHERE player_tag = $9 
                        AND season_id = $10
                """
            await self.bot.pool.execute(
                query,
                player.donations,
                player.received,
                player.trophies,
                player.achievements_dict['Friend in Need'].value,
                player.achievements_dict['Sharing is caring'].value,
                player.attack_wins,
                player.defense_wins,
                player.best_trophies,
                player.tag,
                season_id
            )

            query = """WITH event AS (
                           SELECT events.id FROM events
                           INNER JOIN clans 
                           ON events.guild_id = clans.guild_id
                           INNER JOIN eventplayers 
                           ON clans.clan_tag = eventplayers.clan_tag
                           AND events.start <= now()
                           AND events.finish >= now()
                       )
                       UPDATE eventplayers 
                       SET donations = $1, 
                           received = $2, 
                           trophies = $3, 
                           start_trophies = $3,
                           start_friend_in_need = $4,
                           start_sharing_is_caring = $5,
                           start_attacks = $6,
                           start_defenses = $7,
                           start_best_trophies = $8,
                           start_update = TRUE,
                           live = TRUE
                        FROM event 
                        WHERE player_tag = $9 
                        AND event_id = event.id
                    """

            await self.bot.pool.execute(
                query,
                player.donations,
                player.received,
                player.trophies,
                player.achievements_dict['Friend in Need'].value,
                player.achievements_dict['Sharing is caring'].value,
                player.attack_wins,
                player.defense_wins,
                player.best_trophies,
                player.tag,
            )


def setup(bot):
    bot.add_cog(Syncer(bot))