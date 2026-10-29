# Discord Timer Bot

Schedule messages that ping your server at the time you choose, with reminders, RSVPs,
countdowns, birthdays and more.

## Commands

### Scheduling
| Command | What it does |
|---|---|
| `/schedule when message …` | Schedule a server message (options below) |
| `/edit timer_id …` | Change anything about a timer. Only fill in what you want to change |
| `/pause` · `/resume` · `/cancel` | Pause, resume or delete a timer (the timer number autocompletes) |
| `/timers` | List this server's timers |
| `/remindme when message [repeat] [remind_before]` | Personal reminder sent to **your DMs** (no server ping, anyone can use it) |
| `/myreminders` | List your personal reminders |
| `/status` | Uptime, latency, timer counts, next timer |

Every new timer's confirmation has buttons: **📤 Send now · ✏️ Edit · ⏸️ Pause/▶️ Resume · 🗑️ Cancel**.
Only the person who made it, or a moderator, can use them.

### `/schedule` options (all optional except `when` and `message`)
| Option | Example | What it does |
|---|---|---|
| `channel` | #general | Where to send it (default: current channel) |
| `ping` | @everyone / @here / No ping | Who to ping (default @everyone) |
| `role` | @Gamers | Ping a role instead |
| `ping_going_from` | `3` | Ping **only people who clicked ✅ Going** on timer #3's RSVP message |
| `repeat` | `daily`, `weekdays`, `monthly`, `every 2h`, `mon,thu` | Repeat rule |
| `remind_before` | `1d, 1h, 10m` | Extra "starts in 1 hour" heads-up messages |
| `title` / `image_url` / `color` | `Match Night`, a link, `gold` / `#ff8800` | Turns the message into a nice embed |
| `rsvp` | True | Adds **✅ Going / ❌ Not going / 👥 Who's going?** buttons with live counts |
| `countdown` | True | Posts a countdown message right away that ticks down live until the time |
| `thread` | True | Opens a discussion thread under the message |
| `delete_after` | `2h` | Auto-deletes the messages later to keep the channel clean |
| `discord_event` | True | Also creates an event in the server's **Events** tab |

**Time formats:** `in 30m`, `2h15m`, `18:30`, `6:30 pm`, `noon`, `today 9pm`, `tomorrow 9am`, `friday 8pm`,
`next friday 8pm`, `in 3 days at noon`, `5 november 8pm`, `dec 25 at 10am`, `2026-11-02 18:30`.
Use `\n` in the message for a new line, or use the **Edit** button, whose message box takes real new lines.

**Example: game night with RSVP**
```
/schedule when: friday 7pm  message: Game night tonight at 9! Who's in?  title: 🎮 Game Night  color: purple  rsvp: True
/schedule when: friday 9pm  message: Starting now, hop in VC!  ping_going_from: 1  remind_before: 15m
```

### Admin settings (`/settings`, needs Manage Server)
| Command | What it does |
|---|---|
| `/settings timezone` | Server timezone for reading times (autocomplete; `IST`, `GMT`, `EST`… work) |
| `/settings scheduler_role @role` | Let a role use `/schedule` (by default only mods: Mention @everyone / Manage Server) |
| `/settings max_timers 5` | Limit active timers per member (mods exempt) |
| `/settings log_channel #mod-log` | Logs who scheduled / edited / paused / cancelled what |
| `/settings birthday_channel #general [hour]` | Turns on birthday wishes (default 09:00) |
| `/settings show` | Show current settings |

### Birthdays
`/birthday set day month` · `/birthday remove` · `/birthday list` (upcoming birthdays).
The bot posts "🎂 Happy birthday @you!" in the birthday channel on the day.

## Setup

### 1. Create the bot
1. https://discord.com/developers/applications → **New Application**.
2. **Bot** → **Reset Token** → copy the token (keep it secret).
3. Copy the **Application ID** from **General Information**.

### 2. Invite it (or re-invite to give it the new permissions)
```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=17910013840384
```
This grants: View Channels, Send Messages, Embed Links, Read Message History, **Mention @everyone**,
Create Public Threads, Send Messages in Threads, and Create/Manage Events.
Opening the link again for a server the bot is already in just updates its permissions. Your timers stay.

### 3. Run it
- **24/7 for free in your browser:** follow **DEPLOY-RENDER.md**
- **Your own cloud server:** follow **HOSTING.md**
- **On your computer (for testing):**
  ```bash
  pip install -r requirements.txt
  cp .env.example .env        # paste your token into .env
  python bot.py
  ```
