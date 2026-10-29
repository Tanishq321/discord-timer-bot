"""Discord timer bot: schedule a message that pings the server at a chosen time.

Slash commands
    /schedule  when message [channel] [ping] [role] [repeat]
    /timers    list upcoming timers in this server
    /cancel    id
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from storage import Store
from timeparse import parse_when

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
DATABASE_URL = os.getenv("DATABASE_URL")          # set this on Render (free Neon Postgres)
DB_PATH = os.getenv("DB_PATH", "timers.db")       # used when DATABASE_URL is not set
GUILD_ID = os.getenv("GUILD_ID")                  # optional: instant slash commands in one server
PORT = os.getenv("PORT")                          # Render sets this; we answer health pings on it
CHECK_EVERY_SECONDS = 5

log = logging.getLogger("timer-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

store = Store(DATABASE_URL, DB_PATH)


def next_fire(fire_at: int, repeat: str, now: int) -> Optional[int]:
    """Next run for a repeating timer (wall-clock aware), or None for one-off timers."""
    step = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}.get(repeat)
    if step is None:
        return None
    dt = datetime.fromtimestamp(fire_at, TZ)
    while int(dt.timestamp()) <= now:  # skip any runs missed while the bot was offline
        dt += step
    return int(dt.timestamp())


def ping_prefix(ping: str) -> str:
    if ping == "everyone":
        return "@everyone"
    if ping == "here":
        return "@here"
    if ping.startswith("role:"):
        return f"<@&{ping[5:]}>"
    return ""


# ---------------------------------------------------------------- bot
class TimerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.timers: dict[int, dict] = {}  # in-memory copy; the database is the backup

    async def setup_hook(self) -> None:
        await self.start_web_server()
        await asyncio.to_thread(store.init)
        self.timers = {t["id"]: t for t in await asyncio.to_thread(store.all)}
        log.info("Loaded %d timer(s) from %s", len(self.timers), store.kind)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.check_due.start()

    async def start_web_server(self) -> None:
        """Tiny web page so hosts like Render see a running web service and pingers keep it awake."""
        if not PORT:
            return

        async def health(_request):
            status = "online" if self.is_ready() else "starting"
            return web.Response(text=f"Timer bot {status}. {len(self.timers)} timer(s) scheduled.")

        app = web.Application()
        app.router.add_get("/", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(PORT)).start()
        log.info("Health page listening on port %s", PORT)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (timezone %s)", self.user, TZ.key)

    @tasks.loop(seconds=CHECK_EVERY_SECONDS)
    async def check_due(self) -> None:
        now = int(time.time())
        due = [t for t in self.timers.values() if t["fire_at"] <= now]

        for t in due:
            try:
                channel = self.get_channel(t["channel_id"]) or await self.fetch_channel(t["channel_id"])
                content = f"{ping_prefix(t['ping'])} {t['message']}".strip()
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True),
                )
                log.info("Fired timer #%s", t["id"])
            except (discord.NotFound, discord.Forbidden) as e:
                # Channel gone or bot lost access: drop the timer so it doesn't retry forever.
                log.warning("Dropping timer #%s: %s", t["id"], e)
                self.timers.pop(t["id"], None)
                await self._db(store.delete, t["id"])
                continue
            except discord.HTTPException as e:
                log.warning("Timer #%s failed, will retry: %s", t["id"], e)
                continue

            nxt = next_fire(t["fire_at"], t["repeat"], now)
            if nxt is None:
                self.timers.pop(t["id"], None)
                await self._db(store.delete, t["id"])
            else:
                t["fire_at"] = nxt
                await self._db(store.set_fire_at, t["id"], nxt)

    @check_due.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    async def _db(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception:
            log.exception("Database error in %s", fn.__name__)


bot = TimerBot()


# ---------------------------------------------------------------- commands
@bot.tree.command(name="schedule", description="Schedule a message that pings the server at a set time")
@app_commands.describe(
    when="e.g. 'in 30m', '18:30', '6:30 pm', 'tomorrow 9am', '2026-11-02 18:30'",
    message="What to send. Use \\n for a new line.",
    channel="Where to send it (defaults to this channel)",
    ping="Who to ping (default: @everyone)",
    role="Ping a specific role instead",
    repeat="Send once, or repeat daily/weekly",
)
@app_commands.choices(
    ping=[
        app_commands.Choice(name="@everyone", value="everyone"),
        app_commands.Choice(name="@here (online only)", value="here"),
        app_commands.Choice(name="No ping", value="none"),
    ],
    repeat=[
        app_commands.Choice(name="Once", value="once"),
        app_commands.Choice(name="Every day", value="daily"),
        app_commands.Choice(name="Every week", value="weekly"),
    ],
)
@app_commands.guild_only()
@app_commands.default_permissions(mention_everyone=True)
async def schedule(
    interaction: discord.Interaction,
    when: str,
    message: app_commands.Range[str, 1, 1800],
    channel: Optional[discord.TextChannel] = None,
    ping: Optional[app_commands.Choice[str]] = None,
    role: Optional[discord.Role] = None,
    repeat: Optional[app_commands.Choice[str]] = None,
):
    target = channel or interaction.channel
    now = datetime.now(TZ)

    try:
        fire_dt = parse_when(when, now)
    except ValueError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    if fire_dt <= now:
        return await interaction.response.send_message("❌ That time is in the past.", ephemeral=True)

    ping_value = f"role:{role.id}" if role else (ping.value if ping else "everyone")
    repeat_value = repeat.value if repeat else "once"
    text = message.replace("\\n", "\n")

    # Make sure the bot can actually post (and ping) in the target channel.
    perms = target.permissions_for(target.guild.me)
    if not (perms.view_channel and perms.send_messages):
        return await interaction.response.send_message(
            f"❌ I can't send messages in {target.mention}. Give me View Channel + Send Messages there.",
            ephemeral=True,
        )
    warning = ""
    needs_mention_perm = ping_value in ("everyone", "here") or (role is not None and not role.mentionable)
    if needs_mention_perm and not perms.mention_everyone:
        warning = (
            "\n⚠️ I don't have **Mention @everyone, @here, and All Roles** in that channel, "
            "so the message will post but nobody will get pinged."
        )

    await interaction.response.defer(thinking=True)  # database may take a moment to wake up

    timer = {
        "guild_id": interaction.guild_id, "channel_id": target.id, "author_id": interaction.user.id,
        "message": text, "ping": ping_value, "fire_at": int(fire_dt.timestamp()), "repeat": repeat_value,
    }
    try:
        timer["id"] = await asyncio.to_thread(store.add, timer)
    except Exception:
        log.exception("Could not save timer")
        return await interaction.followup.send("❌ Couldn't save the timer (database error). Try again.")
    bot.timers[timer["id"]] = timer

    ts = timer["fire_at"]
    who = ping_prefix(ping_value) or "no ping"
    repeat_txt = "" if repeat_value == "once" else f", repeating **{repeat_value}**"
    await interaction.followup.send(
        f"⏰ Timer **#{timer['id']}** set for <t:{ts}:F> (<t:{ts}:R>) in {target.mention}"
        f" → {discord.utils.escape_mentions(who)}{repeat_txt}.{warning}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="timers", description="List upcoming timers in this server")
@app_commands.guild_only()
async def list_timers(interaction: discord.Interaction):
    rows = sorted((t for t in bot.timers.values() if t["guild_id"] == interaction.guild_id),
                  key=lambda t: t["fire_at"])[:25]
    if not rows:
        return await interaction.response.send_message("No timers scheduled.", ephemeral=True)

    embed = discord.Embed(title="⏰ Upcoming timers", color=discord.Color.blurple())
    for r in rows:
        preview = r["message"] if len(r["message"]) <= 80 else r["message"][:77] + "..."
        rep = "" if r["repeat"] == "once" else f" · {r['repeat']}"
        embed.add_field(
            name=f"#{r['id']} · <t:{r['fire_at']}:R>{rep}",
            value=f"<t:{r['fire_at']}:f> in <#{r['channel_id']}> → "
                  f"{discord.utils.escape_mentions(ping_prefix(r['ping']) or 'no ping')}\n{preview}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel", description="Cancel a scheduled timer")
@app_commands.describe(timer_id="The timer number shown in /timers")
@app_commands.guild_only()
@app_commands.default_permissions(mention_everyone=True)
async def cancel(interaction: discord.Interaction, timer_id: int):
    t = bot.timers.get(timer_id)
    if not t or t["guild_id"] != interaction.guild_id:
        return await interaction.response.send_message(f"❌ No timer #{timer_id} in this server.", ephemeral=True)
    bot.timers.pop(timer_id, None)
    await bot._db(store.delete, timer_id)
    await interaction.response.send_message(f"🗑️ Timer #{timer_id} cancelled.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (in .env locally, or in your host's environment variables).")
    bot.run(TOKEN, log_handler=None)
