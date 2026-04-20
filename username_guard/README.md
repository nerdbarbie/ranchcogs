# username_guard

A Red-Bot / discord.py cog that automatically kicks members who change their
username or display name to something matching a configurable set of regex
patterns — protecting your server against admin/staff impersonation.

---

## Features

- Watches **both** global username changes (`on_user_update`) **and** guild
  display-name / nickname changes (`on_member_update`) — all name-change
  vectors are covered.
- **Whitelist** — admins (Administrator permission), the guild owner, and any
  nominated moderator roles are never kicked.
- Sends the kicked member a DM explaining why before removing them.
- Optional **audit log channel** for a per-action embed trail.
- Full **slash-command management** at runtime (`/usernameguard …`).
- Validates regex patterns before accepting them to prevent bad input.

---

## Requirements

```
discord.py >= 2.3.0
```

### Privileged intents

The **Server Members Intent** must be enabled in the
[Discord Developer Portal](https://discord.com/developers/applications) for
your bot application, and your bot must request it at startup:

```python
intents = discord.Intents.default()
intents.members = True          # required
bot = commands.Bot(command_prefix="!", intents=intents)
```

### Bot permissions

Your bot needs the **Kick Members** permission, and its role must be
**above** the roles of any members it may need to kick in the server's
role hierarchy.

---

## Installation

Drop the `username_guard/` folder into your `cogs/` directory, then load it:

```python
await bot.load_extension("cogs.username_guard.username_guard")
```

---

## Configuration

Edit the constants near the top of `username_guard.py` to set permanent
defaults (these survive bot restarts):

```python
DEFAULT_BLOCKED_PATTERNS: list[str] = [
    r"\badmin\b",
    r"\bmod(erator)?\b",
    r"\bowner\b",
    # … add your own patterns here
]

DEFAULT_WHITELISTED_ROLE_NAMES: list[str] = [
    "Moderator",
    "Head Moderator",
    # … add role names here
]
```

Runtime changes made via slash commands are **in-memory only** and reset
when the bot restarts.

---

## Slash commands

All commands require the **Administrator** permission.

| Command | Description |
|---|---|
| `/usernameguard status` | Show current config summary |
| `/usernameguard list_patterns` | List all blocked regex patterns |
| `/usernameguard add_pattern <pattern>` | Add a new blocked pattern |
| `/usernameguard remove_pattern <pattern>` | Remove a pattern |
| `/usernameguard test_name <name>` | Test a name without taking action |
| `/usernameguard list_roles` | List whitelisted role names |
| `/usernameguard add_role <role>` | Whitelist a role |
| `/usernameguard remove_role <role>` | Remove a role from the whitelist |
| `/usernameguard set_log_channel <channel>` | Set the audit log channel |
| `/usernameguard clear_log_channel` | Disable audit log messages |

---

## Known limitations

- **Homoglyph attacks** — names using Unicode lookalike characters
  (e.g. `Ａdmin` using a fullwidth A) will bypass plain regex matching.
  Mitigating this requires a Unicode confusables library and is outside the
  scope of this cog.
- **No persistence** — runtime pattern/role changes reset on restart.
  Edit the `DEFAULT_*` constants for permanent changes.

---

## Changelog

- **1.0.0** — Initial release.
