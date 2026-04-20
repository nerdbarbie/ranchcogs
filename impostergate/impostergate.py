"""
impostergate.py — Discord Cog
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
    await bot.load_extension("cogs.impostergate.impostergate")

    Or with Red's downloader:
        [p]cog install ranchcogs impostergate
        [p]load impostergate

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
from redbot.core import commands
from redbot.core.bot import Red

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
        # Charge for a newline separator only after the first line.
        separator = 1 if i > 0 else 0
        if total + separator + len(line) > limit:
            remaining = len(lines) - i
            out.append(f"… *({remaining} more — too many to display)*")
            break
        out.append(line)
        total += separator + len(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ImposterGate(commands.Cog):
    """
    Watches for username / display-name changes and kicks members whose new
    name matches a blocked regex pattern, unless they hold a whitelisted role
    or have the Administrator permission.

    Listens to BOTH on_member_update (nickname / guild-profile changes) and
    on_user_update (global username changes) to cover all name-change vectors.
    """

    def __init__(self, bot: Red) -> None:
        self.bot = bot

        # Mutable runtime state — modified via slash commands.
        self._blocked_patterns: list[str] = list(DEFAULT_BLOCKED_PATTERNS)

        # Stored lowercase in a set for O(1) case-insensitive membership tests.
        self._whitelisted_role_names: set[str] = {
            r.lower() for r in DEFAULT_WHITELISTED_ROLE_NAMES
        }
        # Original casing preserved purely for human-readable /list_roles output.
        self._whitelisted_role_names_display: list[str] = list(DEFAULT_WHITELISTED_ROLE_NAMES)

        # Pre-compiled cache; rebuilt whenever _blocked_patterns changes.
        self._compiled: list[re.Pattern[str]] = _compile_patterns(
            self._blocked_patterns
        )

        # Optional audit-log channel (set via /impostergate set_log_channel).
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
        return bool(member_role_names & self._whitelisted_role_names)

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
            title="🛡️ ImposterGate — Action Taken",
            colour=discord.Colour.red(),
        )
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(
            name="Offending name",
            value=discord.utils.escape_markdown(new_name),
            inline=False,
        )
        embed.add_field(name="Matched pattern", value=f"``{matched_pattern}``", inline=False)
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
                f"ImposterGate: name {name!r} matched blocked pattern '{matched}'."
            )
            # Discord hard-caps audit-log reasons at 512 characters.
            if len(reason) > _MAX_REASON_LEN:
                reason = reason[:_MAX_REASON_LEN - 1] + "…"
            try:
                # Best-effort DM before kicking.
                try:
                    await member.send(
                        f"You have been removed from **{discord.utils.escape_markdown(member.guild.name)}** because "
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
    # Prefix commands  (Red-style, admin only)
    # ------------------------------------------------------------------

    @commands.group(name="impostergate", invoke_without_command=True)
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def impostergate(self, ctx: commands.Context) -> None:
        """Manage the ImposterGate username-spoof protection cog."""
        await ctx.send_help(ctx.command)

    # ── Pattern management ──────────────────────────────────────────────

    @impostergate.command(name="listpatterns")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def list_patterns(self, ctx: commands.Context) -> None:
        """List all blocked regex patterns."""
        if not self._blocked_patterns:
            await ctx.send("No blocked patterns configured.")
            return
        lines = [f"``{p}``" for p in self._blocked_patterns]
        body = _truncated_list(lines)
        await ctx.send(f"**Blocked patterns ({len(self._blocked_patterns)}):**\n{body}")

    @impostergate.command(name="addpattern")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def add_pattern(self, ctx: commands.Context, *, pattern: str) -> None:
        """Add a new blocked regex pattern (case-insensitive).

        Example: `[p]impostergate addpattern \\bsupport\\b`
        """
        if len(pattern) > _MAX_PATTERN_LEN:
            await ctx.send(
                f"❌ Pattern too long ({len(pattern)} chars). Maximum is {_MAX_PATTERN_LEN}."
            )
            return
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            await ctx.send(f"❌ Invalid regex: `{exc}`")
            return
        if pattern in self._blocked_patterns:
            await ctx.send("Pattern already exists.")
            return
        self._blocked_patterns.append(pattern)
        self._rebuild_compiled()
        await ctx.send(f"✅ Pattern ``{pattern}`` added.")

    @impostergate.command(name="removepattern")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def remove_pattern(self, ctx: commands.Context, *, pattern: str) -> None:
        """Remove a blocked regex pattern.

        Example: `[p]impostergate removepattern \\bsupport\\b`
        """
        if pattern not in self._blocked_patterns:
            await ctx.send("Pattern not found.")
            return
        self._blocked_patterns.remove(pattern)
        self._rebuild_compiled()
        await ctx.send(f"✅ Pattern ``{pattern}`` removed.")

    @impostergate.command(name="testname")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def test_name(self, ctx: commands.Context, *, name: str) -> None:
        """Test whether a name would be blocked without taking any action.

        Example: `[p]impostergate testname Server Admin`
        """
        matched = _matches_any(name, self._compiled)
        safe_name = discord.utils.escape_markdown(name)
        if matched:
            await ctx.send(
                f"🚫 `{safe_name}` **would be blocked** — matched pattern ``{matched}``."
            )
        else:
            await ctx.send(f"✅ `{safe_name}` would **not** be blocked.")

    # ── Whitelist management ────────────────────────────────────────────

    @impostergate.command(name="listroles")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def list_roles(self, ctx: commands.Context) -> None:
        """List all whitelisted role names."""
        if not self._whitelisted_role_names_display:
            await ctx.send("No whitelisted roles configured.")
            return
        lines = [f"• {r}" for r in self._whitelisted_role_names_display]
        body = _truncated_list(lines)
        await ctx.send(
            f"**Whitelisted roles ({len(self._whitelisted_role_names_display)}):**\n{body}"
        )

    @impostergate.command(name="addrole")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def add_role(self, ctx: commands.Context, *, role: discord.Role) -> None:
        """Whitelist a role so its members are never kicked.

        Example: `[p]impostergate addrole Moderator`
        """
        role_lower = role.name.lower()
        if role_lower in self._whitelisted_role_names:
            await ctx.send("Role already whitelisted.")
            return
        self._whitelisted_role_names.add(role_lower)
        self._whitelisted_role_names_display.append(role.name)
        await ctx.send(f"✅ Role **{role.name}** whitelisted.")

    @impostergate.command(name="removerole")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def remove_role(self, ctx: commands.Context, *, role: discord.Role) -> None:
        """Remove a role from the whitelist.

        Example: `[p]impostergate removerole Moderator`
        """
        role_lower = role.name.lower()
        if role_lower not in self._whitelisted_role_names:
            await ctx.send("Role not in whitelist.")
            return
        self._whitelisted_role_names.discard(role_lower)
        self._whitelisted_role_names_display = [
            n for n in self._whitelisted_role_names_display if n.lower() != role_lower
        ]
        await ctx.send(f"✅ Role **{role.name}** removed from whitelist.")

    # ── Log channel ─────────────────────────────────────────────────────

    @impostergate.command(name="logchannel")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def set_log_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set a channel for audit log messages.

        Example: `[p]impostergate logchannel #mod-logs`
        """
        self._log_channel_id = channel.id
        await ctx.send(f"✅ Log channel set to {channel.mention}.")

    @impostergate.command(name="clearlogchannel")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def clear_log_channel(self, ctx: commands.Context) -> None:
        """Stop sending audit log messages."""
        self._log_channel_id = None
        await ctx.send("✅ Log channel cleared.")

    # ── Status ───────────────────────────────────────────────────────────

    @impostergate.command(name="status")
    @commands.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def status(self, ctx: commands.Context) -> None:
        """Show the current ImposterGate configuration."""
        embed = discord.Embed(
            title="🛡️ ImposterGate — Status",
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
        await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Setup hook
# ---------------------------------------------------------------------------

async def setup(bot: Red) -> None:
    await bot.add_cog(ImposterGate(bot))
