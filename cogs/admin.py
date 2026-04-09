"""
Admin & Senate commands:
  /setup           — interactive wizard (admin only)
  /test-onboarding — simulate member join for a user
  /test-visitor    — complete visitor flow for a user
  /test-citizen    — complete citizen flow for a user (skips country check)
  /test-embassy    — complete embassy flow for a user
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import json
import re as _re

from country_flags import get_flag, country_channel_name
from warera_api import get_user_lite, get_government_role, get_country_by_id, extract_user_id, LOCAL_ROLES, set_api_key, batch_get_user_lite, get_government_by_country_id, get_users_by_country, classify_player_build

log = logging.getLogger(__name__)


# ── Setup wizard views ────────────────────────────────────────────────────────

class SetupCountryModal(discord.ui.Modal, title='Home Country Setup'):
    country_id = discord.ui.TextInput(
        label='WarEra Country ID (24-char hex)',
        placeholder='e.g. 6873d0ea1758b40e712b5f4c',
        required=True,
        min_length=24,
        max_length=24,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        cid = str(self.country_id.value).strip()
        country_data = await get_country_by_id(cid)
        if not country_data:
            await interaction.response.edit_message(
                content='❌ Country not found in WarEra. Check the ID and try again.',
                view=None
            )
            return
        name = country_data.get('name', cid)
        flag = get_flag(country_data.get('name', ''))
        await self.bot.db.set_guild_config(
            str(interaction.guild_id),
            home_country_id=cid, home_country_name=name, home_country_flag=flag
        )
        await interaction.response.edit_message(
            content=f'✅ Home country set to **{name} {flag}** (`{cid}`).', view=None
        )


class SetupCountryButton(discord.ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label='Enter Country ID', style=discord.ButtonStyle.primary, emoji='🌍')
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        await interaction.response.send_modal(SetupCountryModal(self.bot))

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='⏭️ Home country step skipped.', view=self)


class SetupCategorySelect(discord.ui.View):
    def __init__(self, bot, user_id: int, step: str, categories: list):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.step = step  # 'onboarding' or 'embassy'

        options = [
            discord.SelectOption(label=cat.name[:100], value=str(cat.id))
            for cat in categories[:25]
        ]
        options.append(discord.SelectOption(label='➕ Create new category', value='__create__'))

        select = discord.ui.Select(
            placeholder=f'Select {step} category…',
            options=options,
            custom_id=f'setup_category_{step}'
        )
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        value = interaction.data['values'][0]
        guild = interaction.guild

        if value == '__create__':
            if self.step == 'onboarding':
                cat = await guild.create_category('📬 Onboarding')
            else:
                cat = await guild.create_category('🏛️ Embassies')
            value = str(cat.id)

        key = 'onboarding_category_id' if self.step == 'onboarding' else 'embassy_category_id'
        await self.bot.db.set_guild_config(str(guild.id), **{key: value})

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f'✅ {self.step.capitalize()} category set.',
            view=self
        )


class SetupRoleSelect(discord.ui.View):
    def __init__(self, bot, user_id: int, step: str, roles: list,
                 db_key: str = None, can_create: bool = None, can_skip: bool = False):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.step = step  # display label, e.g. 'senate', 'visitor', 'President'
        self.db_key = db_key if db_key is not None else f'{step}_role_id'

        # Default: only visitor/citizen offer auto-create; callers can override
        _can_create = can_create if can_create is not None else step in ('visitor', 'citizen')

        options = [
            discord.SelectOption(label=r.name[:100], value=str(r.id))
            for r in roles[:24]
        ]
        if _can_create:
            # Use the step name as-is for properly-cased labels (e.g. 'President'),
            # capitalize() for lowercase legacy steps ('visitor' → 'Visitor').
            label = step if step[0].isupper() else step.capitalize()
            options.append(discord.SelectOption(label=f'➕ Create "{label}" role', value='__create__'))
        if can_skip:
            options.append(discord.SelectOption(label='⏭️ Skip (not applicable)', value='__skip__'))

        select = discord.ui.Select(
            placeholder=f'Select {step} role…',
            options=options,
            custom_id=f'setup_role_{step.lower().replace(" ", "_")}'
        )
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        value = interaction.data['values'][0]
        guild = interaction.guild

        if value == '__skip__':
            for item in self.children:
                item.disabled = True
            label = self.step if self.step[0].isupper() else self.step.capitalize()
            await interaction.response.edit_message(content=f'⏭️ {label} role skipped.', view=self)
            return

        if value == '__create__':
            role_name = self.step if self.step[0].isupper() else self.step.capitalize()
            role = await guild.create_role(name=role_name, mentionable=True)
            value = str(role.id)

        await self.bot.db.set_guild_config(str(guild.id), **{self.db_key: value})

        for item in self.children:
            item.disabled = True
        label = self.step if self.step[0].isupper() else self.step.capitalize()
        await interaction.response.edit_message(
            content=f'✅ {label} role set.',
            view=self
        )


class SetupApiKeyModal(discord.ui.Modal, title='WarEra API Key'):
    api_key = discord.ui.TextInput(
        label='API Key',
        placeholder='Paste your WarEra API key here (leave blank to clear)',
        required=False,
        max_length=200,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        key = str(self.api_key.value).strip() or None
        await self.bot.db.set_guild_config(str(interaction.guild_id), warera_api_key=key)
        set_api_key(key)
        status = 'set ✅' if key else 'cleared'
        await interaction.response.edit_message(
            content=f'✅ WarEra API key {status}. Rate limit is now **{"200" if key else "100"} req/min**.',
            view=None
        )


class SetupApiKeyButton(discord.ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label='Enter API Key', style=discord.ButtonStyle.primary, emoji='🔑')
    async def enter_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        await interaction.response.send_modal(SetupApiKeyModal(self.bot))

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='⏭️ API key step skipped.', view=self)


class SetupChannelSelect(discord.ui.View):
    """Channel picker for the eco/war alert channel during /setup."""
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id

        select = discord.ui.ChannelSelect(
            placeholder='Select alert channel for eco/war shift warnings…',
            channel_types=[discord.ChannelType.text],
            custom_id='setup_eco_war_channel',
        )
        select.callback = self._callback
        self.add_item(select)

        skip_btn = discord.ui.Button(label='Skip', style=discord.ButtonStyle.secondary)
        skip_btn.callback = self._skip
        self.add_item(skip_btn)

    async def _callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        channel_id = str(interaction.data['values'][0])
        await self.bot.db.set_guild_config(str(interaction.guild.id),
                                           eco_war_alert_channel_id=channel_id)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content='✅ Eco/war alert channel set.', view=self
        )

    async def _skip(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content='⏭️ Eco/war alert channel skipped.', view=self
        )


class SetupThresholdModal(discord.ui.Modal, title='Eco/War Alert Threshold'):
    threshold = discord.ui.TextInput(
        label='% change to trigger alert (1–100)',
        placeholder='20',
        required=False,
        max_length=3,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.threshold.value).strip()
        try:
            val = max(1, min(100, int(raw))) if raw else 20
        except ValueError:
            val = 20
        await self.bot.db.set_guild_config(str(interaction.guild_id), eco_war_threshold=val)
        await interaction.response.edit_message(
            content=f'✅ Eco/war alert threshold set to **{val}%**.', view=None
        )


class SetupThresholdButton(discord.ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label='Set threshold', style=discord.ButtonStyle.primary, emoji='📊')
    async def set_threshold(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        await interaction.response.send_modal(SetupThresholdModal(self.bot))

    @discord.ui.button(label='Use default (20%)', style=discord.ButtonStyle.secondary)
    async def use_default(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        await self.bot.db.set_guild_config(str(interaction.guild_id), eco_war_threshold=20)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content='✅ Eco/war threshold set to default **20%**.', view=self
        )

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Not your setup.', ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content='⏭️ Eco/war threshold step skipped.', view=self
        )


# ── Admin Cog ─────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog, name='AdminCog'):
    def __init__(self, bot):
        self.bot = bot

    async def _is_senate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        config = await self.bot.db.get_guild_config(str(interaction.guild.id))
        if not config or not config.get('senate_role_id'):
            return False
        senate_role = interaction.guild.get_role(int(config['senate_role_id']))
        return senate_role is not None and senate_role in interaction.user.roles

    # ── /setup ────────────────────────────────────────────────────────────────

    @app_commands.command(name='setup', description='Configure the bot (admin only).')
    @app_commands.default_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction):
        guild = interaction.guild
        categories = [c for c in guild.categories]
        roles = [r for r in guild.roles if not r.is_default() and not r.managed]

        config = await self.bot.db.get_guild_config(str(guild.id)) or {}
        country_name = config.get('home_country_name') or 'your country'
        cur_country = (
            f'{config["home_country_name"]} {config.get("home_country_flag", "")}'.strip()
            if config.get('home_country_name') else '*(not set)*'
        )
        total_steps = 10 + len(LOCAL_ROLES)

        # Step 1: Home country ID
        await interaction.response.send_message(
            f'**Bot Setup — Step 1/{total_steps}** — Set your **home WarEra country** '
            f'(used for citizen verification and government role sync).\n'
            f'Current: {cur_country}',
            view=SetupCountryButton(self.bot, interaction.user.id),
            ephemeral=True
        )

        # Show a summary of current config
        if config:
            lines = []
            for key, label in [
                ('home_country_name',      'Home country'),
                ('onboarding_category_id', 'Onboarding category'),
                ('embassy_category_id',    'Embassy category'),
                ('senate_role_id',         'Senate role'),
                ('visitor_role_id',        'Visitor role'),
                ('citizen_role_id',        'Citizen role'),
            ] + [(db_key, f'{country_name} {name}') for _, db_key, name in LOCAL_ROLES]:
                val = config.get(key)
                if key == 'home_country_name':
                    flag = config.get('home_country_flag', '')
                    display = f'{val} {flag}'.strip() if val else None
                    lines.append(f'{"✅" if display else "❌"} {label}: **{display}**' if display else f'❌ {label}: *not set*')
                elif val:
                    obj = guild.get_channel(int(val)) or guild.get_role(int(val))
                    lines.append(f'✅ {label}: **{obj.name if obj else val}**')
                else:
                    lines.append(f'❌ {label}: *not set*')
            await interaction.followup.send('\n'.join(lines), ephemeral=True)

        await interaction.followup.send(
            f'**Step 2/{total_steps}** — Select the category where **onboarding channels** will be created:',
            view=SetupCategorySelect(self.bot, interaction.user.id, 'onboarding', categories),
            ephemeral=True
        )
        await interaction.followup.send(
            f'**Step 3/{total_steps}** — Select the **Embassy category** (or create one):',
            view=SetupCategorySelect(self.bot, interaction.user.id, 'embassy', categories),
            ephemeral=True
        )
        await interaction.followup.send(
            f'**Step 4/{total_steps}** — Select the **Senate role** (existing only):',
            view=SetupRoleSelect(self.bot, interaction.user.id, 'senate', roles),
            ephemeral=True
        )
        await interaction.followup.send(
            f'**Step 5/{total_steps}** — Select or create the **Visitor role**:',
            view=SetupRoleSelect(self.bot, interaction.user.id, 'visitor', roles),
            ephemeral=True
        )
        await interaction.followup.send(
            f'**Step 6/{total_steps}** — Select or create the **Citizen role**:',
            view=SetupRoleSelect(self.bot, interaction.user.id, 'citizen', roles),
            ephemeral=True
        )
        # Steps 7–12: home-country government roles (local Discord roles for citizens)
        for i, (_, db_key, display_name) in enumerate(LOCAL_ROLES, start=7):
            await interaction.followup.send(
                f'**Step {i}/{total_steps}** — Select or create the **{display_name}** role '
                f'(local {country_name} government role):',
                view=SetupRoleSelect(
                    self.bot, interaction.user.id, display_name, roles,
                    db_key=db_key, can_create=True
                ),
                ephemeral=True
            )
        # Elders/Retirement role — optional, exempt from re-verification
        await interaction.followup.send(
            f'**Step {total_steps - 3}/{total_steps}** — (Optional) Select the **Elders / Retirement role**.\n'
            'Members with this role will be exempt from `/admin-reverify-government`.',
            view=SetupRoleSelect(
                self.bot, interaction.user.id, 'Elders/Retirement', roles,
                db_key='elders_role_id', can_create=False, can_skip=True
            ),
            ephemeral=True
        )
        # Optional WarEra API key (doubles rate limit to 200 req/min)
        api_key_set = bool(config.get('warera_api_key'))
        await interaction.followup.send(
            f'**Step {total_steps - 2}/{total_steps}** — (Optional) Set your **WarEra API key** '
            f'to raise the rate limit from 100 to **200 requests/min**.\n'
            f'Current status: {"✅ API key is set" if api_key_set else "❌ No API key — anonymous limit (100 req/min)"}',
            view=SetupApiKeyButton(self.bot, interaction.user.id),
            ephemeral=True
        )
        # Eco/war alert channel
        await interaction.followup.send(
            f'**Step {total_steps - 1}/{total_steps}** — (Optional) Select a **text channel** for '
            f'eco/war shift alerts.\nThe bot will mention the Senate role here when a tracked '
            f'country switches between eco and war builds.',
            view=SetupChannelSelect(self.bot, interaction.user.id),
            ephemeral=True
        )
        # Eco/war alert threshold
        cur_threshold = config.get('eco_war_threshold') or 20
        await interaction.followup.send(
            f'**Step {total_steps}/{total_steps}** — (Optional) Set the **eco/war alert threshold** '
            f'(% of active players that must shift to trigger an alert).\n'
            f'Current: **{cur_threshold}%**',
            view=SetupThresholdButton(self.bot, interaction.user.id),
            ephemeral=True
        )

    # ── /config ───────────────────────────────────────────────────────────────

    @app_commands.command(name='config', description='Show current bot config and env-var seed values (admin only).')
    @app_commands.default_permissions(administrator=True)
    async def config_show(self, interaction: discord.Interaction):
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id)) or {}

        fields = [
            ('home_country_id',              'SETUP_HOME_COUNTRY_ID'),
            ('home_country_name',            '# auto-fetched name'),
            ('home_country_flag',            '# auto-fetched flag'),
            ('onboarding_category_id',       'SETUP_ONBOARDING_CATEGORY_ID'),
            ('embassy_category_id',          'SETUP_EMBASSY_CATEGORY_ID'),
            ('senate_role_id',               'SETUP_SENATE_ROLE_ID'),
            ('visitor_role_id',              'SETUP_VISITOR_ROLE_ID'),
            ('citizen_role_id',              'SETUP_CITIZEN_ROLE_ID'),
            ('local_role_president_id',      'SETUP_LOCAL_ROLE_PRESIDENT_ID'),
            ('local_role_vice_president_id', 'SETUP_LOCAL_ROLE_VICE_PRESIDENT_ID'),
            ('local_role_mfa_id',            'SETUP_LOCAL_ROLE_MFA_ID'),
            ('local_role_economy_id',        'SETUP_LOCAL_ROLE_ECONOMY_ID'),
            ('local_role_defense_id',        'SETUP_LOCAL_ROLE_DEFENSE_ID'),
            ('local_role_congress_id',       'SETUP_LOCAL_ROLE_CONGRESS_ID'),
            ('elders_role_id',               'SETUP_ELDERS_ROLE_ID'),
            ('eco_war_alert_channel_id',     'SETUP_ECO_WAR_ALERT_CHANNEL_ID'),
            ('eco_war_threshold',            'SETUP_ECO_WAR_THRESHOLD'),
        ]

        lines = ['**Current guild config** (copy IDs into `.env` to survive database resets)\n```']
        for db_key, env_key in fields:
            val = config.get(db_key) or 'not set'
            lines.append(f'{env_key}={val}')
        # API key — mask all but first 6 chars
        raw_key = config.get('warera_api_key')
        masked = (raw_key[:6] + '***') if raw_key else 'not set'
        lines.append(f'WARERA_API_KEY={masked}')
        lines.append('```')

        await interaction.response.send_message('\n'.join(lines), ephemeral=True)

    # ── /admin-restore ────────────────────────────────────────────────────────

    @app_commands.command(
        name='admin-restore',
        description='[Admin] Manually restore a verified user into the database without re-running onboarding.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member='The Discord member to restore',
        role_type='The role to assign',
        warera_id='Their WarEra user ID'
    )
    @app_commands.choices(role_type=[
        app_commands.Choice(name='Visitor', value='visitor'),
        app_commands.Choice(name='Citizen', value='citizen'),
        app_commands.Choice(name='Embassy', value='embassy'),
    ])
    async def admin_restore(
        self, interaction: discord.Interaction,
        member: discord.Member,
        role_type: app_commands.Choice[str],
        warera_id: str
    ):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        if not config:
            await interaction.followup.send('Bot is not configured — run `/setup` first.', ephemeral=True)
            return

        resolved_id = extract_user_id(warera_id) or warera_id
        warera_data = await get_user_lite(resolved_id)
        if not warera_data:
            await interaction.followup.send(
                f'Could not find a WarEra user with ID `{resolved_id}`.',
                ephemeral=True
            )
            return
        warera_id = resolved_id

        warera_username = warera_data.get('username', warera_id)
        rt = role_type.value

        if rt == 'visitor':
            role = guild.get_role(int(config['visitor_role_id'])) if config.get('visitor_role_id') else None
            if not role:
                await interaction.followup.send('Visitor role is not configured.', ephemeral=True)
                return
            await member.add_roles(role)
            await self.bot.db.upsert_tracked_user(
                str(member.id), str(guild.id), warera_id, 'visitor',
                warera_data.get('country'), str(role.id)
            )
            await interaction.followup.send(
                f'✅ {member.mention} restored as **Visitor** (WarEra: `{warera_username}`).',
                ephemeral=True
            )

        elif rt == 'citizen':
            role = guild.get_role(int(config['citizen_role_id'])) if config.get('citizen_role_id') else None
            if not role:
                await interaction.followup.send('Citizen role is not configured.', ephemeral=True)
                return
            await member.add_roles(role)
            await self.bot.db.upsert_tracked_user(
                str(member.id), str(guild.id), warera_id, 'citizen',
                warera_data.get('country'), str(role.id)
            )
            await interaction.followup.send(
                f'✅ {member.mention} restored as **Citizen** (WarEra: `{warera_username}`).',
                ephemeral=True
            )

        elif rt == 'embassy':
            infos = warera_data.get('infos', {})
            role_field, access_level, country_id = get_government_role(infos)
            if not role_field:
                await interaction.followup.send(
                    f'`{warera_username}` has no government role in WarEra. Cannot restore as Embassy.',
                    ephemeral=True
                )
                return

            country_data = await get_country_by_id(country_id)
            country_name = country_data.get('name', 'Unknown') if country_data else 'Unknown'
            country_flag = get_flag(country_name)

            onboarding = self.bot.get_cog('OnboardingCog')
            if not onboarding:
                await interaction.followup.send('OnboardingCog not loaded.', ephemeral=True)
                return

            category = await onboarding._ensure_embassy_category(guild, config)
            emb_channel, base_role, write_role = await onboarding._ensure_embassy_channel_role(
                guild, category, country_name, country_flag, config
            )

            roles_to_add = [base_role]
            if access_level == 'write':
                roles_to_add.append(write_role)
            await member.add_roles(*roles_to_add)

            await self.bot.db.create_embassy_request(
                str(member.id), str(guild.id), country_id, country_name, country_flag,
                role_field, access_level
            )
            await self.bot.db.update_embassy_request(
                str(member.id), str(guild.id),
                embassy_channel_id=str(emb_channel.id),
                embassy_role_id=str(base_role.id),
                embassy_write_role_id=str(write_role.id),
                approval_status='approved'
            )
            await self.bot.db.upsert_tracked_user(
                str(member.id), str(guild.id), warera_id, 'embassy',
                country_id, str(base_role.id)
            )

            access_str = 'write access' if access_level == 'write' else 'read-only'
            await interaction.followup.send(
                f'✅ {member.mention} restored as **Embassy** — {country_name} {country_flag} '
                f'({access_str}, WarEra: `{warera_username}`).',
                ephemeral=True
            )

    # ── /test-onboarding ──────────────────────────────────────────────────────

    @app_commands.command(name='test-onboarding', description='[Senate] Simulate member join for a user.')
    @app_commands.describe(user='The Discord member to test onboarding for.')
    async def test_onboarding(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('Senate role required.', ephemeral=True)
            return
        # Remove any existing request so we start fresh
        await self.bot.db.delete_user_request(str(user.id), str(interaction.guild.id))
        cog = self.bot.get_cog('OnboardingCog')
        await interaction.response.send_message(
            f'🧪 Starting onboarding test for {user.mention}…', ephemeral=True
        )
        await cog.start_onboarding(user)

    # ── /test-visitor ─────────────────────────────────────────────────────────

    @app_commands.command(name='test-visitor', description='[Senate] Instantly complete visitor flow.')
    @app_commands.describe(user='The Discord member to test.')
    async def test_visitor(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('Senate role required.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        request = await self.bot.db.get_user_request(str(user.id), str(interaction.guild.id))
        if not request or not request.get('channel_id'):
            await interaction.followup.send(
                f'No active onboarding channel for {user.mention}. Run `/test-onboarding` first.',
                ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(int(request['channel_id']))
        if not channel:
            await interaction.followup.send('Onboarding channel not found.', ephemeral=True)
            return

        # Build a minimal fake warera_data stub so we can call complete_visitor
        fake_data = {
            '_id': request.get('warera_id') or 'test000000000000test0000',
            'username': request.get('warera_username') or user.name,
            'country': request.get('country_id') or '',
        }
        cog = self.bot.get_cog('OnboardingCog')
        await cog.complete_visitor(channel, user, fake_data)
        await interaction.followup.send(f'✅ Visitor flow completed for {user.mention}.', ephemeral=True)

    # ── /test-citizen ─────────────────────────────────────────────────────────

    @app_commands.command(name='test-citizen', description='[Senate] Instantly complete citizen flow (skips country check).')
    @app_commands.describe(user='The Discord member to test.')
    async def test_citizen(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('Senate role required.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        request = await self.bot.db.get_user_request(str(user.id), str(interaction.guild.id))
        if not request or not request.get('channel_id'):
            await interaction.followup.send(
                f'No active onboarding channel for {user.mention}. Run `/test-onboarding` first.',
                ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(int(request['channel_id']))
        if not channel:
            await interaction.followup.send('Onboarding channel not found.', ephemeral=True)
            return

        # Force warera_id and username if missing
        if not request.get('warera_id'):
            test_cfg = await self.bot.db.get_guild_config(str(guild.id)) or {}
            test_country_id   = test_cfg.get('home_country_id')   or '6873d0ea1758b40e712b5f4c'
            test_country_name = test_cfg.get('home_country_name') or 'Congo'
            await self.bot.db.update_user_request(
                str(user.id), str(interaction.guild.id),
                warera_id='test000000000000test0000',
                warera_username=user.name,
                country_id=test_country_id,
                country_name=test_country_name,
                verification_token='TESTTOKEN',
                requested_role='citizen',
                status='awaiting_company_change'
            )
        cog = self.bot.get_cog('OnboardingCog')
        await cog.complete_citizen(channel, user)
        await interaction.followup.send(f'✅ Citizen flow completed for {user.mention}.', ephemeral=True)

    # ── /test-embassy ─────────────────────────────────────────────────────────

    @app_commands.command(name='test-embassy', description='[Senate] Instantly complete embassy flow.')
    @app_commands.describe(user='The Discord member to test.')
    async def test_embassy(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('Senate role required.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        request = await self.bot.db.get_user_request(str(user.id), str(interaction.guild.id))
        if not request or not request.get('channel_id'):
            await interaction.followup.send(
                f'No active onboarding channel for {user.mention}. Run `/test-onboarding` first.',
                ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(int(request['channel_id']))
        if not channel:
            await interaction.followup.send('Onboarding channel not found.', ephemeral=True)
            return

        # Ensure an embassy_request record exists for the test
        emb = await self.bot.db.get_embassy_request(str(user.id), str(interaction.guild.id))
        test_cfg      = await self.bot.db.get_guild_config(str(interaction.guild.id)) or {}
        test_cid      = test_cfg.get('home_country_id')   or '6873d0ea1758b40e712b5f4c'
        test_cname    = test_cfg.get('home_country_name') or 'Congo'
        test_cflag    = test_cfg.get('home_country_flag') or '🇨🇬'
        if not emb:
            await self.bot.db.create_embassy_request(
                str(user.id), str(interaction.guild.id),
                test_cid, test_cname, test_cflag,
                'presidentOf', 'write'
            )
        if not request.get('warera_id'):
            await self.bot.db.update_user_request(
                str(user.id), str(interaction.guild.id),
                warera_id='test000000000000test0000',
                warera_username=user.name,
                country_id=test_cid,
                country_name=test_cname,
                verification_token='TESTTOKEN',
                requested_role='embassy',
                status='awaiting_company_change'
            )
        cog = self.bot.get_cog('OnboardingCog')
        await cog.complete_embassy(channel, user)
        await interaction.followup.send(f'✅ Embassy flow completed for {user.mention}.', ephemeral=True)


    # ── /addwrite ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name='addwrite',
        description='[Pres/VP/MoFA] Grant write access in your embassy to a registered member.'
    )
    @app_commands.describe(user='The Discord member to grant write access to.')
    async def addwrite(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # 1. Verify the invoker is a tracked embassy official with write-level role
        grantor_tracked = await self.bot.db.get_tracked_user(
            str(interaction.user.id), str(guild.id)
        )
        if not grantor_tracked or grantor_tracked.get('assigned_role') != 'embassy':
            await interaction.followup.send(
                'You must be a registered embassy official to use this command.', ephemeral=True
            )
            return

        warera_data = await get_user_lite(grantor_tracked['warera_id'])
        if not warera_data:
            await interaction.followup.send(
                'Could not verify your WarEra account. Try again later.', ephemeral=True
            )
            return

        infos = warera_data.get('infos', {})
        role_field, access_level, country_id = get_government_role(infos)
        if access_level != 'write':
            await interaction.followup.send(
                'Only **Presidents**, **Vice Presidents**, or **Ministers of Foreign Affairs** '
                'can grant write access.', ephemeral=True
            )
            return

        # 2. Verify the target is in the same country's embassy
        if user.id == interaction.user.id:
            await interaction.followup.send('You cannot grant write access to yourself.', ephemeral=True)
            return

        target_tracked = await self.bot.db.get_tracked_user(str(user.id), str(guild.id))
        if not target_tracked or target_tracked.get('country_id') != country_id:
            await interaction.followup.send(
                f'{user.mention} is not registered in your country\'s embassy.', ephemeral=True
            )
            return

        # 3. Find the write role for this country (predictable name)
        embassy_req = await self.bot.db.get_embassy_request(str(user.id), str(guild.id))
        if not embassy_req or not embassy_req.get('embassy_write_role_id'):
            await interaction.followup.send(
                'Could not find the embassy write role. Make sure the embassy is set up.', ephemeral=True
            )
            return

        write_role = guild.get_role(int(embassy_req['embassy_write_role_id']))
        if not write_role:
            await interaction.followup.send(
                'The embassy write role no longer exists. Please contact an admin.', ephemeral=True
            )
            return

        if write_role in user.roles:
            await interaction.followup.send(
                f'{user.mention} already has write access.', ephemeral=True
            )
            return

        # 4. Grant the write role
        try:
            await user.add_roles(write_role, reason=f'Write access granted by {interaction.user}')
        except discord.Forbidden:
            await interaction.followup.send(
                'I do not have permission to assign that role.', ephemeral=True
            )
            return

        # 5. Record the grant
        await self.bot.db.add_write_grant(
            grantor_discord_id=str(interaction.user.id),
            grantor_warera_id=grantor_tracked['warera_id'],
            grantee_discord_id=str(user.id),
            guild_id=str(guild.id),
            country_id=country_id,
            write_role_id=str(write_role.id)
        )

        # Notify in the embassy channel if it exists
        if embassy_req.get('embassy_channel_id'):
            emb_channel = guild.get_channel(int(embassy_req['embassy_channel_id']))
            if emb_channel:
                await emb_channel.send(
                    f'✅ {interaction.user.mention} has granted **write access** to {user.mention}.'
                )

        await interaction.followup.send(
            f'✅ Write access granted to {user.mention}.\n'
            '⚠️ This access will be automatically revoked if you lose your government role.',
            ephemeral=True
        )


    # ── /admin-db-status ──────────────────────────────────────────────────────

    @app_commands.command(
        name='admin-db-status',
        description='[Admin] List all members tracked in the database and verify their Discord roles match.'
    )
    @app_commands.default_permissions(administrator=True)
    async def admin_db_status(self, interaction: discord.Interaction):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        write_grants = await self.bot.db.get_all_write_grants(str(guild.id))

        if not tracked:
            await interaction.followup.send('No members in the database.', ephemeral=True)
            return

        role_emoji = {'visitor': '👤', 'citizen': '🇨🇬', 'embassy': '🏛️'}

        lines = [f'**Database status — {len(tracked)} tracked member(s)**\n']

        for t in tracked:
            member = guild.get_member(int(t['discord_id']))
            emoji = role_emoji.get(t['assigned_role'], '❓')
            name = member.mention if member else f'*(left server — ID {t["discord_id"]})*'

            # Check that their Discord role still exists and they still hold it
            discord_role = guild.get_role(int(t['discord_role_id'])) if t.get('discord_role_id') else None
            if not discord_role:
                role_status = '⚠️ role deleted'
            elif member and discord_role not in member.roles:
                role_status = '⚠️ role missing from member'
            else:
                role_status = f'✅ `{discord_role.name}`'

            country = f'`{t["country_id"]}`' if t.get('country_id') else '—'
            lines.append(
                f'{emoji} {name}\n'
                f'  Role: **{t["assigned_role"]}** | {role_status}\n'
                f'  WarEra ID: `{t["warera_id"]}` | Country: {country}'
            )

            # Show write grants where this member is the grantee
            grants_for = [g for g in write_grants if g['grantee_discord_id'] == t['discord_id']]
            for g in grants_for:
                grantor = guild.get_member(int(g['grantor_discord_id']))
                grantor_name = grantor.display_name if grantor else g['grantor_discord_id']
                write_role = guild.get_role(int(g['write_role_id'])) if g.get('write_role_id') else None
                wr_status = '✅' if (write_role and member and write_role in member.roles) else '⚠️ role missing'
                lines.append(f'  ✍️ Write grant from **{grantor_name}** {wr_status}')

            lines.append('')

        # Split into chunks to stay under Discord's 2000-char limit
        chunks, current = [], ''
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line + '\n'
            else:
                current += line + '\n'
        if current:
            chunks.append(current)

        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    # ── /admin-restore-write ──────────────────────────────────────────────────

    @app_commands.command(
        name='admin-restore-write',
        description='[Admin] Restore a write grant between two embassy members after a database reset.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        grantor='The official who originally granted write access',
        grantee='The member who received write access'
    )
    async def admin_restore_write(
        self, interaction: discord.Interaction,
        grantor: discord.Member,
        grantee: discord.Member
    ):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        grantor_tracked = await self.bot.db.get_tracked_user(str(grantor.id), str(guild.id))
        if not grantor_tracked or grantor_tracked.get('assigned_role') != 'embassy':
            await interaction.followup.send(
                f'{grantor.mention} is not registered as an embassy member. '
                'Run `/admin-restore` for them first.',
                ephemeral=True
            )
            return

        grantee_tracked = await self.bot.db.get_tracked_user(str(grantee.id), str(guild.id))
        if not grantee_tracked:
            await interaction.followup.send(
                f'{grantee.mention} is not registered in the database. '
                'Run `/admin-restore` for them first.',
                ephemeral=True
            )
            return

        embassy_req = await self.bot.db.get_embassy_request(str(grantor.id), str(guild.id))
        if not embassy_req or not embassy_req.get('embassy_write_role_id'):
            await interaction.followup.send(
                f'Could not find the embassy write role for {grantor.mention}\'s country. '
                'Make sure their embassy is restored first.',
                ephemeral=True
            )
            return

        write_role = guild.get_role(int(embassy_req['embassy_write_role_id']))
        if not write_role:
            await interaction.followup.send(
                'The embassy write role no longer exists in Discord. '
                'Re-run `/admin-restore` for the grantor to recreate it.',
                ephemeral=True
            )
            return

        if write_role not in grantee.roles:
            try:
                await grantee.add_roles(write_role, reason=f'Write grant restored by {interaction.user}')
            except discord.Forbidden:
                await interaction.followup.send(
                    'I do not have permission to assign that role.', ephemeral=True
                )
                return

        await self.bot.db.add_write_grant(
            grantor_discord_id=str(grantor.id),
            grantor_warera_id=grantor_tracked['warera_id'],
            grantee_discord_id=str(grantee.id),
            guild_id=str(guild.id),
            country_id=embassy_req['country_id'],
            write_role_id=str(write_role.id)
        )

        await interaction.followup.send(
            f'✅ Write grant restored: {grantor.mention} → {grantee.mention} '
            f'({embassy_req.get("country_name", "unknown country")}).',
            ephemeral=True
        )


    # ── /admin-restore-localroles ─────────────────────────────────────────────

    @app_commands.command(
        name='admin-restore-localroles',
        description='[Admin] Re-link all citizens to their correct home-country government Discord roles.'
    )
    @app_commands.default_permissions(administrator=True)
    async def admin_restore_localroles(self, interaction: discord.Interaction):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        if not config:
            await interaction.followup.send('Bot is not configured — run `/setup` first.', ephemeral=True)
            return

        # Check that at least one local government role is configured
        configured = [db_key for _, db_key, _ in LOCAL_ROLES if config.get(db_key)]
        if not configured:
            await interaction.followup.send(
                '⚠️ No home-country government roles are configured yet. '
                'Run `/setup` and assign the President, Vice President, etc. roles first.',
                ephemeral=True
            )
            return

        onboarding = self.bot.get_cog('OnboardingCog')
        if not onboarding:
            await interaction.followup.send('OnboardingCog not loaded.', ephemeral=True)
            return

        home_country_id = config.get('home_country_id') or ''
        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        # Process citizens, embassy members, and visitors — sync_local_roles checks
        # the home country ID per role, so it's a no-op for members from other countries.
        # Visitors who are home-country citizens/government officials also need syncing.
        eligible = [
            t for t in tracked
            if t.get('assigned_role') in ('citizen', 'embassy', 'visitor')
        ]

        # discord_id → set of db_keys the member was confirmed to qualify for
        qualified: dict[str, set] = {}
        # discord_ids where the WarEra API failed — don't touch their roles
        api_failed: set[str] = set()

        # Pre-fetch all WarEra data in batch to minimise HTTP calls
        eligible_ids = [t['warera_id'] for t in eligible]
        warera_results = await batch_get_user_lite(eligible_ids)
        warera_map = {uid: data for uid, data in zip(eligible_ids, warera_results) if data}

        # Pre-fetch home-country government data once for all members in this run
        congo_govt = await get_government_by_country_id(home_country_id)

        updated, errors = 0, 0
        detail_lines: list[str] = []
        for t in eligible:
            member = guild.get_member(int(t['discord_id']))
            if not member:
                continue
            warera_data = warera_map.get(t['warera_id'])
            if not warera_data:
                errors += 1
                api_failed.add(str(t['discord_id']))
                continue

            # Skip visitors who are not home-country citizens — nothing to sync for them
            if t.get('assigned_role') == 'visitor' and warera_data.get('country') != home_country_id:
                continue

            # Home-country embassy members also get the Citizen role — check live WarEra data
            if t.get('assigned_role') == 'embassy' and warera_data.get('country') == home_country_id:
                citizen_role_id = config.get('citizen_role_id')
                if citizen_role_id:
                    citizen_role = guild.get_role(int(citizen_role_id))
                    if citizen_role and citizen_role not in member.roles:
                        try:
                            await member.add_roles(citizen_role)
                        except discord.Forbidden:
                            pass

            added, removed_from_member, add_err, rem_err = await onboarding.sync_local_roles(
                guild, member, warera_data, config, govt_data=congo_govt
            )
            updated += 1

            if add_err:
                detail_lines.append(
                    f'⚠️ {member.mention}: failed to **add** '
                    f'{[r.name for r in added]} — `{add_err}`'
                )
            elif added:
                detail_lines.append(
                    f'➕ {member.mention}: added {[r.name for r in added]}'
                )
            if rem_err:
                detail_lines.append(
                    f'⚠️ {member.mention}: failed to **remove** '
                    f'{[r.name for r in removed_from_member]} — `{rem_err}`'
                )
            elif removed_from_member:
                detail_lines.append(
                    f'➖ {member.mention}: removed {[r.name for r in removed_from_member]}'
                )

            # Record which roles this member legitimately holds (use government endpoint
            # data — LOCAL_ROLES uses government field names, not getUserLite.infos)
            user_id = warera_data.get('_id', '')
            if user_id and congo_govt:
                for gf, db_key, _ in LOCAL_ROLES:
                    val = congo_govt.get(gf)
                    if val is None:
                        continue
                    has_role = (user_id in val) if isinstance(val, list) else (val == user_id)
                    if has_role:
                        qualified.setdefault(str(member.id), set()).add(db_key)

        # Second pass: strip government roles from anyone who currently holds one
        # but was not confirmed as a qualifying citizen above.
        # Skip entirely if the government API was unavailable to avoid stripping roles
        # when we can't verify membership.
        removed = 0
        if congo_govt is not None:
            for _, db_key, display_name in LOCAL_ROLES:
                role_id = config.get(db_key)
                if not role_id:
                    continue
                discord_role = guild.get_role(int(role_id))
                if not discord_role:
                    continue
                for m in list(discord_role.members):
                    mid = str(m.id)
                    if mid in api_failed:
                        continue
                    if db_key in qualified.get(mid, set()):
                        continue
                    try:
                        await m.remove_roles(
                            discord_role, reason=f'Local role audit: does not qualify for {display_name}'
                        )
                        removed += 1
                    except Exception as e:
                        detail_lines.append(f'⚠️ {m.mention}: failed to remove `{discord_role.name}` — `{e}`')

        country_label = config.get('home_country_name') or 'home country'
        parts = [f'✅ Synced {country_label} government roles for **{updated}** member(s).']
        if removed:
            parts.append(f'🗑️ Removed unqualified assignments from **{removed}** member(s).')
        if errors:
            parts.append(f'⚠️ WarEra API failed for **{errors}** member(s) (roles left unchanged).')
        if detail_lines:
            parts.append('\n' + '\n'.join(detail_lines))
        await interaction.followup.send('\n'.join(parts), ephemeral=True)

    # ── /admin-diagnose-member ────────────────────────────────────────────────

    @app_commands.command(
        name='admin-diagnose-member',
        description='[Admin] Show raw WarEra data and local role sync result for a specific member.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member='The Discord member to diagnose.')
    async def admin_diagnose_member(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id)) or {}

        tracked = await self.bot.db.get_tracked_user(str(member.id), str(guild.id))
        lines = [f'**Diagnosis for {member.mention}**\n']

        # DB record
        if tracked:
            lines.append(
                f'**DB:** `assigned_role={tracked.get("assigned_role")}` | '
                f'`country_id={tracked.get("country_id")}` | '
                f'`warera_id={tracked.get("warera_id")}`'
            )
        else:
            lines.append('**DB:** ⚠️ Not found in `tracked_users`')
            await interaction.followup.send('\n'.join(lines), ephemeral=True)
            return

        # WarEra data
        warera_data = await get_user_lite(tracked['warera_id'])
        if not warera_data:
            lines.append('**WarEra:** ❌ API returned nothing for this warera_id')
            await interaction.followup.send('\n'.join(lines), ephemeral=True)
            return

        home_cid = config.get('home_country_id') or '(not configured)'
        lines.append(
            f'**WarEra country:** `{warera_data.get("country")}`  '
            f'(home country = `{home_cid}`)'
        )
        infos = warera_data.get('infos') or {}
        lines.append(f'**WarEra infos:** `{infos}`')

        # Per-role diagnosis — fetch government API (same source as sync_local_roles)
        warera_id = warera_data.get('_id', '')
        congo_govt = await get_government_by_country_id(config.get('home_country_id') or '')
        if congo_govt is None:
            lines.append('\n⚠️ **Government API unavailable** — role check below uses user infos only (may be inaccurate)')

        _GOVT_TO_INFOS = {
            'president': 'presidentOf',
            'vicePresident': 'vicePresidentOf',
            'minOfForeignAffairs': 'minOfForeignAffairsOf',
            'minOfEconomy': 'minOfEconomyOf',
            'minOfDefense': 'minOfDefenseOf',
            'congressMembers': 'congressMemberOf',
        }

        lines.append('\n**Local role check:**')
        for warera_field, db_key, display_name in LOCAL_ROLES:
            role_id = config.get(db_key)
            if not role_id:
                lines.append(f'  ⚙️ **{display_name}**: not configured in /setup')
                continue
            discord_role = guild.get_role(int(role_id))
            if not discord_role:
                lines.append(f'  ⚠️ **{display_name}**: role ID `{role_id}` no longer exists in Discord')
                continue

            # Check via government API (matches sync_local_roles logic)
            govt_val = (congo_govt or {}).get(warera_field)
            if isinstance(govt_val, list):
                has_warera_role = bool(warera_id) and warera_id in govt_val
            else:
                has_warera_role = bool(warera_id) and govt_val == warera_id

            # Also show the user's infos field for context
            infos_field = _GOVT_TO_INFOS.get(warera_field, warera_field)
            infos_value = infos.get(infos_field)

            has_discord_role = discord_role in member.roles
            status = '✅' if has_warera_role == has_discord_role else '❌ mismatch'
            lines.append(
                f'  {status} **{display_name}**: '
                f'WarEra `{infos_field}`=`{infos_value}` → has_role={has_warera_role} | '
                f'Discord role present={has_discord_role}'
            )

        await interaction.followup.send('\n'.join(lines), ephemeral=True)

    # ── /admin-eco-status ────────────────────────────────────────────────────

    @app_commands.command(
        name='admin-eco-status',
        description='[Admin] Show current eco/war/hybrid build breakdown for a tracked country.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def admin_eco_status(self, interaction: discord.Interaction, country_id: str):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('No permission.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        existing = await self.bot.db.get_tracked_country(country_id)
        country_name = (existing.get('country_name') if existing else None) or country_id
        country_flag = (existing.get('country_flag') if existing else None) or ''

        await interaction.followup.send(
            f'Fetching skill data for **{country_name} {country_flag}**… this may take a moment.',
            ephemeral=True
        )

        # Paginate all users in this country
        all_items, cursor = [], None
        while True:
            page = await get_users_by_country(country_id, cursor)
            if not page:
                break
            items = (
                page.get('items')
                or (page.get('json') or {}).get('items')
                or []
            )
            all_items.extend(items)
            cursor = (
                page.get('nextCursor')
                or (page.get('json') or {}).get('nextCursor')
            )
            if not cursor:
                break

        if not all_items:
            await interaction.followup.send(
                f'Could not fetch users for `{country_id}`.', ephemeral=True
            )
            return

        from datetime import datetime, timedelta
        from cogs.tracker import _parse_last_online, ACTIVE_MIN_LEVEL

        user_ids = []
        seen: set = set()
        uid_map = {}
        for u in all_items:
            uid = u.get('_id') or u.get('id') or u.get('userId')
            if uid and uid not in seen:
                seen.add(uid)
                user_ids.append(uid)
                uid_map[uid] = u

        results = await batch_get_user_lite(user_ids)

        now_utc = datetime.utcnow()
        active_threshold = now_utc - timedelta(days=7)
        new_threshold = now_utc - timedelta(days=4)
        eco = war = hybrid = uncategorized = skipped = 0

        for uid, r in zip(user_ids, results):
            if not isinstance(r, dict):
                continue
            created_ts = _parse_last_online((uid_map.get(uid) or {}).get('createdAt'))
            if created_ts and created_ts > new_threshold:
                skipped += 1
                continue
            ts = _parse_last_online((r.get('dates') or {}).get('lastConnectionAt'))
            if created_ts and ts and now_utc - created_ts > timedelta(days=4) and ts - created_ts <= timedelta(hours=48):
                skipped += 1
                continue
            level = (r.get('leveling') or {}).get('level')
            if not ts or ts < active_threshold or level is None or level <= ACTIVE_MIN_LEVEL:
                continue
            build = classify_player_build(r.get('skills') or {})
            if build == 'eco':
                eco += 1
            elif build == 'war':
                war += 1
            elif build == 'hybrid':
                hybrid += 1
            else:
                uncategorized += 1

        active = eco + war + hybrid + uncategorized
        if active == 0:
            await interaction.followup.send(
                f'No active players found for **{country_name} {country_flag}**.',
                ephemeral=True
            )
            return

        eco_pct    = eco    / active * 100
        war_pct    = war    / active * 100
        hybrid_pct = hybrid / active * 100

        # Last stored snapshot for comparison
        guild_id = str(interaction.guild.id)
        prev = await self.bot.db.get_last_eco_war_snapshot(guild_id, country_id)
        prev_line = ''
        if prev and prev['active_players']:
            p = prev['active_players']
            prev_line = (
                f'\n*Previous snapshot ({prev["snapshot_time"][:16]}):* '
                f'🌱 {prev["eco_count"]/p*100:.0f}% '
                f'⚔️ {prev["war_count"]/p*100:.0f}% '
                f'🔀 {prev["hybrid_count"]/p*100:.0f}%'
            )

        await interaction.followup.send(
            f'**Eco/War status — {country_name} {country_flag}**\n'
            f'Active players: **{active}** (skipped new/ghost: {skipped})\n'
            f'🌱 Eco:    **{eco_pct:.0f}%** ({eco})\n'
            f'⚔️ War:    **{war_pct:.0f}%** ({war})\n'
            f'🔀 Hybrid: **{hybrid_pct:.0f}%** ({hybrid})\n'
            f'❓ Uncategorized: {uncategorized}'
            f'{prev_line}',
            ephemeral=True
        )

    @app_commands.command(name='backup-db', description='[Admin] Force an immediate database backup.')
    @app_commands.default_permissions(administrator=True)
    async def backup_db(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.db.backup()
            await interaction.followup.send('✅ Database backed up successfully.', ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f'❌ Backup failed: {e}', ephemeral=True)

    # ── /senate-addwrite ──────────────────────────────────────────────────────

    @app_commands.command(
        name='senate-addwrite',
        description='[Senate] Grant write access in a member\'s embassy as a Senate guarantor.'
    )
    @app_commands.describe(user='The embassy member to grant write access to.')
    async def senate_addwrite(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_senate(interaction):
            await interaction.response.send_message('Senate role required.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if user.id == interaction.user.id:
            await interaction.followup.send('You cannot grant write access to yourself.', ephemeral=True)
            return

        # Target must be a tracked embassy member
        target_tracked = await self.bot.db.get_tracked_user(str(user.id), str(guild.id))
        if not target_tracked or target_tracked.get('assigned_role') != 'embassy':
            await interaction.followup.send(
                f'{user.mention} is not registered as an embassy member.', ephemeral=True
            )
            return

        embassy_req = await self.bot.db.get_embassy_request(str(user.id), str(guild.id))
        if not embassy_req or not embassy_req.get('embassy_write_role_id'):
            await interaction.followup.send(
                f'Could not find the embassy write role for {user.mention}\'s country. '
                'Make sure their embassy is set up.', ephemeral=True
            )
            return

        write_role = guild.get_role(int(embassy_req['embassy_write_role_id']))
        if not write_role:
            await interaction.followup.send(
                'The embassy write role no longer exists. Please contact an admin.', ephemeral=True
            )
            return

        if write_role in user.roles:
            await interaction.followup.send(
                f'{user.mention} already has write access.', ephemeral=True
            )
            return

        try:
            await user.add_roles(write_role, reason=f'Senate write grant by {interaction.user}')
        except discord.Forbidden:
            await interaction.followup.send(
                'I do not have permission to assign that role.', ephemeral=True
            )
            return

        # Record grant with grant_type='senate'; grantor_warera_id is empty (no WarEra ID needed)
        await self.bot.db.add_write_grant(
            grantor_discord_id=str(interaction.user.id),
            grantor_warera_id='',
            grantee_discord_id=str(user.id),
            guild_id=str(guild.id),
            country_id=embassy_req['country_id'],
            write_role_id=str(write_role.id),
            grant_type='senate'
        )

        # Notify in the embassy channel
        if embassy_req.get('embassy_channel_id'):
            emb_channel = guild.get_channel(int(embassy_req['embassy_channel_id']))
            if emb_channel:
                await emb_channel.send(
                    f'✅ Senator {interaction.user.mention} has granted **write access** to {user.mention}.'
                )

        await interaction.followup.send(
            f'✅ Write access granted to {user.mention}.\n'
            '⚠️ This access will be automatically revoked if you lose your Senate role.',
            ephemeral=True
        )

    # ── /admin-restore-senate-write ───────────────────────────────────────────

    @app_commands.command(
        name='admin-restore-senate-write',
        description='[Admin] Restore a Senate write grant after a database reset.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        senator='The Senate member who is the guarantor',
        grantee='The embassy member who receives write access'
    )
    async def admin_restore_senate_write(
        self, interaction: discord.Interaction,
        senator: discord.Member,
        grantee: discord.Member
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id))

        # Verify senator still holds the Senate role
        senate_role = None
        if config and config.get('senate_role_id'):
            senate_role = guild.get_role(int(config['senate_role_id']))
        if not senate_role or senate_role not in senator.roles:
            await interaction.followup.send(
                f'{senator.mention} does not currently hold the Senate role.', ephemeral=True
            )
            return

        # Grantee must be a tracked embassy member
        grantee_tracked = await self.bot.db.get_tracked_user(str(grantee.id), str(guild.id))
        if not grantee_tracked or grantee_tracked.get('assigned_role') != 'embassy':
            await interaction.followup.send(
                f'{grantee.mention} is not registered as an embassy member. '
                'Run `/admin-restore` for them first.', ephemeral=True
            )
            return

        embassy_req = await self.bot.db.get_embassy_request(str(grantee.id), str(guild.id))
        if not embassy_req or not embassy_req.get('embassy_write_role_id'):
            await interaction.followup.send(
                f'Could not find the embassy write role for {grantee.mention}\'s country.',
                ephemeral=True
            )
            return

        write_role = guild.get_role(int(embassy_req['embassy_write_role_id']))
        if not write_role:
            await interaction.followup.send(
                'The embassy write role no longer exists in Discord.', ephemeral=True
            )
            return

        if write_role not in grantee.roles:
            try:
                await grantee.add_roles(write_role, reason=f'Senate write grant restored by {interaction.user}')
            except discord.Forbidden:
                await interaction.followup.send(
                    'I do not have permission to assign that role.', ephemeral=True
                )
                return

        await self.bot.db.add_write_grant(
            grantor_discord_id=str(senator.id),
            grantor_warera_id='',
            grantee_discord_id=str(grantee.id),
            guild_id=str(guild.id),
            country_id=embassy_req['country_id'],
            write_role_id=str(write_role.id),
            grant_type='senate'
        )

        await interaction.followup.send(
            f'✅ Senate write grant restored: {senator.mention} → {grantee.mention} '
            f'({embassy_req.get("country_name", "unknown country")}).',
            ephemeral=True
        )

    # ── /admin-run-audit ──────────────────────────────────────────────────────

    @app_commands.command(
        name='admin-run-audit',
        description='Manually trigger the daily role audit (embassy sync, write grant validation).'
    )
    @app_commands.default_permissions(administrator=True)
    async def admin_run_audit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from cogs.scheduler import SchedulerCog
        scheduler: SchedulerCog = self.bot.get_cog('SchedulerCog')
        if not scheduler:
            await interaction.followup.send('Scheduler cog not loaded.', ephemeral=True)
            return
        guild = interaction.guild
        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        await interaction.followup.send(
            f'Running audit on **{len(tracked)}** tracked users… this may take a moment.',
            ephemeral=True
        )
        await scheduler._run_audit(guild)
        await interaction.followup.send('✅ Audit complete.', ephemeral=True)

    # ── /admin-reverify-embassies ─────────────────────────────────────────────

    @app_commands.command(
        name='admin-reverify-embassies',
        description='Rename embassy channels to current schema and start re-verification for all non-senate members.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(category='The category containing embassy channels (defaults to configured embassy category).')
    async def admin_reverify_embassies(
        self, interaction: discord.Interaction,
        category: discord.CategoryChannel = None
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        if not config:
            await interaction.followup.send('Bot not configured — run `/setup` first.', ephemeral=True)
            return

        if category is None:
            cat_id = config.get('embassy_category_id')
            if not cat_id:
                await interaction.followup.send('No embassy category configured and none provided.', ephemeral=True)
                return
            category = guild.get_channel(int(cat_id))
            if not category:
                await interaction.followup.send('Configured embassy category not found in this server.', ephemeral=True)
                return

        senate_role_id = config.get('senate_role_id')
        senate_role = guild.get_role(int(senate_role_id)) if senate_role_id else None
        onboarding_cat_id = config.get('onboarding_category_id')
        onboarding_cat = guild.get_channel(int(onboarding_cat_id)) if onboarding_cat_id else None
        visitor_role_id = config.get('visitor_role_id')

        renamed = 0
        started = 0
        skipped = 0

        for channel in category.text_channels:
            # 1. Determine country info from DB or channel name
            embassy_req_by_channel = None
            async with __import__('aiosqlite').connect(self.bot.db.db_path) as db:
                db.row_factory = __import__('aiosqlite').Row
                async with db.execute(
                    'SELECT * FROM embassy_requests WHERE embassy_channel_id = ? LIMIT 1',
                    (str(channel.id),)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        embassy_req_by_channel = dict(row)

            if embassy_req_by_channel:
                country_name = embassy_req_by_channel['country_name'] or ''
                country_flag = embassy_req_by_channel['country_flag'] or ''
                country_id = embassy_req_by_channel['country_id']
                base_role_id = embassy_req_by_channel.get('embassy_role_id')
                write_role_id = embassy_req_by_channel.get('embassy_write_role_id')
            else:
                # Parse country name from channel name (e.g. "france-embassy" or "embassy-france")
                raw = channel.name.lower()
                raw = _re.sub(r'^embassy[-_]?', '', raw)
                raw = _re.sub(r'[-_]?embassy$', '', raw)
                country_name = raw.replace('-', ' ').replace('_', ' ').strip().title()
                country_flag = get_flag(country_name)
                country_id = None
                base_role_id = None
                write_role_id = None
                # Try to find matching role by name pattern
                base_role = discord.utils.find(
                    lambda r: r.name.lower().startswith(f'embassy {country_name.lower()}')
                              and 'official' not in r.name.lower(),
                    guild.roles
                )
                write_role = discord.utils.find(
                    lambda r: r.name.lower().startswith(f'embassy {country_name.lower()}')
                              and 'official' in r.name.lower(),
                    guild.roles
                )
                if base_role:
                    base_role_id = str(base_role.id)
                if write_role:
                    write_role_id = str(write_role.id)

            if not country_name:
                skipped += 1
                continue

            # 2. Rename channel to current schema
            new_name = f'embassy-{country_channel_name(country_name)}'
            if channel.name != new_name:
                try:
                    await channel.edit(name=new_name)
                    renamed += 1
                except discord.Forbidden:
                    pass

            if not base_role_id:
                skipped += 1
                continue

            base_role = guild.get_role(int(base_role_id))
            if not base_role:
                skipped += 1
                continue

            roles_to_remove = [base_role_id]
            if write_role_id:
                roles_to_remove.append(write_role_id)

            # 3. Start re-verification for each holder of the base role
            for member in guild.members:
                if base_role not in member.roles:
                    continue
                if senate_role and senate_role in member.roles:
                    continue  # senators are exempt
                if member.bot:
                    continue

                existing = await self.bot.db.get_user_request(str(member.id), str(guild.id))
                if existing and existing.get('status') not in ('completed', 'rejected'):
                    continue  # already in a flow

                # Pre-populate embassy_requests so complete_embassy can find it
                if country_name and country_id:
                    await self.bot.db.upsert_embassy_request_for_reverify(
                        str(member.id), str(guild.id),
                        country_id, country_name, country_flag
                    )

                # Create private reverification channel
                safe_name = _re.sub(r'[^a-z0-9\-]', '', member.name.lower().replace(' ', '-'))[:20]
                rev_channel = await guild.create_text_channel(
                    f'reverify-{safe_name}',
                    category=onboarding_cat,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(read_messages=False),
                        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                        guild.me: discord.PermissionOverwrite(
                            read_messages=True, send_messages=True,
                            manage_channels=True, manage_messages=True
                        ),
                    },
                    topic=f'Embassy re-verification for {member.name}'
                )

                await self.bot.db.create_user_request(
                    str(member.id), str(guild.id), str(rev_channel.id),
                    requested_role='reverify_embassy'
                )
                await self.bot.db.create_reverification(
                    str(member.id), str(guild.id), roles_to_remove, 'embassy'
                )

                embed = discord.Embed(
                    title='🏛️ Embassy Re-Verification Required',
                    description=(
                        f'Hello {member.mention}!\n\n'
                        f'As part of a server migration, all embassy members must re-verify '
                        f'their government role in WarEra.\n\n'
                        f'**You have 21 days** to complete this.\n\n'
                        'Please provide your **WarEra.io user ID or profile link** to begin:'
                    ),
                    color=discord.Color.orange()
                )
                await rev_channel.send(embed=embed)
                started += 1

        await interaction.followup.send(
            f'✅ Done.\n'
            f'• Channels renamed: **{renamed}**\n'
            f'• Re-verifications started: **{started}**\n'
            f'• Channels skipped (no role info): **{skipped}**',
            ephemeral=True
        )

    # ── /admin-reverify-government ────────────────────────────────────────────

    @app_commands.command(
        name='admin-reverify-government',
        description='Require all members with access to a channel to re-verify a government/congress role.'
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel='The government channel whose members must re-verify.')
    async def admin_reverify_government(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        if not config:
            await interaction.followup.send('Bot not configured — run `/setup` first.', ephemeral=True)
            return

        senate_role_id = config.get('senate_role_id')
        senate_role = guild.get_role(int(senate_role_id)) if senate_role_id else None
        elders_role_id = config.get('elders_role_id')
        elders_role = guild.get_role(int(elders_role_id)) if elders_role_id else None
        onboarding_cat_id = config.get('onboarding_category_id')
        onboarding_cat = guild.get_channel(int(onboarding_cat_id)) if onboarding_cat_id else None

        # Collect local government role IDs for roles_to_remove on failure
        local_role_ids = []
        for _, db_key, _ in LOCAL_ROLES:
            rid = config.get(db_key)
            if rid:
                local_role_ids.append(rid)

        started = 0
        skipped = 0

        for member in guild.members:
            if member.bot:
                continue
            if member.guild_permissions.administrator:
                continue
            if senate_role and senate_role in member.roles:
                continue  # senators exempt
            if elders_role and elders_role in member.roles:
                continue  # elders exempt

            perms = channel.permissions_for(member)
            if not perms.read_messages:
                continue

            existing = await self.bot.db.get_user_request(str(member.id), str(guild.id))
            if existing and existing.get('status') not in ('completed', 'rejected'):
                skipped += 1
                continue

            # Collect which local government roles this member currently holds
            roles_to_remove = [
                rid for rid in local_role_ids
                if guild.get_role(int(rid)) in member.roles
            ]

            safe_name = _re.sub(r'[^a-z0-9\-]', '', member.name.lower().replace(' ', '-'))[:20]
            rev_channel = await guild.create_text_channel(
                f'reverify-{safe_name}',
                category=onboarding_cat,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    guild.me: discord.PermissionOverwrite(
                        read_messages=True, send_messages=True,
                        manage_channels=True, manage_messages=True
                    ),
                },
                topic=f'Government re-verification for {member.name}'
            )

            await self.bot.db.create_user_request(
                str(member.id), str(guild.id), str(rev_channel.id),
                requested_role='reverify_government'
            )
            await self.bot.db.create_reverification(
                str(member.id), str(guild.id), roles_to_remove, 'government'
            )

            embed = discord.Embed(
                title='🏛️ Government Access Re-Verification',
                description=(
                    f'Hello {member.mention}!\n\n'
                    f'To keep your access to {channel.mention}, you must re-verify that '
                    f'you hold a **congress or government position** in WarEra.\n\n'
                    f'**You have 21 days** to complete this.\n\n'
                    'Please provide your **WarEra.io user ID or profile link** to begin:'
                ),
                color=discord.Color.orange()
            )
            await rev_channel.send(embed=embed)
            started += 1

        await interaction.followup.send(
            f'✅ Done.\n'
            f'• Re-verifications started: **{started}**\n'
            f'• Already in a flow (skipped): **{skipped}**',
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
