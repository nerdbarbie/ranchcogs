"""
CattleGuard - Role management cog for Red-DiscordBot
Tracks member activity via events and handles inactivity-based role transitions.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set

import discord
from redbot.core import checks, commands, Config
from redbot.core.bot import Red

log = logging.getLogger("red.ranchcogs.cattleguard")


class CattleGuard(commands.Cog):
    """
    Manages role assignment for new/roleless members and handles
    inactivity-based role transitions using event-driven last-seen tracking.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x636174746c65, force_registration=True)

        guild_defaults = {
            "default_role": None,        # Role ID to assign to members with no roles
            "inactivity_rules": [],      # List of {from_role, to_role, days}
            "check_interval_hours": 24,  # How often the background task runs
            "log_channel": None,         # Optional channel ID for logging actions
            "enabled": True,             # Master switch
            "bootstrap_date": None,      # ISO timestamp set on first run
        }

        self.config.register_guild(**guild_defaults)
        # Per-member last-seen: MEMBER_SEEN / guild_id / member_id → {"ts": "<iso>"}
        self.config.register_custom("MEMBER_SEEN", guild_id=None, member_id=None)

        self._task: Optional[asyncio.Task] = None

    async def cog_load(self):
        await self.bot.wait_until_ready()
        self._task = asyncio.create_task(self._inactivity_loop())

    def cog_unload(self):
        if self._task:
            self._task.cancel()

    # -------------------------------------------------------------------------
    # Last-seen helpers
    # -------------------------------------------------------------------------

    async def _get_last_seen(self, guild: discord.Guild, member: discord.Member) -> Optional[datetime]:
        """Returns the stored last-seen datetime for a member, or None."""
        try:
            data = await self.config.custom("MEMBER_SEEN", str(guild.id), str(member.id)).all()
            ts = data.get("ts")
            if ts:
                return datetime.fromisoformat(ts)
        except Exception as e:
            log.warning("Failed to read last-seen for %s: %s", member.id, e)
        return None

    async def _set_last_seen(self, guild: discord.Guild, member_id: int, dt: Optional[datetime] = None):
        """Stores the last-seen timestamp for a member."""
        ts = (dt or datetime.now(timezone.utc)).isoformat()
        try:
            await self.config.custom("MEMBER_SEEN", str(guild.id), str(member_id)).set_raw("ts", value=ts)
        except Exception as e:
            log.warning("Failed to write last-seen for %s: %s", member_id, e)

    async def _ensure_bootstrap(self, guild: discord.Guild):
        """
        On first run, stamp all current members as active as of now
        so nobody gets flagged immediately after install.
        """
        bootstrap_date = await self.config.guild(guild).bootstrap_date()
        if bootstrap_date:
            return  # Already done

        now = datetime.now(timezone.utc)
        # Write bootstrap_date FIRST to prevent double-bootstrap if something errors mid-loop
        await self.config.guild(guild).bootstrap_date.set(now.isoformat())
        log.info("CattleGuard bootstrapping guild %s (%s members)", guild.name, len(guild.members))

        for member in guild.members:
            if member.bot:
                continue
            existing = await self._get_last_seen(guild, member)
            if existing is None:
                await self._set_last_seen(guild, member.id, now)

    # -------------------------------------------------------------------------
    # Activity event listeners
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not await self.config.guild(message.guild).enabled():
            return
        await self._set_last_seen(message.guild, message.author.id)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.Member):
        # user can be discord.User in DMs or uncached guilds — guard with isinstance
        if not isinstance(user, discord.Member) or user.bot:
            return
        if not await self.config.guild(user.guild).enabled():
            return
        await self._set_last_seen(user.guild, user.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not await self.config.guild(guild).enabled():
            return

        # Stamp them as seen on join so they don't start their inactivity clock from zero
        await self._set_last_seen(guild, member.id)

        # Assign default role if configured
        default_role_id = await self.config.guild(guild).default_role()
        if not default_role_id:
            return
        role = guild.get_role(default_role_id)
        if not role:
            return
        try:
            await member.add_roles(role, reason="CattleGuard: new member default role")
            await self._log(guild, f"✅ New member {member.mention} assigned default role **{role.name}**.")
        except discord.Forbidden:
            log.warning("Missing permissions to assign default role in %s", guild.name)
        except discord.HTTPException as e:
            log.warning("HTTP error assigning default role to %s: %s", member.id, e)

    # -------------------------------------------------------------------------
    # Background loop
    # -------------------------------------------------------------------------

    async def _inactivity_loop(self):
        while True:
            try:
                await self._run_all_checks()
            except Exception as e:
                log.exception("Error during CattleGuard inactivity check: %s", e)

            # Recalculate sleep interval after each run
            interval_hours = 24
            for guild in self.bot.guilds:
                try:
                    h = await self.config.guild(guild).check_interval_hours()
                    if h < interval_hours:
                        interval_hours = h
                except Exception:
                    pass

            await asyncio.sleep(interval_hours * 3600)

    async def _run_all_checks(self):
        for guild in self.bot.guilds:
            if not await self.config.guild(guild).enabled():
                continue
            await self._ensure_bootstrap(guild)
            await self._assign_default_roles(guild)
            await self._apply_inactivity_rules(guild)

    # -------------------------------------------------------------------------
    # Default role assignment
    # -------------------------------------------------------------------------

    async def _assign_default_roles(self, guild: discord.Guild):
        default_role_id = await self.config.guild(guild).default_role()
        if not default_role_id:
            return
        role = guild.get_role(default_role_id)
        if not role:
            return

        for member in guild.members:
            if member.bot:
                continue
            # len == 1 means only @everyone — no assigned roles
            if len(member.roles) == 1:
                try:
                    await member.add_roles(role, reason="CattleGuard: no roles assigned")
                    await self._log(guild, f"✅ Assigned default role **{role.name}** to {member.mention}.")
                except discord.Forbidden:
                    await self._log(guild, f"⚠️ Missing permissions to assign role to {member.mention}.")
                except discord.HTTPException as e:
                    await self._log(guild, f"⚠️ HTTP error assigning role to {member.mention}: {e}")

    # -------------------------------------------------------------------------
    # Inactivity rule processing
    # -------------------------------------------------------------------------

    async def _apply_inactivity_rules(self, guild: discord.Guild):
        rules = await self.config.guild(guild).inactivity_rules()
        if not rules:
            return

        now = datetime.now(timezone.utc)

        for member in guild.members:
            if member.bot:
                continue

            # Mutable local copy of role IDs — updated as rules fire so later rules
            # in the same pass see the correct post-change state
            member_role_ids: Set[int] = {r.id for r in member.roles}

            last_seen = await self._get_last_seen(guild, member)

            # Fallback: if somehow untracked post-bootstrap, stamp and skip this pass
            # so we don't act on stale data
            if last_seen is None:
                fallback = member.joined_at or now
                await self._set_last_seen(guild, member.id, fallback)
                continue

            inactive_days = (now - last_seen).days

            for rule in rules:
                from_role_id = rule.get("from_role")
                to_role_id = rule.get("to_role")
                days = rule.get("days", 30)

                if from_role_id not in member_role_ids:
                    continue
                if inactive_days < days:
                    continue

                from_role = guild.get_role(from_role_id)
                to_role = guild.get_role(to_role_id) if to_role_id else None

                if not from_role:
                    log.warning(
                        "Rule references deleted role ID %s in guild %s — skipping",
                        from_role_id, guild.name
                    )
                    continue

                try:
                    await member.remove_roles(from_role, reason=f"CattleGuard: inactive {inactive_days}d")
                    if to_role:
                        await member.add_roles(to_role, reason=f"CattleGuard: inactive {inactive_days}d")
                    await self._log(
                        guild,
                        f"🔄 {member.mention} inactive **{inactive_days}d** — "
                        f"removed **{from_role.name}**"
                        + (f", added **{to_role.name}**" if to_role else "")
                        + ".",
                    )
                    # Keep local snapshot in sync for subsequent rules this pass
                    member_role_ids.discard(from_role_id)
                    if to_role_id:
                        member_role_ids.add(to_role_id)
                except discord.Forbidden:
                    await self._log(guild, f"⚠️ Missing permissions to update roles for {member.mention}.")
                    break  # No point trying further rules if we can't touch this member
                except discord.HTTPException as e:
                    await self._log(guild, f"⚠️ HTTP error updating roles for {member.mention}: {e}")

    # -------------------------------------------------------------------------
    # Logging helper
    # -------------------------------------------------------------------------

    async def _log(self, guild: discord.Guild, message: str):
        log_channel_id = await self.config.guild(guild).log_channel()
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if channel is None:
            return
        try:
            await channel.send(f"[CattleGuard] {message}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    @commands.group(name="cattleguard", aliases=["cg"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_roles=True)
    async def cattleguard(self, ctx: commands.Context):
        """CattleGuard role management settings."""

    @cattleguard.command(name="enable")
    async def cg_enable(self, ctx: commands.Context):
        """Enable CattleGuard for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ CattleGuard enabled.")

    @cattleguard.command(name="disable")
    async def cg_disable(self, ctx: commands.Context):
        """Disable CattleGuard for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("⛔ CattleGuard disabled.")

    # -- default role ---------------------------------------------------------

    @cattleguard.command(name="setdefaultrole")
    async def cg_set_default_role(self, ctx: commands.Context, role: discord.Role):
        """
        Set the role to auto-assign to members with no roles.

        Example: `[p]cg setdefaultrole Newcomer`
        """
        await self.config.guild(ctx.guild).default_role.set(role.id)
        await ctx.send(f"✅ Default role set to **{role.name}**.")

    @cattleguard.command(name="cleardefaultrole")
    async def cg_clear_default_role(self, ctx: commands.Context):
        """Remove the default role setting."""
        await self.config.guild(ctx.guild).default_role.set(None)
        await ctx.send("✅ Default role cleared.")

    # -- inactivity rules -----------------------------------------------------

    @cattleguard.command(name="addrule")
    async def cg_add_rule(
        self,
        ctx: commands.Context,
        from_role: discord.Role,
        days: int,
        to_role: Optional[discord.Role] = None,
    ):
        """
        Add an inactivity rule.

        If a member with `from_role` hasn't been seen in `days` days,
        they lose `from_role` and gain `to_role` (optional — omit to just remove).

        Examples:
          `[p]cg addrule Member 180 Lurker`
          `[p]cg addrule ActiveMember 30 Member`
          `[p]cg addrule Subscriber 90`
        """
        if days <= 0:
            return await ctx.send("❌ Days must be a positive integer.")

        # Prevent a role from being both from_role and to_role in the same rule
        if to_role and to_role.id == from_role.id:
            return await ctx.send("❌ `from_role` and `to_role` cannot be the same role.")

        rules = await self.config.guild(ctx.guild).inactivity_rules()
        for rule in rules:
            if rule["from_role"] == from_role.id:
                return await ctx.send(
                    f"❌ A rule for **{from_role.name}** already exists. "
                    f"Remove it first with `{ctx.prefix}cg removerule {from_role.name}`."
                )

        rules.append({
            "from_role": from_role.id,
            "to_role": to_role.id if to_role else None,
            "days": days,
        })
        await self.config.guild(ctx.guild).inactivity_rules.set(rules)

        to_str = f"→ **{to_role.name}**" if to_role else "(role removed, no replacement)"
        await ctx.send(f"✅ Rule added: **{from_role.name}** inactive for **{days}d** {to_str}.")

    @cattleguard.command(name="removerule")
    async def cg_remove_rule(self, ctx: commands.Context, from_role: discord.Role):
        """
        Remove the inactivity rule triggered by `from_role`.

        Example: `[p]cg removerule Member`
        """
        rules = await self.config.guild(ctx.guild).inactivity_rules()
        new_rules = [r for r in rules if r["from_role"] != from_role.id]
        if len(new_rules) == len(rules):
            return await ctx.send(f"❌ No rule found for **{from_role.name}**.")
        await self.config.guild(ctx.guild).inactivity_rules.set(new_rules)
        await ctx.send(f"✅ Rule for **{from_role.name}** removed.")

    @cattleguard.command(name="clearrules")
    async def cg_clear_rules(self, ctx: commands.Context):
        """Remove ALL inactivity rules for this server."""
        await self.config.guild(ctx.guild).inactivity_rules.set([])
        await ctx.send("✅ All inactivity rules cleared.")

    # -- check interval -------------------------------------------------------

    @cattleguard.command(name="setinterval")
    async def cg_set_interval(self, ctx: commands.Context, hours: int):
        """
        Set how often (in hours) the inactivity check runs. Default: 24.

        Since last-seen is tracked in real time, there's rarely a reason
        to run this more often than once a day.

        Example: `[p]cg setinterval 24`
        """
        if hours < 1:
            return await ctx.send("❌ Interval must be at least 1 hour.")
        await self.config.guild(ctx.guild).check_interval_hours.set(hours)
        await ctx.send(f"✅ Check interval set to **{hours}h**.")

    # -- log channel ----------------------------------------------------------

    @cattleguard.command(name="setlogchannel")
    async def cg_set_log_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Set a channel to receive CattleGuard action logs.

        Example: `[p]cg setlogchannel #mod-logs`
        """
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await ctx.send(f"✅ Log channel set to {channel.mention}.")

    @cattleguard.command(name="clearlogchannel")
    async def cg_clear_log_channel(self, ctx: commands.Context):
        """Remove the log channel setting."""
        await self.config.guild(ctx.guild).log_channel.set(None)
        await ctx.send("✅ Log channel cleared.")

    # -- manual run -----------------------------------------------------------

    @cattleguard.command(name="runnow")
    async def cg_run_now(self, ctx: commands.Context):
        """Manually trigger a full CattleGuard check right now."""
        await ctx.send("⏳ Running CattleGuard checks now...")
        await self._ensure_bootstrap(ctx.guild)
        await self._assign_default_roles(ctx.guild)
        await self._apply_inactivity_rules(ctx.guild)
        await ctx.send("✅ Done.")

    # -- inspect a member -----------------------------------------------------

    @cattleguard.command(name="check")
    async def cg_check(self, ctx: commands.Context, member: discord.Member):
        """
        Show the last-seen timestamp and inactivity status for a member.

        Example: `[p]cg check @SomeUser`
        """
        last_seen = await self._get_last_seen(ctx.guild, member)
        now = datetime.now(timezone.utc)

        if last_seen is None:
            await ctx.send(f"⚠️ No last-seen data for {member.mention} yet.")
            return

        # Clamp to 0 — last_seen should never be in the future but guard against
        # clock skew or a manually set bootstrap date
        inactive_days = max(0, (now - last_seen).days)
        ts_str = discord.utils.format_dt(last_seen, style="F")

        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Last Seen", value=ts_str, inline=False)
        embed.add_field(name="Inactive For", value=f"{inactive_days} day(s)", inline=True)

        rules = await self.config.guild(ctx.guild).inactivity_rules()
        member_role_ids = {r.id for r in member.roles}
        applicable = []
        for rule in rules:
            if rule["from_role"] in member_role_ids:
                fr = ctx.guild.get_role(rule["from_role"])
                tr = ctx.guild.get_role(rule["to_role"]) if rule.get("to_role") else None
                days_left = rule["days"] - inactive_days
                status = "🔴 Would trigger NOW" if days_left <= 0 else f"🟡 Triggers in {days_left}d"
                applicable.append(
                    f"**{fr.name if fr else '?'}** → **{tr.name if tr else 'removed'}** "
                    f"@ {rule['days']}d — {status}"
                )

        embed.add_field(
            name="Applicable Rules",
            value="\n".join(applicable) if applicable else "*None*",
            inline=False,
        )

        await ctx.send(embed=embed)

    # -- settings display -----------------------------------------------------

    @cattleguard.command(name="settings")
    async def cg_settings(self, ctx: commands.Context):
        """Display current CattleGuard settings for this server."""
        cfg = self.config.guild(ctx.guild)
        enabled = await cfg.enabled()
        default_role_id = await cfg.default_role()
        rules = await cfg.inactivity_rules()
        interval = await cfg.check_interval_hours()
        log_channel_id = await cfg.log_channel()
        bootstrap_date = await cfg.bootstrap_date()

        default_role = ctx.guild.get_role(default_role_id) if default_role_id else None
        log_channel = ctx.guild.get_channel(log_channel_id) if log_channel_id else None

        embed = discord.Embed(title="🐄 CattleGuard Settings", color=discord.Color.orange())
        embed.add_field(name="Status", value="✅ Enabled" if enabled else "⛔ Disabled", inline=True)
        embed.add_field(name="Check Interval", value=f"{interval}h", inline=True)
        embed.add_field(
            name="Default Role",
            value=default_role.mention if default_role else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "*Not set*",
            inline=True,
        )
        if bootstrap_date:
            dt = datetime.fromisoformat(bootstrap_date)
            embed.add_field(
                name="Bootstrap Date",
                value=discord.utils.format_dt(dt, style="D"),
                inline=True,
            )

        if rules:
            rule_lines = []
            for rule in rules:
                fr = ctx.guild.get_role(rule["from_role"])
                tr = ctx.guild.get_role(rule["to_role"]) if rule.get("to_role") else None
                fr_name = fr.name if fr else f"<deleted:{rule['from_role']}>"
                tr_name = (
                    tr.name if tr
                    else ("*remove only*" if not rule.get("to_role") else f"<deleted:{rule['to_role']}>")
                )
                rule_lines.append(f"**{fr_name}** → **{tr_name}** after `{rule['days']}d` inactive")
            embed.add_field(name="Inactivity Rules", value="\n".join(rule_lines), inline=False)
        else:
            embed.add_field(name="Inactivity Rules", value="*None configured*", inline=False)

        await ctx.send(embed=embed)
