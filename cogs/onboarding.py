"""
Onboarding flow state machine.

States stored in user_requests.status:
  pending                  → channel created, waiting for role selection
  awaiting_warera_id       → role chosen, waiting for user to paste WarEra ID
  awaiting_identity_confirm→ WarEra ID found, waiting for Yes/No confirmation
  awaiting_company_change  → identity confirmed (embassy/citizen), waiting for company rename
  awaiting_approval        → embassy, no-role path: waiting for official approval
  completed                → flow done
  rejected                 → explicitly rejected / kicked
"""

import json
import logging
import random
import re
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from country_flags import country_channel_name, get_flag, get_flag_color
from warera_api import (
    extract_user_id, get_user_lite, get_country_by_id,
    get_company_names, get_government_role, role_display_name, get_all_roles_display,
    CONGO_LOCAL_ROLES
)

log = logging.getLogger(__name__)

CONGO_COUNTRY_ID = '6873d0ea1758b40e712b5f4c'
CHANNEL_DELETE_HOURS = 1

_TOKEN_WORDS = [
    'AURORA', 'NEXUS', 'VECTOR', 'DELTA', 'SIGMA', 'OMEGA', 'TITAN', 'ZEPHYR',
    'PRISM', 'VORTEX', 'CIPHER', 'PHANTOM', 'HERALD', 'COBALT', 'AXIOM',
    'ZENITH', 'RADIANT', 'ECLIPSE', 'COSMO', 'BLAZE', 'NOVA', 'QUARTZ',
    'EMBER', 'FROST', 'STORM', 'SOLAR', 'LUNAR', 'DUSK', 'DAWN', 'RIDGE',
    'FALCON', 'HAWK', 'RAVEN', 'EAGLE', 'SWIFT', 'PIKE', 'CRANE', 'HERON',
]


def _generate_token(existing_names: list) -> str:
    existing_upper = {n.upper() for n in existing_names}
    available = [w for w in _TOKEN_WORDS if w not in existing_upper]
    if available:
        return random.choice(available)
    while True:
        token = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=8))
        if token not in existing_upper:
            return token


# ── Persistent Views ──────────────────────────────────────────────────────────
# All views use fixed custom_ids and look up state from the DB via interaction.user.

class RoleSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, interaction: discord.Interaction, role: str):
        cog: OnboardingCog = interaction.client.get_cog('OnboardingCog')
        request = await interaction.client.db.get_user_request(
            str(interaction.user.id), str(interaction.guild.id)
        )
        if not request or str(interaction.channel.id) != request.get('channel_id'):
            await interaction.response.send_message(
                'This button is not for you.', ephemeral=True
            )
            return
        if request.get('status') != 'pending':
            await interaction.response.send_message(
                'You already selected a role.', ephemeral=True
            )
            return
        await interaction.response.defer()
        await interaction.client.db.update_user_request(
            str(interaction.user.id), str(interaction.guild.id),
            requested_role=role, status='awaiting_warera_id'
        )
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await cog.ask_warera_id(interaction.channel, interaction.user)

    @discord.ui.button(label='Visitor', style=discord.ButtonStyle.secondary,
                       emoji='🏠', custom_id='role_select_visitor')
    async def visitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, 'visitor')

    @discord.ui.button(label='Embassy', style=discord.ButtonStyle.primary,
                       emoji='🏛️', custom_id='role_select_embassy')
    async def embassy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, 'embassy')

    @discord.ui.button(label='Citizen', style=discord.ButtonStyle.success,
                       emoji='🌍', custom_id='role_select_citizen')
    async def citizen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, 'citizen')


class IdentityConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes, that's me!", style=discord.ButtonStyle.success,
                       emoji='✅', custom_id='identity_confirm_yes')
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: OnboardingCog = interaction.client.get_cog('OnboardingCog')
        request = await interaction.client.db.get_user_request(
            str(interaction.user.id), str(interaction.guild.id)
        )
        if not request or str(interaction.channel.id) != request.get('channel_id'):
            await interaction.response.send_message('This button is not for you.', ephemeral=True)
            return
        if request.get('status') != 'awaiting_identity_confirm':
            await interaction.response.send_message('Already confirmed.', ephemeral=True)
            return
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await cog.route_after_identity(interaction.channel, interaction.user, request)

    @discord.ui.button(label='No, wrong account', style=discord.ButtonStyle.danger,
                       emoji='❌', custom_id='identity_confirm_no')
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = await interaction.client.db.get_user_request(
            str(interaction.user.id), str(interaction.guild.id)
        )
        if not request or str(interaction.channel.id) != request.get('channel_id'):
            await interaction.response.send_message('This button is not for you.', ephemeral=True)
            return
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.client.db.update_user_request(
            str(interaction.user.id), str(interaction.guild.id),
            status='awaiting_warera_id', warera_id=None, warera_username=None
        )
        await interaction.channel.send(
            '❌ No problem. Please provide your correct WarEra user ID or profile link.'
        )


