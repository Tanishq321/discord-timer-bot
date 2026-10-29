# Deploy on Render (free, no server or terminal needed)

You'll use three free websites. No credit card is needed for any of them:

| Site | What it does for the bot |
|---|---|
| **Neon** (neon.com) | Saves your timers in a database. Render deletes local files whenever it restarts, so the timers can't live there. |
| **Render** (render.com) | Runs the bot 24/7 |
| **UptimeRobot** (uptimerobot.com) | Opens the bot's web page every 5 minutes. Render's free plan puts an app to sleep after 15 minutes without visits, so this keeps it awake. |

Before you start, have your **bot token** ready (Discord Developer Portal → your app → Bot → Reset Token),
and make sure the bot is already invited to your server (invite link is in README.md).

---

## 1. Create the database (Neon), ~2 min
1. Go to https://neon.com and sign up (the "Continue with GitHub" option is fine).
2. Create a project: name `discord-bot`, Postgres version = default,
   **Region = AWS US West 2 (Oregon)**.
3. On the project dashboard click **Connect**, then copy the **connection string**.
   It looks like `postgresql://neondb_owner:xxxx@ep-xxxx.us-west-2.aws.neon.tech/neondb?sslmode=require`
   Keep it somewhere for step 3. The bot creates its table by itself.

## 2. Put the code on GitHub, ~3 min
1. Unzip `discord-timer-bot.zip` on your laptop.
2. On https://github.com click **New repository** → name `discord-timer-bot` → choose **Private** → **Create repository**.
3. On the empty repo page, click the link **"uploading an existing file"**.
4. Open the unzipped `discord-timer-bot` folder, select **all the files inside it**
   (bot.py, storage.py, timeparse.py, requirements.txt, render.yaml, etc.) and drag them into the page.
   ⚠️ If you made a `.env` file with your token, **don't upload it**.
5. Click **Commit changes**.

## 3. Deploy on Render, ~5 min
1. Go to https://dashboard.render.com and sign in with GitHub.
2. Click **New +** → **Blueprint**.
3. Connect your `discord-timer-bot` repo. If you don't see it, click **Configure GitHub** and give Render
   access to that repo.
4. Render reads `render.yaml` and asks for two values:
   - `DISCORD_TOKEN` → paste your bot token
   - `DATABASE_URL` → paste the Neon connection string
5. Click **Deploy Blueprint** (or **Apply**). Wait until the service shows **Live** (2–4 minutes).
6. Open the service, then click its URL at the top (like `https://discord-timer-bot-xxxx.onrender.com`).
   You should see: **"Timer bot online. 0 timer(s) scheduled."** Copy this URL.

## 4. Keep it awake (UptimeRobot), ~2 min
1. Sign up at https://uptimerobot.com (free plan).
2. **New monitor** → type **HTTP(s)** → paste your Render URL → interval **5 minutes** → **Create**.

That's it. Close your laptop. 🎉

## 5. Test
In Discord: `/schedule when: in 2m message: Hello from Render!`

---

## Changing the code later
Edit the file on GitHub (open it → pencil icon → **Commit changes**).
Render redeploys automatically within a couple of minutes. Your timers are safe in Neon.

## Troubleshooting
- **Where are the logs?** Render → your service → **Logs**.
- **Logs show `429 Too Many Requests` or "blocked by Cloudflare":** Render's free servers share IP addresses
  with other people's apps, and Discord sometimes blocks a shared IP for a while. Go to **Manual Deploy →
  Clear build cache & deploy** to move to a different machine, or try again later. If it keeps happening,
  use the Azure option in HOSTING.md, which gives you your own server.
- **Slash commands don't show up:** wait a few minutes, or restart Discord (Ctrl+R). For instant updates,
  add an env var `GUILD_ID` = your server ID in Render → **Environment**.
- **A timer fired a minute late:** Render's free plan restarts the app now and then. Timers that come due
  during a restart are sent as soon as the bot is back.

## Limits worth knowing
- Render gives **750 free hours per month**, which is enough for exactly one app running all month.
  Don't run other free Render web services at the same time, or the hours run out before the month ends.
- Neon's free plan sleeps the database when nobody uses it. The bot keeps timers in memory and only
  touches the database when you add, cancel or fire one, so it stays well within the free limits.
