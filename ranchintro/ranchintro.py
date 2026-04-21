"""
RanchIntro - Introduction channel welcome cog for Red-DiscordBot
Watches a designated intro channel, welcomes new members to a public channel,
and promotes them to an active member role on their first post.
"""

import asyncio
import logging
from typing import Optional

import discord
from redbot.core import checks, commands, Config
from redbot.core.bot import Red

log = logging.getLogger("red.ranchcogs.ranchintro")


class RanchIntro(commands.Cog):
    """
    Watches an introductions channel for a new member's first message,
    promotes them to an active role, removes their new member role,
    and reposts a welcome embed in a public channel.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x72616e6368, force_registration=True)

        guild_defaults = {
            "watch_channel": None,    # Channel ID to watch for intros
            "welcome_channel": None,  # Channel ID to post the welcome embed
            "new_member_role": None,  # Role ID members have BEFORE posting (will be removed)
            "active_role": None,      # Role ID assigned on first post (also used as "already posted" check)
            "log_channel": None,      # Optional channel ID for error logging
            "enabled": True,
        }

        self.config.register_guild(**guild_defaults)

    # -------------------------------------------------------------------------
    # Event listener
    # -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        guild = message.guild
        member = message.author

        if not await self.config.guild(guild).enabled():
            return

        # Only care about messages in the watch channel
        watch_channel_id = await self.config.guild(guild).watch_channel()
        if not watch_channel_id or message.channel.id != watch_channel_id:
            return

        active_role_id = await self.config.guild(guild).active_role()
        if not active_role_id:
            return

        active_role = guild.get_role(active_role_id)
        if not active_role:
            return

        # If they already have the active role, they've already introduced themselves
        if active_role in member.roles:
            return

        # --- First post detected, run the intro flow ---
        await self._handle_intro(guild, member, message, active_role)

    # -------------------------------------------------------------------------
    # Intro flow
    # -------------------------------------------------------------------------

    async def _handle_intro(
        self,
        guild: discord.Guild,
        member: discord.Member,
        message: discord.Message,
        active_role: discord.Role,
    ):
        cfg = self.config.guild(guild)
        new_member_role_id = await cfg.new_member_role()
        welcome_channel_id = await cfg.welcome_channel()

        new_member_role = guild.get_role(new_member_role_id) if new_member_role_id else None
        welcome_channel = guild.get_channel(welcome_channel_id) if welcome_channel_id else None

        # --- Role changes ---
        role_success = True
        try:
            if new_member_role and new_member_role in member.roles:
                await member.remove_roles(new_member_role, reason="RanchIntro: member posted introduction")
            await member.add_roles(active_role, reason="RanchIntro: member posted introduction")
        except discord.Forbidden:
            role_success = False
            await self._log(
                guild,
                f"⚠️ Missing permissions to update roles for {member.mention}. "
                f"Welcome will still be posted.",
            )
        except discord.HTTPException as e:
            role_success = False
            await self._log(
                guild,
                f"⚠️ HTTP error updating roles for {member.mention}: {e}. "
                f"Welcome will still be posted.",
            )

        if not role_success:
            log.warning(
                "Role update failed for %s (%s) in %s — proceeding with welcome post",
                member.display_name, member.id, guild.name,
            )

        # --- Welcome embed ---
        if not welcome_channel:
            await self._log(guild, "⚠️ No welcome channel set — cannot post intro for "
                                   f"{member.mention}.")
            return

        embed = discord.Embed(
            description=message.content if message.content else "*No message content.*",
            color=discord.Color.orange(),
        )
        embed.set_author(
            name=member.display_name,
            icon_url=member.display_avatar.url,
        )
        embed.set_footer(text=f"@{member.name}")

        try:
            await welcome_channel.send(
                content=f"🤠 Hey y'all! {member.mention} is here!",
                embed=embed,
            )
        except discord.Forbidden:
            await self._log(guild, f"⚠️ Missing permissions to post in {welcome_channel.mention}.")
        except discord.HTTPException as e:
            await self._log(guild, f"⚠️ HTTP error posting welcome for {member.mention}: {e}")

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
            await channel.send(f"[RanchIntro] {message}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    @commands.group(name="ranchintro", aliases=["ri"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_roles=True)
    async def ranchintro(self, ctx: commands.Context):
        """RanchIntro introduction channel settings."""

    @ranchintro.command(name="enable")
    async def ri_enable(self, ctx: commands.Context):
        """Enable RanchIntro for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ RanchIntro enabled.")

    @ranchintro.command(name="disable")
    async def ri_disable(self, ctx: commands.Context):
        """Disable RanchIntro for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("⛔ RanchIntro disabled.")

    @ranchintro.command(name="setwatchchannel")
    async def ri_set_watch_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Set the introductions channel to watch for first messages.

        Example: `[p]ri setwatchchannel #introductions`
        """
        await self.config.guild(ctx.guild).watch_channel.set(channel.id)
        await ctx.send(f"✅ Watch channel set to {channel.mention}.")

    @ranchintro.command(name="setpostchannel")
    async def ri_set_welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Set the public channel where welcome embeds are posted.

        Example: `[p]ri setpostchannel #general`
        """
        await self.config.guild(ctx.guild).welcome_channel.set(channel.id)
        await ctx.send(f"✅ Welcome channel set to {channel.mention}.")

    @ranchintro.command(name="setremoverole")
    async def ri_set_new_member_role(self, ctx: commands.Context, role: discord.Role):
        """
        Set the role new members have before introducing themselves (will be removed on intro).

        Example: `[p]ri setremoverole Newcomer`
        """
        await self.config.guild(ctx.guild).new_member_role.set(role.id)
        await ctx.send(f"✅ New member role set to **{role.name}**.")

    @ranchintro.command(name="setaddrole")
    async def ri_set_active_role(self, ctx: commands.Context, role: discord.Role):
        """
        Set the role assigned when a member posts their introduction.
        This role also acts as the \"already introduced\" check.

        Example: `[p]ri setaddrole Member`
        """
        await self.config.guild(ctx.guild).active_role.set(role.id)
        await ctx.send(f"✅ Active role set to **{role.name}**.")

    @ranchintro.command(name="setlogchannel")
    async def ri_set_log_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Set a channel to receive RanchIntro error logs.

        Example: `[p]ri setlogchannel #mod-logs`
        """
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await ctx.send(f"✅ Log channel set to {channel.mention}.")

    @ranchintro.command(name="clearlogchannel")
    async def ri_clear_log_channel(self, ctx: commands.Context):
        """Remove the log channel setting."""
        await self.config.guild(ctx.guild).log_channel.set(None)
        await ctx.send("✅ Log channel cleared.")

    @ranchintro.command(name="settings")
    async def ri_settings(self, ctx: commands.Context):
        """Display current RanchIntro settings for this server."""
        cfg = self.config.guild(ctx.guild)
        enabled = await cfg.enabled()
        watch_channel_id = await cfg.watch_channel()
        welcome_channel_id = await cfg.welcome_channel()
        new_member_role_id = await cfg.new_member_role()
        active_role_id = await cfg.active_role()
        log_channel_id = await cfg.log_channel()

        watch_channel = ctx.guild.get_channel(watch_channel_id) if watch_channel_id else None
        welcome_channel = ctx.guild.get_channel(welcome_channel_id) if welcome_channel_id else None
        new_member_role = ctx.guild.get_role(new_member_role_id) if new_member_role_id else None
        active_role = ctx.guild.get_role(active_role_id) if active_role_id else None
        log_channel = ctx.guild.get_channel(log_channel_id) if log_channel_id else None

        embed = discord.Embed(title="🤠 RanchIntro Settings", color=discord.Color.orange())
        embed.add_field(name="Status", value="✅ Enabled" if enabled else "⛔ Disabled", inline=True)
        embed.add_field(
            name="Watch Channel",
            value=watch_channel.mention if watch_channel else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Welcome Channel",
            value=welcome_channel.mention if welcome_channel else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="New Member Role",
            value=new_member_role.mention if new_member_role else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Active Role",
            value=active_role.mention if active_role else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Log Channel",
            value=log_channel.mention if log_channel else "*Not set*",
            inline=True,
        )

        await ctx.send(embed=embed)
