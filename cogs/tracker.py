import asyncio
import io
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import discord
import matplotlib
matplotlib.use('Agg')  # non-interactive backend, must be set before importing pyplot
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
from discord import app_commands
from discord.ext import commands, tasks

from warera_api import get_users_by_country, get_user_lite, get_country_by_id, batch_get_user_lite

log = logging.getLogger(__name__)

# ── Level brackets ────────────────────────────────────────────────────────────

THREAT_WEIGHT = {'low': 0.6, 'mid': 2, 'high': 3.5, 'master': 5}
DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
ONLINE_THRESHOLD_MINUTES = 30
THREAT_MAX_PER_USER = max(THREAT_WEIGHT.values())  # 5 — used for max_score calculations
ACTIVE_MIN_LEVEL = 10  # players at or below this level are excluded from active count


def _level_bracket(level) -> str:
    try:
        level = int(level or 0)
    except (TypeError, ValueError):
        level = 0
    if level < 22:
        return 'low'
    if level < 28:
        return 'mid'
    if level <= 33:
        return 'high'
    return 'master'


def _threat_score(low: int, mid: int, high: int, master: int) -> float:
    return (low * THREAT_WEIGHT['low'] + mid * THREAT_WEIGHT['mid']
            + high * THREAT_WEIGHT['high'] + master * THREAT_WEIGHT['master'])


# WarEra returns lastOnline in JS Date format:
#   "Fri Mar 20 2026 13:03:49 GMT+0000 (Coordinated Universal Time)"
# We also handle ISO 8601 as a fallback.
_JS_DATE_RE = re.compile(r'\w+ (\w+) (\d+) (\d+) (\d+):(\d+):(\d+) GMT[+-]\d{4}')
_MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
)}


def _parse_last_online(s) -> Optional[datetime]:
    if not s:
        return None
    # ISO 8601: "2026-03-20T13:03:49.000Z" or "2026-03-20T13:03:49+00:00"
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, AttributeError):
        pass
    # JS Date: "Fri Mar 20 2026 13:03:49 GMT+0000 (Coordinated Universal Time)"
    m = _JS_DATE_RE.search(str(s))
    if m:
        month_str, day, year, hour, minute, second = m.groups()
        month = _MONTH_MAP.get(month_str)
        if month:
            try:
                return datetime(int(year), month, int(day),
                                int(hour), int(minute), int(second))
            except ValueError:
                pass
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────