class RequestApprovalView(discord.ui.View):
    """Sent to officials so they can approve/deny a no-role embassy requester."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Approve', style=discord.ButtonStyle.success,
                       emoji='✅', custom_id='embassy_approve')
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, approved=True)

    @discord.ui.button(label='Deny', style=discord.ButtonStyle.danger,
                       emoji='❌', custom_id='embassy_deny')
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, approved=False)

    async def _handle(self, interaction: discord.Interaction, approved: bool):
        cog: OnboardingCog = interaction.client.get_cog('OnboardingCog')

        approval = await interaction.client.db.get_pending_approval(str(interaction.message.id))
        if not approval:
            await interaction.response.send_message('This request is no longer active.', ephemeral=True)
            return

        # Only a tracked embassy official from the same country can approve/deny
        requester_embassy = await interaction.client.db.get_embassy_request(
            approval['requester_discord_id'], str(interaction.guild.id)
        )
        approver_tracked = await interaction.client.db.get_tracked_user(
            str(interaction.user.id), str(interaction.guild.id)
        )
        requester_country = requester_embassy.get('country_id') if requester_embassy else None
        if (
            not approver_tracked
            or approver_tracked.get('assigned_role') != 'embassy'
            or approver_tracked.get('country_id') != requester_country
        ):
            await interaction.response.send_message(
                'Only a government official from the same country can approve or deny this request.',
                ephemeral=True
            )
            return

        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        requester_id = approval['requester_discord_id']
        requester = interaction.guild.get_member(int(requester_id))
        await interaction.client.db.remove_pending_approval(str(interaction.message.id))

        if approved:
            await interaction.channel.send(
                f'✅ Request approved by {interaction.user.mention}.'
            )
            if requester:
                request = await interaction.client.db.get_user_request(
                    requester_id, str(interaction.guild.id)
                )
                if request:
                    req_channel = interaction.guild.get_channel(int(request['channel_id']))
                    if req_channel:
                        await req_channel.send(
                            f'{requester.mention} ✅ Your embassy request was approved by '
                            f'an official! You have been granted read access to the embassy channel.'
                        )
                    # Grant read-only access: add the base embassy role to the requester
                    embassy_req = await interaction.client.db.get_embassy_request(
                        requester_id, str(interaction.guild.id)
                    )
                    if embassy_req and embassy_req.get('embassy_role_id') and requester:
                        base_role = interaction.guild.get_role(int(embassy_req['embassy_role_id']))
                        if base_role:
                            try:
                                await requester.add_roles(base_role)
                            except discord.Forbidden:
                                pass
                    await interaction.client.db.update_user_request(
                        requester_id, str(interaction.guild.id),
                        status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    if embassy_req:
                        await interaction.client.db.upsert_tracked_user(
                            requester_id, str(interaction.guild.id),
                            request['warera_id'], 'embassy',
                            embassy_req.get('country_id'),
                            embassy_req.get('embassy_role_id')
                        )
                    if req_channel and cog:
                        await cog._schedule_deletion(req_channel)
        else:
            await interaction.channel.send(
                f'❌ Request denied by {interaction.user.mention}.'
            )
            if requester:
                request = await interaction.client.db.get_user_request(
                    requester_id, str(interaction.guild.id)
                )
                if request:
                    req_channel = interaction.guild.get_channel(int(request['channel_id']))
                    if req_channel:
                        await req_channel.send(
                            f'{requester.mention} ❌ Your embassy request was denied by an official.\n'
                            'You remain as Visitor.'
                        )
                        if cog:
                            await cog._schedule_deletion(req_channel)
                await interaction.client.db.update_user_request(
                    requester_id, str(interaction.guild.id),
                    status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                )


# ── Main Cog ──────────────────────────────────────────────────────────────────

class OnboardingCog(commands.Cog, name='OnboardingCog'):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(RoleSelectionView())
        bot.add_view(IdentityConfirmView())
        bot.add_view(RequestApprovalView())

    # ── Entry point ───────────────────────────────────────────────────────────

    async def start_onboarding(self, member: discord.Member):
        guild = member.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        if not config or not config.get('onboarding_category_id'):
            log.warning('No onboarding category configured — run /setup first.')
            return

        category = guild.get_channel(int(config['onboarding_category_id']))
        if not category:
            log.warning('Onboarding category channel not found.')
            return

        # If there's already an active channel, re-use it
        existing = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        if existing and existing.get('status') not in ('completed', 'rejected'):
            ch = guild.get_channel(int(existing['channel_id'])) if existing.get('channel_id') else None
            if ch:
                await ch.send(f'{member.mention} Welcome back! Your application is still open here.')
                return

        safe_name = re.sub(r'[^a-z0-9\-]', '', member.name.lower().replace(' ', '-'))[:20]
        channel_name = f'welcome-{safe_name}'

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                manage_channels=True, manage_messages=True
            ),
        }
        channel = await guild.create_text_channel(
            channel_name, category=category, overwrites=overwrites,
            topic=f'Onboarding for {member.name}'
        )

        await self.bot.db.create_user_request(str(member.id), str(guild.id), str(channel.id))

        embed = discord.Embed(
            title='Welcome to Congo! 🇨🇬',
            description=(
                f'Hello {member.mention}!\n\n'
                'Please select the role that best describes you to get started.'
            ),
            color=discord.Color.green()
        )
        embed.add_field(name='🏠 Visitor', value='Just visiting — no requirements.', inline=False)
        embed.add_field(
            name='🏛️ Embassy',
            value='Represent your country\'s government. Must be a government official in WarEra.',
            inline=False
        )
        embed.add_field(
            name='🌍 Citizen',
            value='Congo citizen in WarEra. Must have Congo citizenship.',
            inline=False
        )
        await channel.send(embed=embed, view=RoleSelectionView())

    # ── Step helpers ──────────────────────────────────────────────────────────

    async def ask_warera_id(self, channel: discord.TextChannel, member: discord.Member):
        embed = discord.Embed(
            title='🔍 WarEra.io Verification',
            description=(
                'Please provide your **WarEra.io user ID** or profile link.\n\n'
                '**Accepted formats:**\n'
                '• `6914ec027c985472c690b896`\n'
                '• `https://app.warera.io/user/6914ec027c985472c690b896`'
            ),
            color=discord.Color.blue()
        )
        await channel.send(embed=embed)

    async def show_identity_embed(self, channel: discord.TextChannel,
                                   member: discord.Member, warera_id: str):
        user_data = await get_user_lite(warera_id)
        if not user_data:
            await channel.send(
                '❌ No WarEra account found for that ID. Please check and try again.'
            )
            return

        country_name = 'Unknown'
        country_flag = '🏳️'
        if user_data.get('country'):
            country_data = await get_country_by_id(user_data['country'])
            if country_data:
                country_name = country_data.get('name', 'Unknown')
                country_flag = get_flag(country_name)

        infos = user_data.get('infos', {})
        roles_str = get_all_roles_display(infos)

        embed = discord.Embed(title='Is this your WarEra account?', color=discord.Color.gold())
        embed.set_thumbnail(url=user_data.get('avatarUrl', ''))
        embed.add_field(name='Username', value=user_data.get('username', '?'), inline=True)
        embed.add_field(name='Country', value=f'{country_name} {country_flag}', inline=True)
        embed.add_field(name='Level', value=str(user_data.get('leveling', {}).get('level', '?')), inline=True)
        embed.add_field(name='Government Role', value=roles_str, inline=False)

        await self.bot.db.update_user_request(
            str(member.id), str(channel.guild.id),
            warera_id=warera_id,
            warera_username=user_data.get('username'),
            country_id=user_data.get('country'),
            country_name=country_name,
            status='awaiting_identity_confirm'
        )
        await channel.send(embed=embed, view=IdentityConfirmView())

    async def route_after_identity(self, channel: discord.TextChannel,
                                    member: discord.Member, request: dict):
        role = request.get('requested_role', 'visitor')
        warera_data = await get_user_lite(request['warera_id'])
        if not warera_data:
            await channel.send('❌ Could not re-fetch your WarEra data. Please try `/reset-request`.')
            return

        if role == 'visitor':
            await self.complete_visitor(channel, member, warera_data)
        elif role == 'citizen':
            await self.start_citizen(channel, member, warera_data)
        elif role == 'embassy':
            await self.start_embassy(channel, member, warera_data)
        elif role == 'reverify_embassy':
            await self.start_embassy(channel, member, warera_data)
        elif role == 'reverify_government':
            await self.start_reverify_government(channel, member, warera_data)

    # ── Visitor path ──────────────────────────────────────────────────────────

    async def complete_visitor(self, channel: discord.TextChannel,
                                member: discord.Member, warera_data: dict):
        guild = channel.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        username = warera_data.get('username', member.name)

        await self._set_nickname(member, username)
        visitor_role = await self._assign_role(guild, member, config, 'visitor_role_id')

        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        )
        await self.bot.db.upsert_tracked_user(
            str(member.id), str(guild.id),
            warera_data['_id'], 'visitor',
            warera_data.get('country'),
            str(visitor_role.id) if visitor_role else None
        )
        await self._schedule_deletion(channel)

        embed = discord.Embed(
            title='✅ Welcome, Visitor!',
            description=(
                f'You are now registered as **{username}** with the **Visitor** role.\n\n'
                f'This channel will be deleted in {CHANNEL_DELETE_HOURS} hours.'
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    # ── Citizen path ──────────────────────────────────────────────────────────

    async def start_citizen(self, channel: discord.TextChannel,
                             member: discord.Member, warera_data: dict):
        guild = channel.guild
        if warera_data.get('country') != CONGO_COUNTRY_ID:
            embed = discord.Embed(
                title='❌ Not a Congo Citizen',
                description=(
                    'Your WarEra citizenship is not with **Congo**.\n\n'
                    'You will be registered as a **Visitor** instead.'
                ),
                color=discord.Color.red()
            )
            await channel.send(embed=embed)
            await self.complete_visitor(channel, member, warera_data)
            return

        company_names = await get_company_names(warera_data['_id'])
        token = _generate_token(company_names)

        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            verification_token=token, status='awaiting_company_change'
        )
        embed = discord.Embed(
            title='🏢 Company Name Verification',
            description=(
                'To verify your **Congo citizenship**, rename one of your companies in WarEra to:\n\n'
                f'# `{token}`\n\n'
                'The bot checks every minute automatically.\n'
                'You can also send any message here to trigger an immediate check.\n\n'
                '⚠️ The name must match exactly (case-insensitive).'
            ),
            color=discord.Color.blue()
        )
        await channel.send(embed=embed)

    async def complete_citizen(self, channel: discord.TextChannel, member: discord.Member):
        guild = channel.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        request = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        if not request:
            return

        username = request.get('warera_username', member.name)
        await self._set_nickname(member, username)

        # Remove visitor role if present
        visitor_role_id = config.get('visitor_role_id') if config else None
        if visitor_role_id:
            vr = guild.get_role(int(visitor_role_id))
            if vr and vr in member.roles:
                try:
                    await member.remove_roles(vr)
                except discord.Forbidden:
                    pass

        citizen_role = await self._assign_role(guild, member, config, 'citizen_role_id')

        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        )
        await self.bot.db.upsert_tracked_user(
            str(member.id), str(guild.id),
            request['warera_id'], 'citizen',
            request.get('country_id'),
            str(citizen_role.id) if citizen_role else None
        )

        # Assign any Congolese government roles this citizen currently holds in WarEra
        warera_data = await get_user_lite(request['warera_id'])
        if warera_data:
            await self.sync_congo_local_roles(guild, member, warera_data, config)

        await self._schedule_deletion(channel)

        embed = discord.Embed(
            title='✅ Citizenship Verified!',
            description=(
                f'Welcome home, **{username}**! 🇨🇬\n\n'
                'You have been granted **Citizen** status.\n\n'
                f'This channel will be deleted in {CHANNEL_DELETE_HOURS} hours.'
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    # ── Embassy path ──────────────────────────────────────────────────────────

    async def start_embassy(self, channel: discord.TextChannel,
                             member: discord.Member, warera_data: dict):
        guild = channel.guild
        infos = warera_data.get('infos', {})
        role_field, access_level, country_id = get_government_role(infos)

        if not role_field:
            await self._handle_embassy_no_role(channel, member, warera_data)
            return

        # Get country info
        country_data = await get_country_by_id(country_id)
        country_name = country_data.get('name', 'Unknown') if country_data else 'Unknown'
        country_flag = get_flag(country_name)

        # Generate company token
        company_names = await get_company_names(warera_data['_id'])
        token = _generate_token(company_names)

        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            verification_token=token, status='awaiting_company_change'
        )
        await self.bot.db.create_embassy_request(
            str(member.id), str(guild.id),
            country_id, country_name, country_flag, role_field, access_level
        )

        access_str = 'Read & Write' if access_level == 'write' else 'Read Only'
        embed = discord.Embed(
            title='🏛️ Embassy Verification',
            description=(
                f'Detected role: **{role_display_name(role_field)}** of '
                f'**{country_name} {country_flag}**\n'
                f'Access level: **{access_str}**\n\n'
                'To verify your identity, rename one of your companies in WarEra to:\n\n'
                f'# `{token}`\n\n'
                'The bot checks every minute automatically.\n'
                'You can also send any message here to trigger an immediate check.\n\n'
                '⚠️ The name must match exactly (case-insensitive).'
            ),
            color=discord.Color.blue()
        )
        await channel.send(embed=embed)

    async def complete_embassy(self, channel: discord.TextChannel, member: discord.Member):
        guild = channel.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        request = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        embassy_req = await self.bot.db.get_embassy_request(str(member.id), str(guild.id))

        if not request or not embassy_req:
            return

        country_name = embassy_req['country_name']
        country_flag = embassy_req['country_flag']
        country_id = embassy_req['country_id']
        access_level = embassy_req['access_level']
        username = request.get('warera_username', member.name)

        await self._set_nickname(member, username)

        # Ensure embassy category exists
        embassy_cat = await self._ensure_embassy_category(guild, config)

        # Ensure embassy channel and BOTH roles exist
        emb_channel, base_role, write_role = await self._ensure_embassy_channel_role(
            guild, embassy_cat, country_name, country_flag, config
        )

        # Everyone gets the base role (read-only view)
        try:
            await member.add_roles(base_role)
        except discord.Forbidden:
            pass

        if access_level == 'write':
            # Officials also get the write role — Discord's most-permissive rule gives them send
            try:
                await member.add_roles(write_role)
            except discord.Forbidden:
                pass
            access_str = 'Read & Write'
        else:
            access_str = 'Read Only'
            # Inform member and notify officials in the embassy channel
            await channel.send(
                f'ℹ️ Your government role grants **Read Only** access to {emb_channel.mention}.\n'
                'Your country\'s officials have been notified.'
            )
            await emb_channel.send(
                f'📢 **{username}** ({member.mention}) has joined as '
                f'**{role_display_name(embassy_req["warera_role"])}** — read-only access.\n\n'
                f'{write_role.mention} — you can grant them write access with '
                f'`/addwrite {username}`.'
            )

        await self.bot.db.update_embassy_request(
            str(member.id), str(guild.id),
            embassy_channel_id=str(emb_channel.id),
            embassy_role_id=str(base_role.id),
            embassy_write_role_id=str(write_role.id),
            approval_status='approved'
        )
        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        )
        await self.bot.db.upsert_tracked_user(
            str(member.id), str(guild.id),
            request['warera_id'], 'embassy',
            country_id, str(base_role.id)
        )

        # Congolese embassy members also receive the Citizen role
        if country_id == CONGO_COUNTRY_ID:
            await self._assign_role(guild, member, config, 'citizen_role_id')

        await self._schedule_deletion(channel)

        embed = discord.Embed(
            title='✅ Embassy Access Granted!',
            description=(
                f'Welcome, **{username}** ({country_flag})!\n\n'
                f'You have **{access_str}** access to {emb_channel.mention}.\n\n'
                f'This channel will be deleted in {CHANNEL_DELETE_HOURS} hours.'
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    async def _handle_embassy_no_role(self, channel: discord.TextChannel,
                                       member: discord.Member, warera_data: dict):
        guild = channel.guild
        config = await self.bot.db.get_guild_config(str(guild.id))

        # For re-verification flows: fail immediately instead of the normal no-role UI
        request = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        if request and request.get('requested_role') in ('reverify_embassy', 'reverify_government'):
            await channel.send(
                f'{member.mention} ❌ No government role detected in WarEra — your access will be revoked.'
            )
            await self._fail_reverification(channel, member)
            return

        # Set as visitor first
        await self.complete_visitor(channel, member, warera_data)

        # Determine their country to find officials
        country_id = warera_data.get('country')
        officials = await self._find_country_officials(guild, country_id)

        embed = discord.Embed(
            title='ℹ️ No Government Role Detected',
            description=(
                'You have no official government role in WarEra.\n'
                'You have been set as a **Visitor**.\n\n'
                'If you need specific embassy access, your country\'s officials '
                'can approve your request using the button below.'
            ),
            color=discord.Color.orange()
        )
        await channel.send(embed=embed)

        if not officials:
            await self.bot.db.remove_deletion(str(channel.id))
            await channel.send(
                '⚠️ No officials from your country are currently registered in this Discord.\n'
                'Use `/retry-application` when an official is available.'
            )
            return

        # Find or create the embassy channel so officials can see the request
        embassy_cat = await self._ensure_embassy_category(guild, config)
        country_data = await get_country_by_id(country_id) if country_id else None
        country_name = country_data.get('name', 'Unknown') if country_data else 'Unknown'
        country_flag = get_flag(country_name)

        emb_channel, base_role, write_role = await self._ensure_embassy_channel_role(
            guild, embassy_cat, country_name, country_flag, config
        )

        await self.bot.db.create_embassy_request(
            str(member.id), str(guild.id),
            country_id or '', country_name, country_flag, 'none', 'none'
        )
        await self.bot.db.update_embassy_request(
            str(member.id), str(guild.id),
            embassy_channel_id=str(emb_channel.id),
            embassy_role_id=str(base_role.id),
            embassy_write_role_id=str(write_role.id),
            approval_status='pending'
        )

        official_mentions = ' '.join(o.mention for o in officials)
        approval_embed = discord.Embed(
            title='📨 Embassy Access Request',
            description=(
                f'**{warera_data.get("username")}** ({member.mention}) from '
                f'**{country_name} {country_flag}** is requesting embassy access.\n\n'
                'They have no detected government role in WarEra.'
            ),
            color=discord.Color.orange()
        )
        msg = await emb_channel.send(
            content=f'Officials: {official_mentions}',
            embed=approval_embed,
            view=RequestApprovalView()
        )
        await self.bot.db.add_pending_approval(
            str(msg.id), str(guild.id), str(member.id)
        )
        await self.bot.db.update_embassy_request(
            str(member.id), str(guild.id), approval_message_id=str(msg.id)
        )
        await channel.send(
            f'📨 Your request has been sent to your country\'s officials: {official_mentions}\n'
            'They will review and approve or deny your request.'
        )

    # ── Company verification check ────────────────────────────────────────────

    # ── Re-verification helpers ───────────────────────────────────────────────

    async def start_reverify_government(self, channel: discord.TextChannel,
                                         member: discord.Member, warera_data: dict):
        """Start the company-rename step for a government re-verification."""
        guild = channel.guild
        infos = warera_data.get('infos', {})
        role_field, _, _ = get_government_role(infos)
        if not role_field:
            await channel.send(
                f'{member.mention} ❌ No congress or government role detected in WarEra. '
                'Your access will be revoked.'
            )
            await self._fail_reverification(channel, member)
            return

        company_names = await get_company_names(warera_data['_id'])
        token = _generate_token(company_names)
        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            verification_token=token, status='awaiting_company_change'
        )
        embed = discord.Embed(
            title='🏛️ Government Role Verification',
            description=(
                f'Detected role: **{role_display_name(role_field)}**\n\n'
                'To verify your identity, rename one of your companies in WarEra to:\n\n'
                f'# `{token}`\n\n'
                'The bot checks every minute automatically.\n'
                'You can also send any message here to trigger an immediate check.\n\n'
                '⚠️ The name must match exactly (case-insensitive).'
            ),
            color=discord.Color.blue()
        )
        await channel.send(embed=embed)

    async def complete_government_reverify(self, channel: discord.TextChannel,
                                            member: discord.Member):
        """Complete a government re-verification — confirms the member, keeps their roles."""
        guild = channel.guild
        request = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        if not request:
            return
        await self.bot.db.update_user_request(
            str(member.id), str(guild.id),
            status='completed', completed_at=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        )
        # Track them if not already tracked
        if request.get('warera_id'):
            await self.bot.db.upsert_tracked_user(
                str(member.id), str(guild.id),
                request['warera_id'], 'citizen',
                request.get('country_id'), None
            )
        await self.bot.db.delete_reverification(str(member.id), str(guild.id))
        await self._schedule_deletion(channel)
        embed = discord.Embed(
            title='✅ Government Role Verified!',
            description=(
                f'Thank you, {member.mention}! Your government role has been confirmed.\n\n'
                f'This channel will be deleted in {CHANNEL_DELETE_HOURS} hour(s).'
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed)

    async def _fail_reverification(self, channel: discord.TextChannel, member: discord.Member):
        """Strip roles, give visitor, clean up — used when re-verification fails or times out."""
        guild = channel.guild
        config = await self.bot.db.get_guild_config(str(guild.id))
        rev = await self.bot.db.get_reverification(str(member.id), str(guild.id))
        if rev:
            for role_id in json.loads(rev['roles_to_remove']):
                role = guild.get_role(int(role_id))
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason='Re-verification failed')
                    except discord.Forbidden:
                        pass
        visitor_role_id = config.get('visitor_role_id') if config else None
        if visitor_role_id:
            vr = guild.get_role(int(visitor_role_id))
            if vr and vr not in member.roles:
                try:
                    await member.add_roles(vr, reason='Re-verification failed — downgraded to Visitor')
                except discord.Forbidden:
                    pass
        try:
            await member.send(
                '⚠️ Your embassy/government access has been removed because you did not '
                'complete re-verification in time. You have been given the **Visitor** role.'
            )
        except discord.Forbidden:
            pass
        await self.bot.db.delete_user_request(str(member.id), str(guild.id))
        await self.bot.db.delete_reverification(str(member.id), str(guild.id))
        if channel:
            try:
                await channel.delete(reason='Re-verification failed or timed out')
            except discord.Forbidden:
                pass

    async def check_company_verification(self, channel: discord.TextChannel,
                                          member: discord.Member) -> bool:
        """Returns True if verified (and completes the flow), False otherwise."""
        request = await self.bot.db.get_user_request(str(member.id), str(channel.guild.id))
        if not request or request.get('status') != 'awaiting_company_change':
            return False

        token = request.get('verification_token')
        warera_id = request.get('warera_id')
        if not token or not warera_id:
            return False

        names = await get_company_names(warera_id)
        if any(n.upper() == token.upper() for n in names):
            role = request.get('requested_role')
            if role == 'citizen':
                await self.complete_citizen(channel, member)
            elif role == 'embassy':
                await self.complete_embassy(channel, member)
            elif role == 'reverify_embassy':
                await self.complete_embassy(channel, member)
                await self.bot.db.delete_reverification(str(member.id), str(channel.guild.id))
            elif role == 'reverify_government':
                await self.complete_government_reverify(channel, member)
            return True
        return False

    # ── Congo local government role helpers ───────────────────────────────────

    async def sync_congo_local_roles(
        self, guild: discord.Guild, member: discord.Member,
        warera_data: dict, config: dict
    ):
        """
        Assign or remove the configured Congolese government Discord roles for a
        citizen based on their current WarEra infos.  Only roles that are
        configured in guild_config are touched; unconfigured roles are skipped.
        All additions and removals are batched into single API calls so that
        members with multiple concurrent roles (e.g. President + Congress Member)
        are handled correctly without hitting per-call failures.
        """
        infos = warera_data.get('infos', {})
        to_add = []
        to_remove = []
        for warera_field, db_key, _ in CONGO_LOCAL_ROLES:
            role_id = config.get(db_key) if config else None
            if not role_id:
                continue
            discord_role = guild.get_role(int(role_id))
            if not discord_role:
                continue
            has_role = (infos.get(warera_field) == CONGO_COUNTRY_ID)
            if has_role and discord_role not in member.roles:
                to_add.append(discord_role)
            elif not has_role and discord_role in member.roles:
                to_remove.append(discord_role)
        add_error = None
        remove_error = None
        if to_add:
            try:
                await member.add_roles(*to_add, reason='WarEra: Congo government roles synced')
            except Exception as e:
                add_error = str(e)
                log.warning('sync_congo_local_roles: add_roles failed for %s: %s', member, e)
        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason='WarEra: Congo government roles synced')
            except Exception as e:
                remove_error = str(e)
                log.warning('sync_congo_local_roles: remove_roles failed for %s: %s', member, e)
        return to_add, to_remove, add_error, remove_error

    async def remove_all_congo_local_roles(
        self, guild: discord.Guild, member: discord.Member, config: dict
    ):
        """Strip all configured Congolese government roles from a member."""
        for _, db_key, _ in CONGO_LOCAL_ROLES:
            role_id = config.get(db_key) if config else None
            if not role_id:
                continue
            discord_role = guild.get_role(int(role_id))
            if discord_role and discord_role in member.roles:
                try:
                    await member.remove_roles(discord_role, reason='No longer a Congo citizen')
                except discord.Forbidden:
                    pass

    # ── Discord helpers ───────────────────────────────────────────────────────

    async def _set_nickname(self, member: discord.Member, name: str):
        try:
            await member.edit(nick=name[:32])
        except discord.Forbidden:
            pass

    async def _assign_role(self, guild: discord.Guild, member: discord.Member,
                            config: dict, role_key: str) -> discord.Role | None:
        if not config or not config.get(role_key):
            return None
        role = guild.get_role(int(config[role_key]))
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass
        return role

    async def _schedule_deletion(self, channel: discord.TextChannel):
        delete_at = (datetime.utcnow() + timedelta(hours=CHANNEL_DELETE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
        await self.bot.db.schedule_deletion(str(channel.id), delete_at)

    async def _ensure_embassy_category(self, guild: discord.Guild, config: dict) -> discord.CategoryChannel:
        if config and config.get('embassy_category_id'):
            cat = guild.get_channel(int(config['embassy_category_id']))
            if cat:
                return cat
        cat = await guild.create_category('🏛️ Embassies')
        await self.bot.db.set_guild_config(str(guild.id), embassy_category_id=str(cat.id))
        return cat

    async def _ensure_embassy_channel_role(
        self, guild: discord.Guild, category: discord.CategoryChannel,
        country_name: str, country_flag: str, config: dict
    ) -> tuple:
        """
        Returns (channel, base_role, write_role).
        base_role  → read-only access (all embassy members)
        write_role → read+write access (officials only)
        Permissions are additive: write users hold BOTH roles.
        """
        ch_name = f'embassy-{country_channel_name(country_name)}'
        base_role_name = f'Embassy {country_name} {country_flag}'
        write_role_name = f'Embassy {country_name} {country_flag} - Officials'

        role_color = discord.Color(get_flag_color(country_name))

        base_role = discord.utils.get(guild.roles, name=base_role_name)
        if not base_role:
            base_role = await guild.create_role(
                name=base_role_name, mentionable=True, color=role_color, hoist=True
            )
        else:
            await base_role.edit(color=role_color, hoist=True)

        write_role = discord.utils.get(guild.roles, name=write_role_name)
        if not write_role:
            write_role = await guild.create_role(
                name=write_role_name, mentionable=True, color=role_color, hoist=True
            )
        else:
            await write_role.edit(color=role_color, hoist=True)

        channel = discord.utils.get(guild.text_channels, name=ch_name)
        if not channel:
            senate_role = None
            if config and config.get('senate_role_id'):
                senate_role = guild.get_role(int(config['senate_role_id']))

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_channels=True
                ),
                # base_role: read only — no send, but can use slash commands
                base_role: discord.PermissionOverwrite(read_messages=True, send_messages=False, use_application_commands=True),
                # write_role: read + write + slash commands for /addwrite
                write_role: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, use_application_commands=True
                ),
            }
            if senate_role:
                overwrites[senate_role] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, use_application_commands=True
                )
            channel = await guild.create_text_channel(
                ch_name, category=category, overwrites=overwrites,
                topic=f'Embassy of {country_name} {country_flag}'
            )
        else:
            # Channel already exists — ensure the roles have correct overwrites.
            # This handles cases where roles were recreated after a reset.
            await channel.set_permissions(base_role, read_messages=True, send_messages=False, use_application_commands=True)
            await channel.set_permissions(
                write_role, read_messages=True, send_messages=True, use_application_commands=True
            )
        return channel, base_role, write_role

    async def _find_country_officials(
        self, guild: discord.Guild, country_id: str
    ) -> list:
        """Return Discord members who are embassy-write users for the given country."""
        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        officials = []
        for t in tracked:
            if t.get('country_id') == country_id and t.get('assigned_role') == 'embassy':
                member = guild.get_member(int(t['discord_id']))
                if member:
                    emb = await self.bot.db.get_embassy_request(t['discord_id'], str(guild.id))
                    if emb and emb.get('access_level') == 'write':
                        officials.append(member)
        return officials

    # ── Startup permission sync ───────────────────────────────────────────────

    async def sync_embassy_permissions(self, guild: discord.Guild):
        """
        On startup, iterate every known embassy and ensure channel permission
        overwrites are correct for base_role and write_role.  This fixes any
        drift caused by role/channel recreation after a database reset or manual
        Discord edits without waiting for a new member to trigger the flow.
        """
        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        seen_channel_ids: set = set()

        for t in tracked:
            if t.get('assigned_role') != 'embassy':
                continue
            emb = await self.bot.db.get_embassy_request(t['discord_id'], str(guild.id))
            if not emb:
                continue

            channel_id = emb.get('embassy_channel_id')
            if not channel_id or channel_id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel_id)

            channel = guild.get_channel(int(channel_id))
            if not channel:
                continue

            role_id = emb.get('embassy_role_id')
            write_role_id = emb.get('embassy_write_role_id')
            base_role = guild.get_role(int(role_id)) if role_id else None
            write_role = guild.get_role(int(write_role_id)) if write_role_id else None

            country_name = emb.get('country_name')
            role_color = discord.Color(get_flag_color(country_name)) if country_name else None

            try:
                if base_role:
                    await channel.set_permissions(base_role, read_messages=True, send_messages=False, use_application_commands=True)
                    if role_color:
                        await base_role.edit(color=role_color, hoist=True)
                if write_role:
                    await channel.set_permissions(
                        write_role, read_messages=True, send_messages=True,
                        use_application_commands=True
                    )
                    if role_color:
                        await write_role.edit(color=role_color, hoist=True)
            except discord.Forbidden:
                log.warning('Missing permissions to sync embassy channel %s', channel.name)

        log.info('Embassy permission sync complete (%d channels checked).', len(seen_channel_ids))

    @commands.Cog.listener()
    async def on_ready(self):
        guild = self.bot.get_guild(self.bot.guild_id)
        if guild:
            await self.sync_embassy_permissions(guild)

    # ── on_message listener ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        member = message.author
        guild = message.guild
        request = await self.bot.db.get_user_request(str(member.id), str(guild.id))
        if not request:
            return
        if str(message.channel.id) != request.get('channel_id'):
            return

        # Update activity timestamp
        await self.bot.db.update_user_request(str(member.id), str(guild.id))

        status = request.get('status')

        if status == 'awaiting_warera_id':
            warera_id = extract_user_id(message.content)
            if warera_id:
                await message.add_reaction('⏳')
                await self.show_identity_embed(message.channel, member, warera_id)
            else:
                await message.channel.send(
                    '❌ Invalid format. Please provide a valid WarEra user ID or profile URL.'
                )

        elif status == 'awaiting_company_change':
            checking_msg = await message.channel.send('🔄 Checking company names…')
            verified = await self.check_company_verification(message.channel, member)
            if not verified:
                await checking_msg.edit(content='⏳ Company name not changed yet. Make sure it matches exactly and try again.')


async def setup(bot):
    await bot.add_cog(OnboardingCog(bot))
