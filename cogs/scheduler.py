"""
Background tasks:
  • Every 1 min  — check pending company-name verifications
  • Every 5 min  — execute scheduled channel deletions
  • Every 1 hour — check channel inactivity (7-day warn, 14-day kick)
  • Every 1 hour — daily role audit at 7:00 UTC
"""

import logging
from datetime import datetime

import discord
from discord.ext import commands, tasks

from country_flags import get_flag
from warera_api import get_user_lite, get_government_role, role_display_name, get_country_by_id, CONGO_LOCAL_ROLES, batch_get_user_lite

log = logging.getLogger(__name__)

CONGO_COUNTRY_ID = '6873d0ea1758b40e712b5f4c'


class SchedulerCog(commands.Cog, name='SchedulerCog'):
    def __init__(self, bot):
        self.bot = bot
        self._daily_check_ran_hour: int = -1
        self.check_company_names.start()
        self.check_scheduled_deletions.start()
        self.check_inactivity.start()
        self.check_reverification_inactivity.start()
        self.daily_role_audit.start()
        self.daily_backup.start()

    def cog_unload(self):
        self.check_company_names.cancel()
        self.check_scheduled_deletions.cancel()
        self.check_inactivity.cancel()
        self.check_reverification_inactivity.cancel()
        self.daily_role_audit.cancel()
        self.daily_backup.cancel()

    def _get_onboarding(self):
        return self.bot.get_cog('OnboardingCog')

    # ── Task: company name verification ──────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_company_names(self):
        guild = self.bot.get_guild(self.bot.guild_id)
        if not guild:
            return

        pending = await self.bot.db.get_pending_requests_by_status(
            str(guild.id), 'awaiting_company_change'
        )
        onboarding = self._get_onboarding()
        if not onboarding:
            return

        for req in pending:
            member = guild.get_member(int(req['discord_id']))
            if not member or not req.get('channel_id'):
                continue
            channel = guild.get_channel(int(req['channel_id']))
            if not channel:
                continue
            try:
                await onboarding.check_company_verification(channel, member)
            except Exception:
                log.exception(f'Error checking company verification for {req["discord_id"]}')

    @check_company_names.before_loop
    async def before_company_check(self):
        await self.bot.wait_until_ready()

    # ── Task: scheduled channel deletions ────────────────────────────────────

    @tasks.loop(minutes=5)
    async def check_scheduled_deletions(self):
        guild = self.bot.get_guild(self.bot.guild_id)
        if not guild:
            return

        due = await self.bot.db.get_due_deletions()
        for row in due:
            channel = guild.get_channel(int(row['channel_id']))
            if channel:
                try:
                    await channel.delete(reason='Onboarding completed — auto-cleanup')
                except discord.Forbidden:
                    log.warning(f'Cannot delete channel {row["channel_id"]}')
            await self.bot.db.remove_deletion(row['channel_id'])

    @check_scheduled_deletions.before_loop
    async def before_deletion_check(self):
        await self.bot.wait_until_ready()

    # ── Task: inactivity checks ───────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def check_inactivity(self):
        guild = self.bot.get_guild(self.bot.guild_id)
        if not guild:
            return

        # 14-day threshold: kick + delete
        old_requests = await self.bot.db.get_inactive_requests(str(guild.id), days=14)
        for req in old_requests:
            if req.get('inactivity_warned') != 1:
                continue  # Should have been warned first; skip if warning was missed
            member = guild.get_member(int(req['discord_id']))
            channel = guild.get_channel(int(req['channel_id'])) if req.get('channel_id') else None
            try:
                if member:
                    await member.kick(reason='Onboarding inactive for 14 days')
            except discord.Forbidden:
                pass
            if channel:
                try:
                    await channel.delete(reason='Onboarding inactive — 14 days')
                except discord.Forbidden:
                    pass
            await self.bot.db.delete_user_request(req['discord_id'], str(guild.id))

        # 7-day threshold: warn (only if not already warned)
        warned_requests = await self.bot.db.get_inactive_requests(str(guild.id), days=7)
        for req in warned_requests:
            if req.get('inactivity_warned') == 1:
                continue
            member = guild.get_member(int(req['discord_id']))
            channel = guild.get_channel(int(req['channel_id'])) if req.get('channel_id') else None
            if member and channel:
                try:
                    await channel.send(
                        f'{member.mention} ⚠️ Your onboarding has been **inactive for 7 days**.\n'
                        'Please continue your application, or you will be removed from the server '
                        'in 7 more days.\n\nUse `/reset-request` to start over if needed.'
                    )
                except discord.Forbidden:
                    pass
            await self.bot.db.update_user_request(
                req['discord_id'], str(guild.id), inactivity_warned=1
            )

    @check_inactivity.before_loop
    async def before_inactivity_check(self):
        await self.bot.wait_until_ready()

    # ── Task: re-verification deadline / warning checks ──────────────────────

    @tasks.loop(hours=1)
    async def check_reverification_inactivity(self):
        guild = self.bot.get_guild(self.bot.guild_id)
        if not guild:
            return
        onboarding = self._get_onboarding()
        pending = await self.bot.db.get_all_pending_reverifications(str(guild.id))
        for rev in pending:
            req = await self.bot.db.get_user_request(rev['discord_id'], str(guild.id))
            if not req or req.get('status') == 'completed':
                await self.bot.db.delete_reverification(rev['discord_id'], str(guild.id))
                continue
            member = guild.get_member(int(rev['discord_id']))
            if not member:
                await self.bot.db.delete_reverification(rev['discord_id'], str(guild.id))
                await self.bot.db.delete_user_request(rev['discord_id'], str(guild.id))
                continue
            channel = guild.get_channel(int(req['channel_id'])) if req.get('channel_id') else None
            try:
                created_at = datetime.fromisoformat(req['created_at'])
            except (ValueError, TypeError):
                continue
            days_elapsed = (datetime.utcnow() - created_at).days

            # Fail at 21 days
            if days_elapsed >= 21:
                if channel:
                    try:
                        await channel.send(
                            f'{member.mention} ⏰ Re-verification deadline has passed. '
                            'Your access will now be revoked.'
                        )
                    except discord.Forbidden:
                        pass
                if onboarding:
                    await onboarding._fail_reverification(channel, member)
                continue

            # Warn every 7 days and send a final warning at day 20
            warn_count = rev.get('warn_count') or 0
            last_warned_str = rev.get('last_warned_at')
            last_warned = datetime.fromisoformat(last_warned_str) if last_warned_str else created_at
            days_since_warn = (datetime.utcnow() - last_warned).days
            final_warning = days_elapsed >= 20 and warn_count < (days_elapsed // 7 + 1)
            if days_since_warn >= 7 or final_warning:
                days_left = 21 - days_elapsed
                urgency = '🚨 **FINAL WARNING** — ' if days_elapsed >= 20 else '⚠️ '
                if channel:
                    try:
                        await channel.send(
                            f'{member.mention} {urgency}Re-verification reminder: '
                            f'**{days_left} day(s) remaining**.\n'
                            'Complete your verification to keep your access.'
                        )
                    except discord.Forbidden:
                        pass
                await self.bot.db.update_reverification_warn(rev['discord_id'], str(guild.id))

    @check_reverification_inactivity.before_loop
    async def before_reverification_check(self):
        await self.bot.wait_until_ready()

    # ── Task: daily role audit at 07:00 UTC ──────────────────────────────────

    @tasks.loop(hours=1)
    async def daily_role_audit(self):
        now = datetime.utcnow()
        if now.hour != 7:
            return
        if self._daily_check_ran_hour == now.day:
            return  # Already ran today
        self._daily_check_ran_hour = now.day

        guild = self.bot.get_guild(self.bot.guild_id)
        if not guild:
            return

        await self._run_audit(guild)

    @daily_role_audit.before_loop
    async def before_daily_audit(self):
        await self.bot.wait_until_ready()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _downgrade_to_visitor(
        self, guild: discord.Guild, member: discord.Member,
        tracked: dict, config: dict, warera_data: dict
    ):
        # Remove base embassy/citizen role (and write role if held)
        roles_to_remove = []
        if tracked.get('discord_role_id'):
            old_role = guild.get_role(int(tracked['discord_role_id']))
            if old_role and old_role in member.roles:
                roles_to_remove.append(old_role)
        # Also strip any write role they may hold
        await self._revoke_write_role_if_held(guild, member, str(member.id))
        # Revoke any write grants they made
        await self._revoke_grants_by_grantor(guild, str(member.id))
        # Strip any local Congolese government roles
        for _, db_key, _ in CONGO_LOCAL_ROLES:
            role_id = config.get(db_key) if config else None
            if role_id:
                gov_role = guild.get_role(int(role_id))
                if gov_role and gov_role in member.roles:
                    roles_to_remove.append(gov_role)

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove)
            except discord.Forbidden:
                pass

        # Assign visitor role
        visitor_role = None
        if config and config.get('visitor_role_id'):
            visitor_role = guild.get_role(int(config['visitor_role_id']))
            if visitor_role:
                try:
                    await member.add_roles(visitor_role)
                except discord.Forbidden:
                    pass

        await self.bot.db.upsert_tracked_user(
            str(member.id), str(guild.id),
            tracked['warera_id'], 'visitor',
            warera_data.get('country'),
            str(visitor_role.id) if visitor_role else None
        )

        try:
            await member.send(
                '⚠️ Your role in the **Congo Discord** has been changed to **Visitor** '
                'because you no longer meet the requirements for your previous role.'
            )
        except discord.Forbidden:
            pass

    async def _switch_embassy(
        self, guild: discord.Guild, member: discord.Member,
        tracked: dict, config: dict, warera_data: dict,
        new_country_id: str, access_level: str, warera_role: str
    ):
        """Strip the old embassy role(s) and grant access to the new country's embassy."""
        onboarding_cog = self.bot.get_cog('OnboardingCog')

        # --- Remove old embassy roles ---
        if tracked.get('discord_role_id'):
            old_role = guild.get_role(int(tracked['discord_role_id']))
            if old_role and old_role in member.roles:
                try:
                    await member.remove_roles(old_role)
                except discord.Forbidden:
                    pass
        await self._revoke_write_role_if_held(guild, member, str(member.id))
        await self._revoke_grants_by_grantor(guild, str(member.id))

        # --- Look up new country details ---
        country_data = await get_country_by_id(new_country_id) if new_country_id else None
        country_name = country_data.get('name', 'Unknown') if country_data else 'Unknown'
        country_flag = get_flag(country_name)

        # --- Ensure new embassy channel/roles exist ---
        if not onboarding_cog:
            log.warning('OnboardingCog not found; cannot switch embassy for %s', member)
            return

        embassy_cat = await onboarding_cog._ensure_embassy_category(guild, config)
        emb_channel, base_role, write_role = await onboarding_cog._ensure_embassy_channel_role(
            guild, embassy_cat, country_name, country_flag, config
        )

        # --- Grant new roles ---
        try:
            await member.add_roles(base_role)
        except discord.Forbidden:
            pass
        if access_level == 'write':
            try:
                await member.add_roles(write_role)
            except discord.Forbidden:
                pass

        # --- Update DB records ---
        await self.bot.db.update_embassy_request(
            str(member.id), str(guild.id),
            embassy_channel_id=str(emb_channel.id),
            embassy_role_id=str(base_role.id),
            embassy_write_role_id=str(write_role.id),
            approval_status='approved'
        )
        await self.bot.db.upsert_tracked_user(
            str(member.id), str(guild.id),
            tracked['warera_id'], 'embassy',
            new_country_id, str(base_role.id)
        )

        try:
            await member.send(
                f'🔄 Your country changed — your embassy access has been updated to '
                f'**{country_name} {country_flag}** ({emb_channel.mention}).'
            )
        except discord.Forbidden:
            pass

    async def _revoke_write_role_if_held(
        self, guild: discord.Guild, member: discord.Member, discord_id: str
    ):
        """Remove the write role from a member if they still hold it."""
        embassy_req = await self.bot.db.get_embassy_request(discord_id, str(guild.id))
        if embassy_req and embassy_req.get('embassy_write_role_id'):
            write_role = guild.get_role(int(embassy_req['embassy_write_role_id']))
            if write_role and write_role in member.roles:
                try:
                    await member.remove_roles(write_role)
                except discord.Forbidden:
                    pass

    async def _revoke_grants_by_grantor(
        self, guild: discord.Guild, grantor_discord_id: str, reason: str = None
    ):
        """Revoke all write grants made by this grantor."""
        grants = await self.bot.db.remove_all_write_grants_by_grantor(
            grantor_discord_id, str(guild.id)
        )
        default_reason = reason or 'the official who granted it is no longer in a qualifying government role'
        for grant in grants:
            grantee = guild.get_member(int(grant['grantee_discord_id']))
            if not grantee:
                continue
            write_role = guild.get_role(int(grant['write_role_id']))
            if write_role and write_role in grantee.roles:
                try:
                    await grantee.remove_roles(
                        write_role, reason=f'Write grant revoked: {default_reason}'
                    )
                except discord.Forbidden:
                    pass
                try:
                    await grantee.send(
                        f'⚠️ Your **write access** in an embassy has been revoked because '
                        f'{default_reason}.'
                    )
                except discord.Forbidden:
                    pass

    async def _run_audit(self, guild: discord.Guild):
        """Core of the daily role audit — usable by the scheduler and admin commands."""
        config = await self.bot.db.get_guild_config(str(guild.id))
        tracked = await self.bot.db.get_all_tracked_users(str(guild.id))
        log.info(f'Role audit: checking {len(tracked)} tracked users')

        # Pre-fetch all WarEra data in batch to minimise HTTP calls
        all_warera_ids = [t['warera_id'] for t in tracked]
        warera_results = await batch_get_user_lite(all_warera_ids)
        warera_map = {uid: data for uid, data in zip(all_warera_ids, warera_results) if data}

        for t in tracked:
            member = guild.get_member(int(t['discord_id']))
            if not member:
                continue

            warera_data = warera_map.get(t['warera_id'])
            if not warera_data:
                continue

            assigned = t.get('assigned_role')

            if assigned == 'citizen':
                if warera_data.get('country') != CONGO_COUNTRY_ID:
                    log.info(f'Downgrading {member} from citizen (no longer Congo)')
                    await self._downgrade_to_visitor(guild, member, t, config, warera_data)
                else:
                    onboarding = self._get_onboarding()
                    if onboarding:
                        await onboarding.sync_congo_local_roles(guild, member, warera_data, config)

            elif assigned == 'visitor':
                infos = warera_data.get('infos', {})
                role_field, access_level, _ = get_government_role(infos)
                if role_field and access_level:
                    await self._notify_upgrade_available(member, role_field)

            elif assigned == 'embassy':
                infos = warera_data.get('infos', {})
                role_field, access_level, warera_role = get_government_role(infos)
                current_country = warera_data.get('country')
                country_changed = bool(current_country and current_country != t.get('country_id'))
                if current_country == CONGO_COUNTRY_ID:
                    citizen_role_id = config.get('citizen_role_id') if config else None
                    if citizen_role_id:
                        citizen_role = guild.get_role(int(citizen_role_id))
                        if citizen_role and citizen_role not in member.roles:
                            try:
                                await member.add_roles(citizen_role)
                            except discord.Forbidden:
                                pass
                    onboarding = self._get_onboarding()
                    if onboarding:
                        await onboarding.sync_congo_local_roles(guild, member, warera_data, config)
                if not role_field:
                    log.info(f'Downgrading {member} from embassy (lost government role)')
                    await self._downgrade_to_visitor(guild, member, t, config, warera_data)
                elif country_changed:
                    log.info(f'Switching {member} embassy: {t.get("country_id")} → {current_country}')
                    await self._switch_embassy(guild, member, t, config, warera_data, current_country, access_level, warera_role)
                elif access_level != 'write':
                    grants_received = await self.bot.db.get_write_grants_by_grantee(
                        str(t['discord_id']), str(guild.id)
                    )
                    if not grants_received:
                        await self._revoke_write_role_if_held(guild, member, str(t['discord_id']))
                    await self._revoke_grants_by_grantor(guild, str(t['discord_id']))

        await self._audit_write_grants(guild)

    async def _audit_write_grants(self, guild: discord.Guild):
        """Check all write grants and revoke any where the grantor lost their qualifying role."""
        grants = await self.bot.db.get_all_write_grants(str(guild.id))

        # Pre-load the Senate role once
        config = await self.bot.db.get_guild_config(str(guild.id))
        senate_role = None
        if config and config.get('senate_role_id'):
            senate_role = guild.get_role(int(config['senate_role_id']))

        # Batch-fetch WarEra data for all non-senate grantors up front
        non_senate_grants = [g for g in grants if g.get('grant_type') != 'senate']
        grantor_ids = [g['grantor_warera_id'] for g in non_senate_grants]
        fetched = await batch_get_user_lite(grantor_ids)
        warera_map = {uid: data for uid, data in zip(grantor_ids, fetched) if data}

        for grant in grants:
            if grant.get('grant_type') == 'senate':
                # Senate grant: check that the grantor still holds the Senate role
                grantor_member = guild.get_member(int(grant['grantor_discord_id']))
                still_senate = (
                    grantor_member is not None
                    and senate_role is not None
                    and senate_role in grantor_member.roles
                )
                if not still_senate:
                    log.info(
                        f'Revoking senate write grant: grantor {grant["grantor_discord_id"]} '
                        'lost Senate role'
                    )
                    await self._revoke_grants_by_grantor(
                        guild, grant['grantor_discord_id'],
                        reason='the senator who guaranteed it is no longer in the Senate'
                    )
            else:
                # Official grant: check WarEra write-level role
                warera_data = warera_map.get(grant['grantor_warera_id'])
                if not warera_data:
                    continue
                infos = warera_data.get('infos', {})
                _, access_level, _ = get_government_role(infos)
                if access_level != 'write':
                    log.info(
                        f'Revoking official write grant: grantor {grant["grantor_discord_id"]} '
                        'lost write-level government role'
                    )
                    await self._revoke_grants_by_grantor(guild, grant['grantor_discord_id'])

    async def _notify_upgrade_available(self, member: discord.Member, role_field: str):
        try:
            await member.send(
                f'📢 You now hold the government role **{role_display_name(role_field)}** in WarEra!\n'
                'You may be eligible for Embassy or Citizen access.\n'
                'Please re-join the server or contact an admin.'
            )
        except discord.Forbidden:
            pass


    # ── Task: daily database backup ───────────────────────────────────────────

    @tasks.loop(hours=1)
    async def daily_backup(self):
        try:
            await self.bot.db.backup()
            log.info('Database backup written to congobot.db.bak')
        except Exception:
            log.exception('Database backup failed')

    @daily_backup.before_loop
    async def before_daily_backup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(SchedulerCog(bot))
