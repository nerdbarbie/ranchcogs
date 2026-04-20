"""
username_guard.py — Discord Cog
Automatically kicks members who change their username or display name to
something matching a configurable set of regex patterns, protecting against
admin/staff impersonation.  Admins and configurable moderator roles are
always whitelisted.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pip install "discord.py>=2.3.0"

PRIVILEGED INTENTS (must be enabled in the Discord Developer Portal):
    • Server Members Intent  →  required for on_member_update / on_user_update

    In your bot setup:
        intents = discord.Intents.default()
        intents.members = True          # <-- required
        bot = commands.Bot(..., intents=intents)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOADING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    await bot.load_extension("cogs.username_guard.username_guard")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PERSISTENCE NOTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Runtime changes made via slash commands (patterns, whitelisted roles,
    log channel) are held in memory only and will reset when the bot
    restarts.  Edit DEFAULT_BLOCKED_PATTERNS and DEFAULT_WHITELISTED_ROLE_NAMES
    below to make changes permanent.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard-coded defaults  (edit these to persist changes across restarts)
# ---------------------------------------------------------------------------

DEFAULT_BLOCKED_PATTERNS: list[str] = [
    r"\badmin\b",
    r"\bmod(erator)?\b",
    r"\bowner\b",
    r"\bstaff\b",
    r"\bsupport\b",
    r"\[admin\]",
    r"\[mod\]",
    r"\[staff\]",
    r"\bserver\s*admin\b",
    r"\bhead\s*mod\b",
]

DEFAULT_WHITELISTED_ROLE_NAMES: list[str] = [
    "Moderator",
    "Head Moderator",
    "Senior Mod",
    "Trial Moderator",
]

# Maximum characters allowed in a list_patterns / list_roles response before
# the output is truncated, keeping us safely under Discord's 2 000-char limit.
_LIST_TRUNCATE_AT = 1800

# Discord's hard limit for audit-log kick reasons.
_MAX_REASON_LEN = 512

# Maximum length accepted for a user-supplied regex pattern string.
# Keeps kick reasons well within _MAX_REASON_LEN even with a long matched name.
_MAX_PATTERN_LEN = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile_patterns(raw: list[str]) -> list[re.Pattern[str]]:
    """Compile pattern strings; skip and log any that are invalid regex."""
    compiled: list[re.Pattern[str]] = []
    for p in raw:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            log.warning("Invalid regex pattern %r skipped: %s", p, exc)
    return compiled


def _matches_any(text: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    """Return the pattern string of the first match, or None."""
    for pat in patterns:
        if pat.search(text):
            return pat.pattern
    return None


def _truncated_list(lines: list[str], limit: int = _LIST_TRUNCATE_AT) -> str:
    """
    Join *lines* with newlines.  If the result would exceed *limit* chars,
    truncate and append a '… (N more)' notice so we never exceed Discord's
    2 000-character message cap.
    """
    out: list[str] = []
    total = 0
    for i, line in enumerate(lines):
        # +1 for the newline separator
        if total + len(line) + 1 > limit:
            remaining = len(lines) - i
            out.append(f"… *({remaining} more — too many to display)*")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class UsernameGuard(commands.Cog):
    """
    Watches for username / display-name changes and kicks members whose new
    name matches a blocked regex pattern, unless they hold a whitelisted role
    or have the Administrator permission.

    Listens to BOTH on_member_update (nickname / guild-profile changes) and
    on_user_update (global username changes) to cover all name-change vectors.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Mutable runtime state — modified via slash commands.
        self._blocked_patterns: list[str] = list(DEFAULT_BLOCKED_PATTERNS)

        # Stored lowercase for fast case-insensitive membership tests.
        self._whitelisted_role_names: list[str] = [
            r.lower() for r in DEFAULT_WHITELISTED_ROLE_NAMES
        ]
        # Original casing preserved purely for human-readable /list_roles output.
        self._whitelisted_role_names_display: list[str] = list(DEFAULT_WHITELISTED_ROLE_NAMES)

        # Pre-compiled cache; rebuilt whenever _blocked_patterns changes.
        self._compiled: list[re.Pattern[str]] = _compile_patterns(
            self._blocked_patterns
        )

        # Optional audit-log channel (set via /usernameguard set_log_channel).
        self._log_channel_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_compiled(self) -> None:
        self._compiled = _compile_patterns(self._blocked_patterns)

    def _is_whitelisted(self, member: discord.Member) -> bool:
        """Return True if this member must never be kicked by this cog."""
        if member.bot:
            return True
        if member.guild.owner_id == member.id:
            return True
        if member.guild_permissions.administrator:
            return True
        member_role_names = {r.name.lower() for r in member.roles}
        return bool(member_role_names & set(self._whitelisted_role_names))

    async def _log_action(
        self,
        guild: discord.Guild,
        member: discord.Member,
        new_name: str,
        matched_pattern: str,
        action: str,
    ) -> None:
        """Send an audit embed to the configured log channel, if any."""
        if self._log_channel_id is None:
            return
        channel = guild.get_channel_or_thread(self._log_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = discord.Embed(
            title="🛡️ Username Guard — Action Taken",
            colour=discord.Colour.red(),
        )
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(
            name="Offending name",
            value=discord.utils.escape_markdown(new_name),
            inline=False,
        )
        embed.add_field(name="Matched pattern", value=f"`{matched_pattern}`", inline=False)
        embed.add_field(name="Action", value=action, inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send log message: %s", exc)

    async def _enforce(
        self, member: discord.Member, names_changed: list[str]
    ) -> None:
        """
        Core enforcement logic.  Given a list of *new* name strings that
        changed for *member*, kick them if any name matches a blocked pattern
        (unless they are whitelisted).
        """
        if self._is_whitelisted(member):
            return

        # Deduplicate while preserving order (e.g. name == display_name when
        # no nickname/global_name is set — both branches would add the same str).
        seen_names = list(dict.fromkeys(names_changed))

        for name in seen_names:
            matched = _matches_any(name, self._compiled)
            if matched is None:
                continue

            reason = (
                f"Username Guard: name {name!r} matched blocked pattern '{matched}'."
            )
            # Discord hard-caps audit-log reasons at 512 characters.
            if len(reason) > _MAX_REASON_LEN:
                reason = reason[:_MAX_REASON_LEN - 1] + "…"
            try:
                # Best-effort DM before kicking.
                try:
                    await member.send(
                        f"You have been removed from **{member.guild.name}** because "
                        f"your username or display name "
                        f"(`{discord.utils.escape_markdown(name)}`) is not permitted "
                        f"on this server.\n\n"
                        f"If you believe this is a mistake, please contact the server staff."
                    )
                except discord.HTTPException:
                    pass  # Closed DMs — that is fine, kick anyway.

                await member.kick(reason=reason)
                log.info(
                    "Kicked %s (%s) from %s — name %r matched pattern %r",
                    member,
                    member.id,
                    member.guild.name,
                    name,
                    matched,
                )
                await self._log_action(member.guild, member, name, matched, "Kicked")

            except discord.Forbidden:
                log.warning(
                    "Insufficient permissions to kick %s (%s) in %s.",
                    member,
                    member.id,
                    member.guild.name,
                )
                await self._log_action(
                    member.guild, member, name, matched,
                    "⚠️ Kick FAILED — missing permissions",
                )
            except discord.HTTPException as exc:
                log.error("HTTPException while kicking %s: %s", member, exc)

            # Only need to act once per update.
            break

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """
        Covers guild-level profile changes: server nickname and, on some
        gateway versions, the guild display name.
        """
        changed: list[str] = []

        if before.nick != after.nick and after.nick:
            changed.append(after.nick)

        # display_name = nick ?? global_name ?? name
        # Catches any residual display_name shift not covered by nick alone.
        if before.display_name != after.display_name:
            changed.append(after.display_name)

        if changed:
            await self._enforce(after, changed)

    @commands.Cog.listener()
    async def on_user_update(
        self, before: discord.User, after: discord.User
    ) -> None:
        """
        Covers *global* username changes (discord.User.name and global_name).
        These fire on on_user_update, NOT on_member_update — a separate
        listener is required to catch this attack vector.

        We resolve the User to a Member in every mutual guild the bot can see
        and enforce independently on each.
        """
        changed: list[str] = []

        if before.name != after.name:
            changed.append(after.name)

        before_gn = getattr(before, "global_name", None)
        after_gn = getattr(after, "global_name", None)
        if before_gn != after_gn and after_gn:
            changed.append(after_gn)

        if not changed:
            return

        # Enforce in every guild this user shares with the bot.
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None:
                await self._enforce(member, changed)

    # ------------------------------------------------------------------
    # Slash commands  (Administrator permission required)
    # ------------------------------------------------------------------

    guard_group = app_commands.Group(
        name="usernameguard",
        description="Manage the Username Guard cog.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ── Pattern management ──────────────────────────────────────────────

    @guard_group.command(name="list_patterns", description="List all blocked regex patterns.")
    async def list_patterns(self, interaction: discord.Interaction) -> None:
        if not self._blocked_patterns:
            await interaction.response.send_message(
                "No blocked patterns configured.", ephemeral=True
            )
            return
        lines = [f"`{p}`" for p in self._blocked_patterns]
        body = _truncated_list(lines)
        await interaction.response.send_message(
            f"**Blocked patterns ({len(self._blocked_patterns)}):**\n{body}",
            ephemeral=True,
        )

    @guard_group.command(name="add_pattern", description="Add a new blocked regex pattern.")
    @app_commands.describe(pattern="The regex pattern to block (case-insensitive).")
    async def add_pattern(self, interaction: discord.Interaction, pattern: str) -> None:
        if len(pattern) > _MAX_PATTERN_LEN:
            await interaction.response.send_message(
                f"❌ Pattern too long ({len(pattern)} chars). Maximum is {_MAX_PATTERN_LEN}.",
                ephemeral=True,
            )
            return
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            await interaction.response.send_message(
                f"❌ Invalid regex: `{exc}`", ephemeral=True
            )
            return
        if pattern in self._blocked_patterns:
            await interaction.response.send_message(
                "Pattern already exists.", ephemeral=True
            )
            return
        self._blocked_patterns.append(pattern)
        self._rebuild_compiled()
        await interaction.response.send_message(
            f"✅ Pattern `{pattern}` added.", ephemeral=True
        )

    @guard_group.command(name="remove_pattern", description="Remove a blocked regex pattern.")
    @app_commands.describe(pattern="The exact pattern string to remove.")
    async def remove_pattern(self, interaction: discord.Interaction, pattern: str) -> None:
        if pattern not in self._blocked_patterns:
            await interaction.response.send_message("Pattern not found.", ephemeral=True)
            return
        self._blocked_patterns.remove(pattern)
        self._rebuild_compiled()
        await interaction.response.send_message(
            f"✅ Pattern `{pattern}` removed.", ephemeral=True
        )

    @guard_group.command(
        name="test_name", description="Test whether a name would be blocked."
    )
    @app_commands.describe(name="The username / display name to test.")
    async def test_name(self, interaction: discord.Interaction, name: str) -> None:
        matched = _matches_any(name, self._compiled)
        safe_name = discord.utils.escape_markdown(name)
        if matched:
            await interaction.response.send_message(
                f"🚫 `{safe_name}` **would be blocked** — matched pattern `{matched}`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ `{safe_name}` would **not** be blocked.",
                ephemeral=True,
            )

    # ── Whitelist management ────────────────────────────────────────────

    @guard_group.command(name="list_roles", description="List whitelisted role names.")
    async def list_roles(self, interaction: discord.Interaction) -> None:
        if not self._whitelisted_role_names_display:
            await interaction.response.send_message(
                "No whitelisted roles configured.", ephemeral=True
            )
            return
        lines = [f"• {r}" for r in self._whitelisted_role_names_display]
        body = _truncated_list(lines)
        await interaction.response.send_message(
            f"**Whitelisted roles ({len(self._whitelisted_role_names_display)}):**\n{body}",
            ephemeral=True,
        )

    @guard_group.command(name="add_role", description="Whitelist a role.")
    @app_commands.describe(role="The role to whitelist.")
    async def add_role(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        role_lower = role.name.lower()
        if role_lower in self._whitelisted_role_names:
            await interaction.response.send_message(
                "Role already whitelisted.", ephemeral=True
            )
            return
        self._whitelisted_role_names.append(role_lower)
        self._whitelisted_role_names_display.append(role.name)
        await interaction.response.send_message(
            f"✅ Role **{role.name}** whitelisted.", ephemeral=True
        )

    @guard_group.command(name="remove_role", description="Remove a role from the whitelist.")
    @app_commands.describe(role="The role to remove from the whitelist.")
    async def remove_role(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        role_lower = role.name.lower()
        if role_lower not in self._whitelisted_role_names:
            await interaction.response.send_message(
                "Role not in whitelist.", ephemeral=True
            )
            return
        self._whitelisted_role_names.remove(role_lower)
        # Remove by lowercase comparison so display list stays in sync.
        self._whitelisted_role_names_display = [
            n for n in self._whitelisted_role_names_display if n.lower() != role_lower
        ]
        await interaction.response.send_message(
            f"✅ Role **{role.name}** removed from whitelist.", ephemeral=True
        )

    # ── Log channel ─────────────────────────────────────────────────────

    @guard_group.command(
        name="set_log_channel", description="Set the channel for audit log messages."
    )
    @app_commands.describe(channel="The text channel to send log messages to.")
    async def set_log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        self._log_channel_id = channel.id
        await interaction.response.send_message(
            f"✅ Log channel set to {channel.mention}.", ephemeral=True
        )

    @guard_group.command(
        name="clear_log_channel", description="Stop sending audit log messages."
    )
    async def clear_log_channel(self, interaction: discord.Interaction) -> None:
        self._log_channel_id = None
        await interaction.response.send_message("✅ Log channel cleared.", ephemeral=True)

    # ── Status ───────────────────────────────────────────────────────────

    @guard_group.command(name="status", description="Show the current configuration.")
    async def status(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🛡️ Username Guard — Status",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Blocked patterns",
            value=str(len(self._blocked_patterns)),
            inline=True,
        )
        embed.add_field(
            name="Whitelisted roles",
            value=str(len(self._whitelisted_role_names)),
            inline=True,
        )
        log_ch = f"<#{self._log_channel_id}>" if self._log_channel_id else "Not set"
        embed.add_field(name="Log channel", value=log_ch, inline=True)
        embed.set_footer(text="Admins and guild owner are always whitelisted.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Setup hook
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UsernameGuard(bot))
