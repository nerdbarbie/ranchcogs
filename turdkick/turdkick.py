import discord
from datetime import datetime, timezone
from typing import Optional

from redbot.core import commands, Config
from redbot.core.bot import Red

MAX_REASON_LEN = 480  # Discord audit log hard limit is 512; leave headroom for "[TurdKick] " prefix


class TurdKick(commands.Cog):
    """
    Automatically kicks members who receive a designated trap role.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x545552444B49434B, force_registration=True
        )
        default_guild = {
            "trap_role_id": None,
            "enabled": True,
            "kick_reason": "You have been automatically removed from this server.",
            "log_channel_id": None,
        }
        self.config.register_guild(**default_guild)

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    @commands.group(name="turdkick", invoke_without_command=True)
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def turdkick(self, ctx: commands.Context):
        """Manage the TurdKick trap-role auto-kick system."""
        await ctx.send_help(ctx.command)

    @turdkick.command(name="role")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def set_role(self, ctx: commands.Context, role: discord.Role):
        """Set the trap role that triggers an automatic kick.

        Example: `[p]turdkick role @TrapRole`
        """
        # Block @everyone — would fire on every member update in the server
        if role.id == ctx.guild.id:
            await ctx.send("\u274c You cannot set \`@everyone\` as the trap role.")
            return

        # Block managed roles (bot/integration roles can't be manually assigned anyway)
        if role.managed:
            await ctx.send(
                "\u274c That is a managed role (assigned by a bot or integration) "
                "and cannot be used as a trap role."
            )
            return

        # Prevent setting a role the bot already has
        if role in ctx.guild.me.roles:
            await ctx.send(
                "\u274c That role is currently assigned to the bot. "
                "Choose a different role to avoid the bot kicking itself."
            )
            return

        await self.config.guild(ctx.guild).trap_role_id.set(role.id)
        await ctx.send(
            f"\u2705 Trap role set to **{role.name}** (`{role.id}`). "
            f"Any member who receives this role will be automatically kicked."
        )

    @turdkick.command(name="clearrole")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def clear_role(self, ctx: commands.Context):
        """Remove the currently configured trap role (disables auto-kick)."""
        await self.config.guild(ctx.guild).trap_role_id.set(None)
        await ctx.send("\u2705 Trap role cleared. Auto-kick is now disabled.")

    @turdkick.command(name="enable")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def enable(self, ctx: commands.Context):
        """Enable the TurdKick auto-kick system."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("\u2705 TurdKick auto-kick **enabled**.")

    @turdkick.command(name="disable")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def disable(self, ctx: commands.Context):
        """Disable the TurdKick auto-kick system without clearing the role."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("\u23f8\ufe0f TurdKick auto-kick **disabled**.")

    @turdkick.command(name="reason")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def set_reason(self, ctx: commands.Context, *, reason: str):
        """Set the kick reason shown in the audit log and DM.

        Example: `[p]turdkick reason You've been removed for violating server rules.`
        """
        if len(reason) > MAX_REASON_LEN:
            await ctx.send(
                f"\u274c Reason is too long ({len(reason)} chars). "
                f"Please keep it under {MAX_REASON_LEN} characters."
            )
            return
        await self.config.guild(ctx.guild).kick_reason.set(reason)
        await ctx.send(f"\u2705 Kick reason updated to:\n> {reason}")

    @turdkick.command(name="logchannel")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def set_log_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ):
        """Set a channel to log auto-kick events. Leave blank to disable logging.

        Example: `[p]turdkick logchannel #mod-logs`
        """
        if channel:
            await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
            await ctx.send(f"\u2705 Log channel set to {channel.mention}.")
        else:
            await self.config.guild(ctx.guild).log_channel_id.set(None)
            await ctx.send(
                "\u2705 Log channel cleared. Kick events will no longer be logged."
            )

    @turdkick.command(name="status")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        """Show the current TurdKick configuration for this server."""
        guild_config = await self.config.guild(ctx.guild).all()
        trap_role_id = guild_config["trap_role_id"]
        enabled = guild_config["enabled"]
        reason = guild_config["kick_reason"]
        log_channel_id = guild_config["log_channel_id"]

        trap_role = ctx.guild.get_role(trap_role_id) if trap_role_id else None
        log_channel = ctx.guild.get_channel(log_channel_id) if log_channel_id else None

        embed = discord.Embed(
            title="\U0001f4a9 TurdKick Status", color=discord.Color.orange()
        )
        embed.add_field(
            name="Enabled",
            value="\u2705 Yes" if enabled else "\u274c No",
            inline=True,
        )
        embed.add_field(
            name="Trap Role",
            value=trap_role.mention if trap_role else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "*Not set*",
            inline=True,
        )
        embed.add_field(name="Kick Reason", value=reason, inline=False)
        await ctx.send(embed=embed)

    # -------------------------------------------------------------------------
    # Listener
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Watch for the trap role being added and kick the member."""
        # Don't act during startup before the bot is fully ready
        if not self.bot.is_ready():
            return

        guild = after.guild

        # Guard: bot's own Member object must be cached and available
        bot_member = guild.me
        if bot_member is None:
            return

        # Bail early if the role list didn't change
        if before.roles == after.roles:
            return

        guild_config = await self.config.guild(guild).all()

        if not guild_config["enabled"]:
            return

        trap_role_id = guild_config["trap_role_id"]
        if not trap_role_id:
            return

        # Check whether the trap role was just added in this update
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if trap_role_id not in (after_ids - before_ids):
            return

        # Never try to kick the bot itself
        if after.id == bot_member.id:
            return

        # Confirm the bot has the Kick Members permission
        if not bot_member.guild_permissions.kick_members:
            return

        # Don't kick members whose top role is >= the bot's top role (un-kickable)
        if after.top_role >= bot_member.top_role:
            return

        # Never kick server administrators
        if after.guild_permissions.administrator:
            return

        reason = guild_config["kick_reason"]

        # Attempt to DM the member before kicking
        try:
            await after.send(
                f"You have been removed from **{guild.name}**.\nReason: {reason}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs disabled or blocked — proceed with kick anyway

        # Perform the kick
        kick_succeeded = False
        kick_failed_reason = "Unknown error."
        try:
            await after.kick(reason=f"[TurdKick] {reason}"[:512])
            kick_succeeded = True
        except discord.Forbidden:
            kick_failed_reason = "Missing permissions to kick this member."
        except discord.HTTPException as e:
            kick_failed_reason = f"HTTP error: {e}"

        # Log the result if a log channel is configured
        log_channel_id = guild_config["log_channel_id"]
        if not log_channel_id:
            return

        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            return

        trap_role = guild.get_role(trap_role_id)
        now = datetime.now(timezone.utc)

        if kick_succeeded:
            embed = discord.Embed(
                title="\U0001f4a9 TurdKick — Member Auto-Kicked",
                color=discord.Color.red(),
                timestamp=now,
            )
            embed.add_field(
                name="Member", value=f"{after} (`{after.id}`)", inline=False
            )
            embed.add_field(
                name="Trap Role",
                value=trap_role.name if trap_role else str(trap_role_id),
                inline=True,
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            try:
                embed.set_thumbnail(url=after.display_avatar.url)
            except Exception:
                pass  # Avatar URL unavailable on stale member object; non-critical
        else:
            embed = discord.Embed(
                title="\u26a0\ufe0f TurdKick — Kick FAILED",
                color=discord.Color.yellow(),
                timestamp=now,
            )
            embed.add_field(
                name="Member", value=f"{after} (`{after.id}`)", inline=False
            )
            embed.add_field(
                name="Trap Role",
                value=trap_role.name if trap_role else str(trap_role_id),
                inline=True,
            )
            embed.add_field(name="Error", value=kick_failed_reason, inline=False)

        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass
