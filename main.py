import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '0'))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True


class CongoBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.db = Database()
        self.guild_id = DISCORD_GUILD_ID

    async def setup_hook(self):
        await self.db.init()
        await self.load_extension('cogs.onboarding')
        await self.load_extension('cogs.scheduler')
        await self.load_extension('cogs.admin')
        await self.load_extension('cogs.user_commands')
        await self.load_extension('cogs.tracker')

        guild_obj = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild_obj)
        synced = await self.tree.sync(guild=guild_obj)
        log.info(f'Synced {len(synced)} slash commands to guild {self.guild_id}')

        # Clear any globally registered commands to avoid duplicates
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

    async def on_ready(self):
        log.info(f'Logged in as {self.user} (ID: {self.user.id})')
        await self._seed_guild_config()
        # Apply stored API key so all warera_api calls use it immediately
        config = await self.db.get_guild_config(str(self.guild_id))
        if config and config.get('warera_api_key'):
            from warera_api import set_api_key
            set_api_key(config['warera_api_key'])
            log.info('WarEra API key loaded from guild config')
        # Set bot status using the configured home country
        name = (config or {}).get('home_country_name') or 'WarEra'
        flag = (config or {}).get('home_country_flag') or ''
        await self.change_presence(activity=discord.Game(name=f'Guarding {name} {flag}'.strip()))

    async def _seed_guild_config(self):
        """
        Pre-populate guild_config from env-var seeds so the bot works after a
        database reset without requiring /setup to be re-run.  Only fills in
        fields that are currently NULL/missing; existing values are never
        overwritten.
        """
        seed = {
            'onboarding_category_id':       os.getenv('SETUP_ONBOARDING_CATEGORY_ID'),
            'embassy_category_id':          os.getenv('SETUP_EMBASSY_CATEGORY_ID'),
            'senate_role_id':               os.getenv('SETUP_SENATE_ROLE_ID'),
            'visitor_role_id':              os.getenv('SETUP_VISITOR_ROLE_ID'),
            'citizen_role_id':              os.getenv('SETUP_CITIZEN_ROLE_ID'),
            'local_role_president_id':      os.getenv('SETUP_LOCAL_ROLE_PRESIDENT_ID'),
            'local_role_vice_president_id': os.getenv('SETUP_LOCAL_ROLE_VICE_PRESIDENT_ID'),
            'local_role_mfa_id':            os.getenv('SETUP_LOCAL_ROLE_MFA_ID'),
            'local_role_economy_id':        os.getenv('SETUP_LOCAL_ROLE_ECONOMY_ID'),
            'local_role_defense_id':        os.getenv('SETUP_LOCAL_ROLE_DEFENSE_ID'),
            'local_role_congress_id':       os.getenv('SETUP_LOCAL_ROLE_CONGRESS_ID'),
            'elders_role_id':               os.getenv('SETUP_ELDERS_ROLE_ID'),
            'warera_api_key':               os.getenv('WARERA_API_KEY'),
            'home_country_id':              os.getenv('SETUP_HOME_COUNTRY_ID'),
        }
        # Drop empty/unset entries
        seed = {k: v for k, v in seed.items() if v}
        if not seed:
            return

        guild_id = str(self.guild_id)
        config = await self.db.get_guild_config(guild_id) or {}
        to_set = {k: v for k, v in seed.items() if not config.get(k)}
        if to_set:
            await self.db.set_guild_config(guild_id, **to_set)
            log.info('Seeded guild_config from env: %s', list(to_set.keys()))

        # Auto-fill home country name/flag from WarEra if only the ID is known
        config = await self.db.get_guild_config(guild_id) or {}
        if config.get('home_country_id') and not config.get('home_country_name'):
            try:
                from warera_api import get_country_by_id
                from country_flags import get_flag
                country_data = await get_country_by_id(config['home_country_id'])
                if country_data:
                    c_name = country_data.get('name', '')
                    c_flag = get_flag(country_data.get('name', ''))
                    await self.db.set_guild_config(guild_id,
                                                   home_country_name=c_name,
                                                   home_country_flag=c_flag)
                    log.info('Auto-filled home country name/flag: %s %s', c_name, c_flag)
            except Exception as e:
                log.warning('Could not auto-fill home country name/flag: %s', e)

    async def on_member_join(self, member: discord.Member):
        if member.guild.id != self.guild_id:
            return
        cog = self.get_cog('OnboardingCog')
        if cog:
            await cog.start_onboarding(member)


bot = CongoBot()

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