class TrackerCog(commands.Cog, name='TrackerCog'):
    def __init__(self, bot):
        self.bot = bot
        self.poll_countries.start()

    def cog_unload(self):
        self.poll_countries.cancel()

    # ── Senate role guard ─────────────────────────────────────────────────────

    async def _check_senate(self, interaction: discord.Interaction) -> bool:
        config = await self.bot.db.get_guild_config(str(interaction.guild_id))
        senate_role_id = config.get('senate_role_id') if config else None
        if not senate_role_id or not any(
            r.id == int(senate_role_id) for r in interaction.user.roles
        ):
            await interaction.response.send_message(
                'This command requires the Senate role.', ephemeral=True
            )
            return False
        return True

    # ── Background polling ────────────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def poll_countries(self):
        countries = await self.bot.db.get_all_tracked_countries()
        if not countries:
            return
        await asyncio.gather(
            *[self._safe_snapshot(c['country_id']) for c in countries],
            return_exceptions=True
        )

    async def _safe_snapshot(self, country_id: str):
        try:
            await self._snapshot_country(country_id)
        except Exception as e:
            log.error('Tracker: snapshot failed for %s: %s', country_id, e)

    @poll_countries.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ── Snapshot logic ────────────────────────────────────────────────────────

    async def _snapshot_country(self, country_id: str):
        """Paginate all users in country, extract online status + level, store snapshot."""
        # 1. Paginate getUsersByCountry — collect all user items
        all_items = []
        cursor = None
        while True:
            page = await get_users_by_country(country_id, cursor)
            if not page:
                break
            # Support both flat and tRPC-json-wrapped responses
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
            log.warning('Tracker: no users found for country %s', country_id)
            return 0, 0, {'low': 0, 'mid': 0, 'high': 0, 'master': 0}

        # 2. Fetch getUserLite for each user via tRPC batch calls
        user_ids = [
            u.get('_id') or u.get('id') or u.get('userId')
            for u in all_items
        ]
        user_ids = [uid for uid in user_ids if uid]
        # Deduplicate while preserving order (cursor overlap can return same user twice)
        seen: set = set()
        deduped: list = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                deduped.append(uid)
        user_ids = deduped

        # Build uid→item map for createdAt lookups in the counting loop.
        # New accounts (<24h) have lastConnectionAt = creation time and inflate active counts.
        # Ghost accounts (created >48h ago, never logged back in after first 48h) are also excluded.
        now_utc = datetime.utcnow()
        uid_map = {
            (u.get('_id') or u.get('id') or u.get('userId')): u
            for u in all_items
        }
        new_user_ids: set = set()
        for uid, item in uid_map.items():
            created_ts = _parse_last_online(item.get('createdAt'))
            if created_ts and created_ts > now_utc - timedelta(days=4):
                new_user_ids.add(uid)

        results = await batch_get_user_lite(user_ids)

        # 3. Count online users and their level brackets
        #    Level is nested: r['leveling']['level']
        #    Last connection is nested: r['dates']['lastConnectionAt']
        online_threshold = now_utc - timedelta(minutes=ONLINE_THRESHOLD_MINUTES)
        active_threshold = now_utc - timedelta(days=7)
        counts = {'low': 0, 'mid': 0, 'high': 0, 'master': 0}
        online_total = 0
        active_count = 0

        for uid, r in zip(user_ids, results):
            if not isinstance(r, dict):
                continue
            if uid in new_user_ids:
                continue  # Skip accounts created in last 4 days
            dates = r.get('dates') or {}
            last = dates.get('lastConnectionAt') if isinstance(dates, dict) else None
            ts = _parse_last_online(last)
            # Skip ghost accounts: created >48h ago but never returned after their first 48h
            created_ts = _parse_last_online((uid_map.get(uid) or {}).get('createdAt'))
            if (ts and created_ts
                    and now_utc - created_ts > timedelta(days=4)
                    and ts - created_ts <= timedelta(hours=48)):
                continue
            leveling = r.get('leveling') or {}
            level = leveling.get('level') if isinstance(leveling, dict) else None
            if ts and ts > active_threshold and level is not None and level > ACTIVE_MIN_LEVEL:
                active_count += 1
            if ts and ts > online_threshold:
                online_total += 1
                bracket = _level_bracket(level)
                counts[bracket] += 1

        # 4. Store snapshot — use deduplicated user count as total_users
        total_users = len(user_ids)
        now = now_utc.isoformat()
        await self.bot.db.insert_activity_snapshot(
            country_id, now, total_users, online_total,
            counts['low'], counts['mid'], counts['high'], counts['master'],
            active_users=active_count
        )
        log.info(
            'Tracker: %s — %d/%d online, %d active (%d raw) (low=%d mid=%d high=%d master=%d)',
            country_id, online_total, total_users, active_count, len(all_items),
            counts['low'], counts['mid'], counts['high'], counts['master']
        )
        return total_users, online_total, counts, active_count

    # ── Heatmap generation ────────────────────────────────────────────────────

    def _generate_heatmap(self, snapshots: list, country_name: str) -> discord.File:
        """Build a three-panel PNG: raw % heatmap, threat heatmap, hourly bar chart."""
        # Build 7×24 grids (day_of_week × hour).
        # For each (iso_year, iso_week, dow, hour) occurrence take the PEAK online count
        # within that hour, then average those peaks across weeks.  This avoids
        # under-counting caused by players who stay online across multiple 15-min snapshots.
        peak_by_slot: dict = defaultdict(lambda: {
            'online': -1, 'total': 1, 'score': 0.0, 'max_score': 1.0
        })

        for snap in snapshots:
            try:
                ts = datetime.fromisoformat(snap['snapshot_time'])
            except (ValueError, KeyError):
                continue
            dow = ts.weekday()
            hour = ts.hour
            iso_year, iso_week, _ = ts.isocalendar()
            slot_key = (iso_year, iso_week, dow, hour)

            total = snap.get('active_users') or snap['total_users'] or 1
            online = snap['online_count']
            score = _threat_score(
                snap['online_low'], snap['online_mid'],
                snap['online_high'], snap['online_master']
            )
            max_score = total * THREAT_MAX_PER_USER
            if online > peak_by_slot[slot_key]['online']:
                peak_by_slot[slot_key] = {
                    'online': online, 'total': total,
                    'score': score, 'max_score': max_score,
                    'dow': dow, 'hour': hour
                }

        raw_totals = np.zeros((7, 24))
        threat_totals = np.zeros((7, 24))
        counts_grid = np.zeros((7, 24))
        hourly_online_sum = np.zeros(24)
        hourly_online_cnt = np.zeros(24)

        for data in peak_by_slot.values():
            d, h = data['dow'], data['hour']
            total = data['total'] or 1
            max_score = data['max_score'] or 1
            raw_totals[d, h] += data['online'] / total * 100
            threat_totals[d, h] += data['score'] / max_score * 100 if max_score else 0
            counts_grid[d, h] += 1
            hourly_online_sum[h] += data['online']
            hourly_online_cnt[h] += 1

        raw_grid = np.zeros((7, 24))
        threat_grid = np.zeros((7, 24))
        mask = counts_grid > 0
        raw_grid[mask] = raw_totals[mask] / counts_grid[mask]
        threat_grid[mask] = threat_totals[mask] / counts_grid[mask]

        # Colour map: yellow (safe/low) → red (dangerous/high)
        cmap = mcolors.LinearSegmentedColormap.from_list(
            'attack', ['#2ecc40', '#ffdc00', '#ff851b', '#ff4136']
        )

        fig, axes = plt.subplots(4, 1, figsize=(14, 18))
        fig.patch.set_facecolor('#1a1a2e')

        hour_labels = [f'{h:02d}:00' for h in range(24)]

        def _draw_heatmap(ax, grid, title):
            ax.set_facecolor('#1a1a2e')
            im = ax.imshow(
                grid, cmap=cmap, aspect='auto', vmin=0, vmax=100,
                interpolation='nearest'
            )
            ax.set_xticks(range(24))
            ax.set_xticklabels(hour_labels, rotation=45, ha='right',
                               fontsize=7, color='white')
            ax.set_yticks(range(7))
            ax.set_yticklabels(DAY_NAMES, color='white', fontsize=9)
            ax.set_title(title, color='white', fontsize=11, pad=8)
            # Annotate each cell with the value
            for dow in range(7):
                for hour in range(24):
                    val = grid[dow, hour]
                    if counts_grid[dow, hour] > 0:
                        text_color = 'white' if val > 50 else '#cccccc'
                        ax.text(hour, dow, f'{val:.0f}%', ha='center', va='center',
                                fontsize=6, color=text_color)
            cb = plt.colorbar(im, ax=ax, fraction=0.015, pad=0.02)
            cb.ax.yaxis.set_tick_params(color='white')
            cb.set_label('% online', color='white', fontsize=8)
            plt.setp(cb.ax.yaxis.get_ticklabels(), color='white', fontsize=7)

        _draw_heatmap(axes[0], raw_grid,
                      f'{country_name} — Raw Online % (UTC)\n(green = low online = good attack time)')
        _draw_heatmap(axes[1], threat_grid,
                      f'{country_name} — Threat-Weighted Heatmap (UTC)\n(weights: low×0.6, mid×2, high×3.5, master×5)')

        # Bar chart: hourly average threat score collapsed across all days
        ax3 = axes[2]
        ax3.set_facecolor('#1a1a2e')
        hourly_threat = np.zeros(24)
        hourly_counts = np.zeros(24)
        for dow in range(7):
            for h in range(24):
                if counts_grid[dow, h] > 0:
                    hourly_threat[h] += threat_grid[dow, h]
                    hourly_counts[h] += 1
        hourly_avg = np.where(hourly_counts > 0, hourly_threat / hourly_counts, 0)

        # Colour bars by quartile
        bar_colors = []
        for val in hourly_avg:
            if val < 25:
                bar_colors.append('#2ecc40')
            elif val < 50:
                bar_colors.append('#ffdc00')
            elif val < 75:
                bar_colors.append('#ff851b')
            else:
                bar_colors.append('#ff4136')

        bars = ax3.bar(range(24), hourly_avg, color=bar_colors, edgecolor='#333355')
        ax3.set_xticks(range(24))
        ax3.set_xticklabels(hour_labels, rotation=45, ha='right',
                            fontsize=7, color='white')
        ax3.set_ylabel('Avg threat score (%)', color='white', fontsize=9)
        ax3.set_title(
            f'{country_name} — Average Hourly Threat (all days)',
            color='white', fontsize=11
        )
        ax3.set_ylim(0, 100)
        ax3.tick_params(axis='y', colors='white')
        ax3.spines['bottom'].set_color('#555577')
        ax3.spines['left'].set_color('#555577')
        ax3.spines['top'].set_visible(False)
        ax3.spines['right'].set_visible(False)

        # 4th panel: average absolute online count per hour
        ax4 = axes[3]
        ax4.set_facecolor('#1a1a2e')
        hourly_online_avg = np.where(hourly_online_cnt > 0,
                                     hourly_online_sum / hourly_online_cnt, 0)
        max_val = hourly_online_avg.max() or 1
        bar_colors4 = []
        for val in hourly_online_avg:
            pct = val / max_val
            if pct < 0.25:
                bar_colors4.append('#2ecc40')
            elif pct < 0.5:
                bar_colors4.append('#ffdc00')
            elif pct < 0.75:
                bar_colors4.append('#ff851b')
            else:
                bar_colors4.append('#ff4136')
        ax4.bar(range(24), hourly_online_avg, color=bar_colors4, edgecolor='#333355')
        ax4.set_xticks(range(24))
        ax4.set_xticklabels(hour_labels, rotation=45, ha='right', fontsize=7, color='white')
        ax4.set_ylabel('Avg players online', color='white', fontsize=9)
        ax4.set_title(f'{country_name} — Average Players Online per Hour (all days)',
                      color='white', fontsize=11)
        ax4.tick_params(axis='y', colors='white')
        ax4.spines['bottom'].set_color('#555577')
        ax4.spines['left'].set_color('#555577')
        ax4.spines['top'].set_visible(False)
        ax4.spines['right'].set_visible(False)

        fig.tight_layout(pad=2.0)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=110, bbox_inches='tight',
                    facecolor='#1a1a2e')
        plt.close(fig)
        buf.seek(0)
        return discord.File(buf, filename='activity_heatmap.png')

    def _best_attack_windows(self, snapshots: list, top_n: int = 5) -> list:
        """Return list of (dow, hour, minute, avg_threat_pct, avg_online_pct, snap_count, avgs)."""
        slot_threat: dict = defaultdict(float)
        slot_raw: dict = defaultdict(float)
        slot_counts: dict = defaultdict(int)
        slot_levels: dict = defaultdict(lambda: {'low': 0.0, 'mid': 0.0, 'high': 0.0, 'master': 0.0})

        for snap in snapshots:
            try:
                ts = datetime.fromisoformat(snap['snapshot_time'])
            except (ValueError, KeyError):
                continue
            dow = ts.weekday()
            hour = ts.hour
            minute = (ts.minute // 15) * 15
            key = (dow, hour, minute)
            total = snap.get('active_users') or snap['total_users'] or 1
            score = _threat_score(
                snap['online_low'], snap['online_mid'],
                snap['online_high'], snap['online_master']
            )
            max_score = total * THREAT_MAX_PER_USER
            slot_threat[key] += score / max_score * 100 if max_score else 0
            slot_raw[key] += snap['online_count'] / total * 100
            slot_counts[key] += 1
            for bracket in ('low', 'mid', 'high', 'master'):
                slot_levels[key][bracket] += snap[f'online_{bracket}']

        results = []
        for key, n in slot_counts.items():
            dow, hour, minute = key
            threat_avg = slot_threat[key] / n
            raw_avg = slot_raw[key] / n
            avgs = {k: slot_levels[key][k] / n for k in ('low', 'mid', 'high', 'master')}
            results.append((dow, hour, minute, threat_avg, raw_avg, n, avgs))

        results.sort(key=lambda x: x[3])
        return results[:top_n]

    # ── /track ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name='track',
        description='Start tracking a country\'s player activity every 15 minutes.'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        existing = await self.bot.db.get_tracked_country(country_id)
        if existing:
            await interaction.followup.send(
                f'Already tracking **{existing["country_name"] or country_id}**. '
                f'Use `/track-now` to take a snapshot or `/track-stats` to see results.',
                ephemeral=True
            )
            return

        country_data = await get_country_by_id(country_id)
        if not country_data:
            await interaction.followup.send(
                f'Could not find a country with ID `{country_id}`. '
                f'Double-check the ID and try again.',
                ephemeral=True
            )
            return

        country_name = country_data.get('name') or country_id
        country_flag = country_data.get('flag') or ''

        await self.bot.db.add_tracked_country(
            country_id, country_name, country_flag,
            str(interaction.channel_id), str(interaction.guild_id),
            str(interaction.user.id)
        )

        await interaction.followup.send(
            f'Now tracking **{country_flag} {country_name}** (`{country_id}`).\n'
            f'Snapshots are taken every 15 minutes. Use `/track-now` to get an '
            f'immediate snapshot, or `/track-stats` after a few hours to see patterns.',
            ephemeral=True
        )

    # ── /track-stop ───────────────────────────────────────────────────────────

    @app_commands.command(
        name='track-stop',
        description='Stop tracking a country\'s player activity.'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track_stop(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return

        existing = await self.bot.db.get_tracked_country(country_id)
        if not existing:
            await interaction.response.send_message(
                f'`{country_id}` is not currently being tracked.', ephemeral=True
            )
            return

        await self.bot.db.remove_tracked_country(country_id)
        name = existing.get('country_name') or country_id
        snap_count = await self.bot.db.get_snapshot_count(country_id)
        await interaction.response.send_message(
            f'Stopped tracking **{name}**. '
            f'{snap_count} snapshots remain in the database for `/track-stats`.',
            ephemeral=True
        )

    # ── /track-purge ──────────────────────────────────────────────────────────

    @app_commands.command(
        name='track-purge',
        description='Delete all stored snapshots for a country (keeps tracking active).'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track_purge(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return

        deleted = await self.bot.db.purge_activity_snapshots(country_id)
        existing = await self.bot.db.get_tracked_country(country_id)
        name = (existing.get('country_name') if existing else None) or country_id
        still_tracking = ' Still tracking — new snapshots will accumulate.' if existing else ''
        await interaction.response.send_message(
            f'Deleted **{deleted}** snapshot(s) for **{name}**.{still_tracking}',
            ephemeral=True
        )

    # ── /track-now ────────────────────────────────────────────────────────────

    @app_commands.command(
        name='track-now',
        description='Take an immediate activity snapshot for a tracked country.'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track_now(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        existing = await self.bot.db.get_tracked_country(country_id)
        if not existing:
            # Allow one-off snapshots even if not in the auto-track list, but warn
            country_data = await get_country_by_id(country_id)
            name = (country_data.get('name') if country_data else None) or country_id
            flag = (country_data.get('flag') if country_data else None) or ''
        else:
            name = existing.get('country_name') or country_id
            flag = existing.get('country_flag') or ''

        try:
            total, online, counts, active = await self._snapshot_country(country_id)
        except Exception as e:
            await interaction.followup.send(
                f'Snapshot failed: {e}', ephemeral=True
            )
            return

        threat = _threat_score(counts['low'], counts['mid'], counts['high'], counts['master'])
        max_threat = (active if active else total) * THREAT_MAX_PER_USER
        pct = online / active * 100 if active else (online / total * 100 if total else 0)
        threat_pct = threat / max_threat * 100 if max_threat else 0

        snap_count = await self.bot.db.get_snapshot_count(country_id)
        lines = [
            f'**{flag} {name}** — snapshot #{snap_count} stored',
            f'`{online}/{active}` active users online ({pct:.1f}%)  '
            f'*(total registered: {total})*',
            f'Low (<20): **{counts["low"]}** 🟢  '
            f'Mid (20-27): **{counts["mid"]}** 🟡  '
            f'High (28-35): **{counts["high"]}** 🟠  '
            f'Master (35+): **{counts["master"]}** 🔴',
            f'Threat score: **{threat}** pts / {max_threat} max ({threat_pct:.1f}%)',
            f'`/track-stats` available after {max(0, 8 - snap_count)} more snapshot(s).',
        ]
        if not existing:
            lines.append(
                f'\n*Country not in auto-track list. Use `/track {country_id}` to add it.*'
            )

        await interaction.followup.send('\n'.join(lines), ephemeral=True)

    # ── /track-stats ──────────────────────────────────────────────────────────

    @app_commands.command(
        name='track-stats',
        description='Show activity heatmap and best attack windows for a tracked country.'
    )
    @app_commands.describe(
        country_id='WarEra country ID (24-char hex)',
        days='How many days of history to include (default: 30)'
    )
    async def track_stats(
        self, interaction: discord.Interaction,
        country_id: str,
        days: int = 30
    ):
        if not await self._check_senate(interaction):
            return
        await interaction.response.defer()

        snap_count = await self.bot.db.get_snapshot_count(country_id)
        if snap_count < 8:
            await interaction.followup.send(
                f'Not enough data yet for `{country_id}`. '
                f'Need at least 8 snapshots (2 hours); have **{snap_count}**.\n'
                f'Use `/track-now` to take an immediate snapshot.',
                ephemeral=True
            )
            return

        snapshots = await self.bot.db.get_activity_snapshots(country_id, since_days=days)
        if not snapshots:
            await interaction.followup.send(
                f'No snapshots found for `{country_id}` in the last {days} days.',
                ephemeral=True
            )
            return

        existing = await self.bot.db.get_tracked_country(country_id)
        country_name = (existing.get('country_name') if existing else None) or country_id
        country_flag = (existing.get('country_flag') if existing else None) or ''

        # Generate heatmap image
        heatmap_file = self._generate_heatmap(snapshots, f'{country_flag} {country_name}'.strip())

        # Best attack windows
        best = self._best_attack_windows(snapshots, top_n=5)
        window_lines = []
        for i, (dow, hour, minute, threat_avg, raw_avg, n, avgs) in enumerate(best, 1):
            master = avgs['master']
            high = avgs['high']
            mid = avgs['mid']
            low = avgs['low']
            window_lines.append(
                f'`{i}.` **{DAY_NAMES[dow]} {hour:02d}:{minute:02d} UTC** — '
                f'threat {threat_avg:.1f}%, {raw_avg:.1f}% online '
                f'(🔴{master:.1f} 🟠{high:.1f} 🟡{mid:.1f} 🟢{low:.1f} avg) '
                f'— {n} samples'
            )

        date_range = f'{snapshots[0]["snapshot_time"][:10]} → {snapshots[-1]["snapshot_time"][:10]}'
        embed = discord.Embed(
            title=f'Attack Windows — {country_flag} {country_name}',
            color=0x2ecc40
        )
        embed.add_field(
            name='🎯 Best Attack Windows (lowest threat)',
            value='\n'.join(window_lines) or 'Not enough data per slot.',
            inline=False
        )
        embed.set_footer(
            text=f'{len(snapshots)} snapshots  •  {date_range}  •  '
                 f'Threat weights: low×0.6, mid×2, high×3.5, master×5'
        )

        await interaction.followup.send(file=heatmap_file, embed=embed)

    # ── /track-recalibrate ────────────────────────────────────────────────────

    @app_commands.command(
        name='track-recalibrate',
        description='Backfill active_users on old snapshots using the current active player count.'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track_recalibrate(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        existing = await self.bot.db.get_tracked_country(country_id)
        name = (existing.get('country_name') if existing else None) or country_id

        await interaction.followup.send(
            f'Fetching current active players for **{name}**… this may take a minute.',
            ephemeral=True
        )

        # Paginate all users, then call getUserLite to find who's been online in 7 days
        all_items = []
        cursor = None
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

        user_ids = [
            u.get('_id') or u.get('id') or u.get('userId')
            for u in all_items
        ]
        user_ids = [uid for uid in user_ids if uid]
        # Deduplicate while preserving order
        seen: set = set()
        deduped: list = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                deduped.append(uid)
        raw_count = len(all_items)
        user_ids = deduped

        # Accounts created in last 24h: lastConnectionAt = creation time, not real play time
        now_utc = datetime.utcnow()
        new_user_threshold = now_utc - timedelta(hours=24)
        uid_map = {
            (u.get('_id') or u.get('id') or u.get('userId')): u
            for u in all_items
        }
        new_user_ids: set = set()
        for uid, item in uid_map.items():
            created_ts = _parse_last_online(item.get('createdAt'))
            if created_ts and created_ts > new_user_threshold:
                new_user_ids.add(uid)

        results = await batch_get_user_lite(user_ids)

        active_threshold = now_utc - timedelta(days=7)
        buckets = {'0-1d': 0, '1-2d': 0, '2-4d': 0, '4-7d': 0, '7d+': 0,
                   'no date': 0, 'new (<4d)': 0, 'ghost': 0}
        active_count = 0
        for uid, r in zip(user_ids, results):
            if uid in new_user_ids:
                buckets['new (<4d)'] += 1
                continue
            if not isinstance(r, dict):
                buckets['no date'] += 1
                continue
            ts = _parse_last_online((r.get('dates') or {}).get('lastConnectionAt'))
            if ts is None:
                buckets['no date'] += 1
                continue
            # Ghost account: created >48h ago, never logged back after first 48h
            created_ts = _parse_last_online((uid_map.get(uid) or {}).get('createdAt'))
            if (created_ts
                    and now_utc - created_ts > timedelta(hours=48)
                    and ts - created_ts <= timedelta(hours=48)):
                buckets['ghost'] += 1
                continue
            age = (now_utc - ts).total_seconds() / 86400
            if age < 1:
                buckets['0-1d'] += 1
            elif age < 2:
                buckets['1-2d'] += 1
            elif age < 4:
                buckets['2-4d'] += 1
            elif age < 7:
                buckets['4-7d'] += 1
            else:
                buckets['7d+'] += 1
            leveling = r.get('leveling') or {}
            level = leveling.get('level') if isinstance(leveling, dict) else None
            if ts > active_threshold and level is not None and level > ACTIVE_MIN_LEVEL:
                active_count += 1

        updated = await self.bot.db.backfill_active_users(country_id, active_count)

        unique_count = len(user_ids)
        dedup_note = (
            f' *(API returned {raw_count} raw — {raw_count - unique_count} duplicates removed)*'
            if raw_count != unique_count else ''
        )
        bucket_str = '  '.join(f'`{k}`: {v}' for k, v in buckets.items())
        await interaction.followup.send(
            f'✅ Recalibrated **{name}**: active (≤7d) = **{active_count}** '
            f'/ {unique_count} unique players{dedup_note}\n'
            f'Backfilled **{updated}** snapshot(s).\n'
            f'Date distribution: {bucket_str}',
            ephemeral=True
        )

    # ── /track-debug ──────────────────────────────────────────────────────────

    @app_commands.command(
        name='track-debug',
        description='Show raw API fields for one user from a country (for diagnostics).'
    )
    @app_commands.describe(country_id='WarEra country ID (24-char hex)')
    async def track_debug(self, interaction: discord.Interaction, country_id: str):
        if not await self._check_senate(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        import aiohttp, json as _json

        base = 'https://api2.warera.io/trpc'
        headers = {'accept': '*/*', 'Content-Type': 'application/json'}

        # ── Raw call to getUsersByCountry ─────────────────────────────────────
        payload = {'countryId': country_id, 'limit': 5}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{base}/user.getUsersByCountry', json=payload, headers=headers
            ) as resp:
                status = resp.status
                raw_text = await resp.text()

        try:
            raw_json = _json.loads(raw_text)
        except Exception:
            raw_json = None

        lines = [f'**getUsersByCountry** — HTTP {status}']
        lines.append(f'```\n{raw_text[:600]}\n```')

        if status != 200:
            await interaction.followup.send('\n'.join(lines), ephemeral=True)
            return

        # Walk down common tRPC nesting paths to find items
        data = raw_json
        for key in ('result', 'data', 'json'):
            if isinstance(data, dict) and key in data:
                data = data[key]

        lines.append(f'Raw top-level keys: `{list(raw_json.keys()) if raw_json else "n/a"}`')
        if isinstance(raw_json, dict) and 'result' in raw_json:
            r = raw_json['result']
            lines.append(f'`result` keys: `{list(r.keys()) if isinstance(r, dict) else r}`')
            if isinstance(r, dict) and 'data' in r:
                d = r['data']
                lines.append(f'`result.data` keys: `{list(d.keys()) if isinstance(d, dict) else type(d).__name__}`')
                if isinstance(d, dict) and 'json' in d:
                    j = d['json']
                    lines.append(f'`result.data.json` keys: `{list(j.keys()) if isinstance(j, dict) else type(j).__name__}`')

        # Find items wherever they live
        items = None
        for candidate in [raw_json, raw_json.get('result', {}) if raw_json else {},
                          (raw_json.get('result', {}) or {}).get('data', {}),
                          ((raw_json.get('result', {}) or {}).get('data', {}) or {}).get('json', {})]:
            if isinstance(candidate, dict) and 'items' in candidate:
                items = candidate['items']
                break

        if not items:
            lines.append('Could not find `items` array anywhere in the response.')
            lines.append(f'```json\n{raw_text[:800]}\n```')
            await interaction.followup.send('\n'.join(lines), ephemeral=True)
            return

        first_item = items[0]
        uid = first_item.get('_id') or first_item.get('id') or first_item.get('userId')
        lines += [
            f'First item keys: `{list(first_item.keys())}`',
            f'```json\n{_truncate_dict(first_item)}\n```',
        ]

        # ── Raw call to getUserLite ───────────────────────────────────────────
        if uid:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f'{base}/user.getUserLite', json={'userId': uid}, headers=headers
                ) as resp2:
                    status2 = resp2.status
                    raw2 = await resp2.text()
            try:
                raw2_json = _json.loads(raw2)
            except Exception:
                raw2_json = None

            lines.append(f'**getUserLite** (`{uid}`) — HTTP {status2}')
            if status2 == 200 and raw2_json:
                # Walk to actual user object
                user_obj = raw2_json
                for key in ('result', 'data', 'json'):
                    if isinstance(user_obj, dict) and key in user_obj:
                        user_obj = user_obj[key]
                lines += [
                    f'Keys: `{list(user_obj.keys()) if isinstance(user_obj, dict) else type(user_obj).__name__}`',
                    f'```json\n{_truncate_dict(user_obj) if isinstance(user_obj, dict) else raw2[:600]}\n```',
                ]
            else:
                lines.append(f'```\n{raw2[:400]}\n```')

        await interaction.followup.send('\n'.join(lines), ephemeral=True)


def _truncate_dict(d: dict, max_len: int = 800) -> str:
    import json
    s = json.dumps(d, default=str, indent=2)
    if len(s) > max_len:
        s = s[:max_len] + '\n... (truncated)'
    return s


async def setup(bot):
    await bot.add_cog(TrackerCog(bot))
