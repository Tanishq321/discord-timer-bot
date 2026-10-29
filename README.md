# Discord Timer Bot

Schedule a message that pings `@everyone` (or `@here`, or a role) at the time you choose.

## Commands

| Command | What it does |
|---|---|
| `/schedule when message [channel] [ping] [role] [repeat]` | Schedule a ping |
| `/timers` | Show upcoming timers in this server |
| `/cancel timer_id` | Cancel a timer |

**Examples**
```
/schedule when: 18:30 message: Match starts now, join VC!
/schedule when: in 2h message: Submission deadline in 1 hour ping: @here
/schedule when: tomorrow 9am message: Good morning!\nStandup in 10 mins repeat: Every day
/schedule when: 2026-11-02 20:00 message: Movie night 🍿 channel: #general
```

Time formats: `in 30m`, `2h15m`, `1d 4h`, `18:30`, `6:30 pm`, `6pm`, `tomorrow 9am`,
`2026-11-02 18:30`, `02-11-2026 18:30`, `02/11/2026 6:30 pm`.
A time with no date means the next time it comes around (today, or tomorrow if it has passed).
Times are read in the `TIMEZONE` from `.env` (default `Asia/Kolkata`). The confirmation message
shows the time in each viewer's own timezone, so you can double-check it.

Only members with the **Mention @everyone** permission see `/schedule` and `/cancel` by default.
Server admins can change that under Server Settings → Integrations → your bot.

## Setup

### 1. Create the bot
1. Go to https://discord.com/developers/applications → **New Application**.
2. Open **Bot** → **Reset Token** → copy the token (keep it secret).
3. Copy the **Application ID** from **General Information**.

### 2. Invite it to your server
Open this link with your Application ID filled in:
```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=150528
```
That grants View Channels, Send Messages, Embed Links and **Mention @everyone, @here, and All Roles**.
Without the mention permission, the message still posts but nobody gets notified.

### 3. Run it
```bash
cd discord-timer-bot
python -m venv venv
# Windows: venv\Scripts\activate     macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# edit .env and paste your token
python bot.py
```
Tip: put your server ID in `GUILD_ID` (right-click the server → Copy Server ID, with Developer Mode on)
so the slash commands appear immediately.

## Keeping it running 24/7
The bot only fires timers while it is running. Timers are saved in `timers.db`,
so if the bot was off when a timer was due, it sends it as soon as it starts again.

To keep it online without your laptop:
- **DEPLOY-RENDER.md** is the easiest way: free, all in the browser, no terminal (Render + Neon + UptimeRobot).
- **HOSTING.md** covers your own small cloud server (Azure for Students / Oracle), which is more reliable
  but needs a few terminal commands.
