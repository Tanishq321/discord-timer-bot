"""Discord timer bot: schedule messages that ping your server at the time you choose.

Commands
    /schedule      schedule a server message (embeds, RSVP, countdown, thread, auto-delete, Discord event...)
    /edit          change a timer          /pause /resume /cancel
    /timers        list server timers      /remindme  personal DM reminder   /myreminders
    /settings ...  timezone, who can schedule, limits, log channel, birthday channel (admins)
    /birthday ...  set / remove / list birthdays
    /status        bot health
"""

import asyncio
import calendar
import copy
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from storage import Store
from timeparse import (align_first, describe_repeat, format_duration, next_occurrence,
                       parse_duration, parse_duration_list, parse_repeat, parse_when)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_TZ = os.getenv("TIMEZONE", "Asia/Kolkata")
DATABASE_URL = os.getenv("DATABASE_URL")          # set this on Render (free Neon Postgres)
DB_PATH = os.getenv("DB_PATH", "timers.db")       # used when DATABASE_URL is not set
GUILD_ID = os.getenv("GUILD_ID")                  # optional: instant slash commands in one server
PORT = os.getenv("PORT")                          # Render sets this; we answer health pings on it
# Public URL of the bot's web page (Render sets RENDER_EXTERNAL_URL). Visiting it keeps free hosts awake.
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")
KEEPALIVE_MINUTES = 5
CHECK_EVERY_SECONDS = 5
LATE_NOTICE_SECONDS = 120        # if a timer fires this late, say so in the message
MAX_PERSONAL_REMINDERS = 25
MAX_OFFSET = 30 * 86400          # reminders / auto-delete: up to 30 days

log = logging.getLogger("timer-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

store = Store(DATABASE_URL, DB_PATH)
STARTED_AT = time.time()
ALL_MENTIONS = discord.AllowedMentions(everyone=True, roles=True, users=True)
NO_MENTIONS = discord.AllowedMentions.none()
BLURPLE = 0x5865F2
COLORS = {
    "blurple": BLURPLE, "red": 0xED4245, "green": 0x57F287, "blue": 0x3498DB, "yellow": 0xFEE75C,
    "gold": 0xF1C40F, "orange": 0xE67E22, "purple": 0x9B59B6, "pink": 0xEB459E, "teal": 0x1ABC9C,
    "white": 0xFFFFFF, "black": 0x23272A, "grey": 0x95A5A6,
}
REPEAT_SUGGESTIONS = ["once", "daily", "weekly", "monthly", "weekdays", "weekends", "hourly",
                      "every 2h", "every 3 days", "mon,thu", "every sat & sun"]
TZ_ALIASES = {"ist": "Asia/Kolkata", "india": "Asia/Kolkata", "gmt": "Europe/London", "uk": "Europe/London",
              "est": "America/New_York", "pst": "America/Los_Angeles", "cet": "Europe/Paris",
              "jst": "Asia/Tokyo", "aest": "Australia/Sydney", "utc": "UTC", "dubai": "Asia/Dubai"}
ALL_TZS = sorted(available_timezones())


class UserError(Exception):
    """A problem with what the user typed; shown to them as-is."""


# ---------------------------------------------------------------- small helpers
def label_of(t: dict) -> str:
    title = t["options"].get("title")
    if title:
        return title
    first = t["message"].strip().splitlines()[0] if t["message"].strip() else "Timer"
    return first if len(first) <= 80 else first[:77] + "..."


def has_embed(t: dict) -> bool:
    o = t["options"]
    return bool(o.get("title") or o.get("image") or o.get("color"))


def build_embed(t: dict) -> discord.Embed:
    o = t["options"]
    e = discord.Embed(title=o.get("title"), description=t["message"], color=o.get("color", BLURPLE))
    if o.get("image"):
        e.set_image(url=o["image"])
    return e


def parse_color(text: str) -> int:
    s = text.strip().lower().lstrip("#")
    if s in COLORS:
        return COLORS[s]
    if re.fullmatch(r"[0-9a-f]{6}", s):
        return int(s, 16)
    raise UserError(f"Unknown colour `{text}`. Use a name ({', '.join(COLORS)}) or hex like `#ff8800`.")


def ping_label(ping: str) -> str:
    if ping == "everyone":
        return "@everyone"
    if ping == "here":
        return "@here"
    if ping.startswith("role:"):
        return f"<@&{ping[5:]}>"
    if ping.startswith("rsvp:"):
        return f"people Going on timer #{ping[5:]}"
    if ping == "dm":
        return "you (DM)"
    return "no ping"


def ping_mention(ping: str) -> str:
    if ping == "everyone":
        return "@everyone"
    if ping == "here":
        return "@here"
    if ping.startswith("role:"):
        return f"<@&{ping[5:]}>"
    return ""


def chunk_mentions(user_ids: list[int], limit: int = 1900) -> list[str]:
    chunks, cur = [], ""
    for uid in user_ids:
        m = f"<@{uid}>"
        if len(cur) + len(m) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {m}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def feature_icons(t: dict) -> str:
    o = t["options"]
    icons = []
    if t["repeat"] != "once":
        icons.append(f"🔁 {describe_repeat(t['repeat'])}")
    if o.get("before"):
        icons.append("⏳ " + ", ".join(format_duration(x) for x in o["before"]) + " before")
    if o.get("rsvp"):
        icons.append("📊 RSVP")
    if o.get("countdown"):
        icons.append("⏱️ countdown")
    if o.get("thread"):
        icons.append("🧵 thread")
    if o.get("delete_after"):
        icons.append(f"🗑️ delete after {format_duration(o['delete_after'])}")
    if o.get("event"):
        icons.append("📅 Discord event")
    if has_embed(t):
        icons.append("🖼️ embed")
    return " · ".join(icons)


def is_manager(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    p = member.guild_permissions
    return p.administrator or p.manage_guild or p.mention_everyone


def can_modify(user, t: dict) -> bool:
    if t["author_id"] == user.id:
        return True
    return t["ping"] != "dm" and isinstance(user, discord.Member) and user.guild.id == t["guild_id"] and is_manager(user)


# ---------------------------------------------------------------- the bot
class TimerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.timers: dict[int, dict] = {}       # in-memory copy; the database is the backup
        self.settings: dict[int, dict] = {}
        self.deletions: list[dict] = []
        self.lock = asyncio.Lock()
        self.session: Optional[aiohttp.ClientSession] = None
        self._bg_tasks: set = set()

    # ------------------------------------------------------------ startup
    async def setup_hook(self) -> None:
        await self.start_web_server()
        await asyncio.to_thread(store.init)
        self.timers = {t["id"]: t for t in await asyncio.to_thread(store.all_timers)}
        self.settings = await asyncio.to_thread(store.all_settings)
        self.deletions = await asyncio.to_thread(store.all_deletions)
        log.info("Loaded %d timer(s), %d server setting(s) from %s",
                 len(self.timers), len(self.settings), store.kind)

        self.add_dynamic_items(TimerButton, RsvpButton)
        self.tree.add_command(settings_group)
        self.tree.add_command(birthday_group)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        self.session = aiohttp.ClientSession()
        # Warm up the date parser now; its first use is slow, which could make a command time out.
        try:
            await asyncio.to_thread(parse_when, "5 november 8pm", datetime.now(ZoneInfo(DEFAULT_TZ)))
        except Exception:
            pass
        self.check_due.start()
        self.birthday_check.start()
        if KEEPALIVE_URL:
            self.keep_awake.start()
            log.info("Keep-alive: visiting %s every %d min", KEEPALIVE_URL, KEEPALIVE_MINUTES)

    async def start_web_server(self) -> None:
        """Tiny web page so hosts like Render see a running web service and pingers keep it awake."""
        if not PORT:
            return

        async def health(_request):
            status = "online" if self.is_ready() else "starting"
            return web.Response(text=f"Timer bot {status}. {len(self.timers)} timer(s) scheduled.")

        app = web.Application()
        app.router.add_get("/", health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(PORT)).start()
        log.info("Health page listening on port %s", PORT)

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    # ------------------------------------------------------------ settings
    def gsettings(self, guild_id: Optional[int]) -> dict:
        s = {"timezone": DEFAULT_TZ, "scheduler_role": None, "max_timers": 0, "log_channel": None,
             "birthday_channel": None, "birthday_hour": 9}
        s.update(self.settings.get(guild_id or 0, {}))
        return s

    def tz_for(self, guild_id: Optional[int]) -> ZoneInfo:
        try:
            return ZoneInfo(self.gsettings(guild_id)["timezone"])
        except ZoneInfoNotFoundError:
            return ZoneInfo(DEFAULT_TZ)

    def timer_tz(self, t: dict) -> ZoneInfo:
        try:
            return ZoneInfo(t["options"].get("tz") or self.gsettings(t["guild_id"])["timezone"])
        except ZoneInfoNotFoundError:
            return ZoneInfo(DEFAULT_TZ)

    async def update_settings(self, guild_id: int, **changes) -> dict:
        data = dict(self.settings.get(guild_id, {}))
        data.update(changes)
        self.settings[guild_id] = data
        # Save in the background so commands can reply to Discord instantly.
        self._bg(asyncio.to_thread(store.save_settings, guild_id, data), "save settings")
        return self.gsettings(guild_id)

    def _bg(self, coro, what: str) -> None:
        async def runner():
            try:
                await coro
            except Exception:
                log.exception("Background task failed: %s", what)
        task = asyncio.create_task(runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def can_schedule(self, member) -> bool:
        if is_manager(member):
            return True
        role_id = self.gsettings(member.guild.id)["scheduler_role"]
        return bool(role_id) and any(r.id == role_id for r in member.roles)

    async def log_event(self, guild_id: int, text: str) -> None:
        ch_id = self.gsettings(guild_id)["log_channel"]
        if not ch_id:
            return
        try:
            ch = await self.get_chan(ch_id)
            await ch.send(text, allowed_mentions=NO_MENTIONS)
        except Exception as e:
            log.warning("Log channel error: %s", e)

    # ------------------------------------------------------------ persistence
    async def save(self, t: dict) -> None:
        try:
            await asyncio.to_thread(store.save_timer, t)
        except Exception:
            log.exception("Could not save timer #%s", t["id"])

    async def get_chan(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def create_timer(self, t: dict) -> dict:
        t["id"] = await asyncio.to_thread(store.add_timer, t)
        self.timers[t["id"]] = t
        if t["options"].get("countdown") or t["options"].get("event"):
            await self.update_countdown(t)
            await self.sync_event(t)
            await self.save(t)
        return t

    async def cancel_timer(self, t: dict) -> None:
        self.timers.pop(t["id"], None)
        await self.delete_countdown(t)
        await self.delete_event(t)
        await asyncio.to_thread(store.delete_timer, t["id"])

    async def finish_timer(self, t: dict) -> None:
        self.timers.pop(t["id"], None)
        await self.update_countdown(t, "started")
        await asyncio.to_thread(store.delete_timer, t["id"])

    async def drop_timer(self, t: dict, reason: str) -> None:
        log.warning("Dropping timer #%s: %s", t["id"], reason)
        self.timers.pop(t["id"], None)
        await asyncio.to_thread(store.delete_timer, t["id"])
        if t["ping"] != "dm":
            await self.log_event(t["guild_id"], f"⚠️ Removed timer #{t['id']}: {reason}")

    def reset_before(self, t: dict, now: int) -> None:
        """Mark advance reminders whose moment already passed as done."""
        o = t["options"]
        o["before_sent"] = [x for x in o.get("before", []) if t["fire_at"] - x <= now]

    # ------------------------------------------------------------ sending
    async def ping_parts(self, t: dict) -> tuple[str, list[str]]:
        """Mention text for this timer, plus overflow chunks if there are many people."""
        if t["ping"].startswith("rsvp:"):
            rec = await asyncio.to_thread(store.latest_rsvp_message, int(t["ping"][5:]))
            users = (await asyncio.to_thread(store.rsvp_users, rec["message_id"]))["yes"] if rec else []
            chunks = chunk_mentions(users)
            return (chunks[0], chunks[1:]) if chunks else ("", [])
        return ping_mention(t["ping"]), []

    async def track_for_deletion(self, t: dict, messages: list) -> None:
        after = t["options"].get("delete_after")
        if not after:
            return
        when = int(time.time()) + after
        for m in messages:
            d = {"message_id": m.id, "channel_id": m.channel.id, "delete_at": when}
            self.deletions.append(d)
            await asyncio.to_thread(store.add_deletion, m.id, m.channel.id, when)

    async def deliver(self, t: dict, late: bool = False) -> discord.Message:
        o = t["options"]
        note = f"\n-# ⏰ Delayed: this was scheduled for <t:{t['fire_at']}:t>." if late else ""

        if t["ping"] == "dm":
            user = self.get_user(t["author_id"]) or await self.fetch_user(t["author_id"])
            if has_embed(t):
                return await user.send(f"⏰ **Reminder**{note}", embed=build_embed(t))
            return await user.send(f"⏰ **Reminder:** {t['message']}{note}")

        channel = await self.get_chan(t["channel_id"])
        prefix, extra = await self.ping_parts(t)
        embed = build_embed(t) if has_embed(t) else None
        if embed:
            content = (prefix + note).strip() or None
        else:
            if len(prefix) + len(t["message"]) + len(note) > 1990:
                extra.insert(0, prefix)
                prefix = ""
            content = f"{prefix} {t['message']}".strip() + note

        view = rsvp_view(0, 0) if o.get("rsvp") else None
        msg = await channel.send(content=content, embed=embed, view=view, allowed_mentions=ALL_MENTIONS)
        sent = [msg]
        for chunk in extra:
            if chunk:
                sent.append(await channel.send(chunk, allowed_mentions=ALL_MENTIONS))

        if o.get("rsvp"):
            await asyncio.to_thread(store.add_rsvp_message, msg.id, t["id"], t["guild_id"], int(time.time()))
        if o.get("thread") and isinstance(channel, discord.TextChannel):
            try:
                await msg.create_thread(name=label_of(t)[:90], auto_archive_duration=1440)
            except discord.HTTPException as e:
                log.warning("Couldn't create thread for #%s: %s", t["id"], e)
        await self.track_for_deletion(t, sent)
        return msg

    async def send_advance(self, t: dict, offset: int) -> None:
        fa = t["fire_at"]
        if t["ping"] == "dm":
            user = self.get_user(t["author_id"]) or await self.fetch_user(t["author_id"])
            await user.send(f"⏳ **{label_of(t)}** <t:{fa}:R> (<t:{fa}:t>)")
            return
        channel = await self.get_chan(t["channel_id"])
        prefix, extra = await self.ping_parts(t)
        msg = await channel.send(f"{prefix} ⏳ **{label_of(t)}** starts <t:{fa}:R> (<t:{fa}:t>)".strip(),
                                 allowed_mentions=ALL_MENTIONS)
        sent = [msg] + [await channel.send(c, allowed_mentions=ALL_MENTIONS) for c in extra]
        await self.track_for_deletion(t, sent)

    async def fire(self, t: dict, now: int, manual: bool = False) -> bool:
        late = not manual and now - t["fire_at"] > LATE_NOTICE_SECONDS
        try:
            await self.deliver(t, late=late)
            log.info("Fired timer #%s%s", t["id"], " (manual)" if manual else "")
        except (discord.NotFound, discord.Forbidden) as e:
            await self.drop_timer(t, f"I can't post there anymore ({e.text or e})")
            return False
        except discord.HTTPException as e:
            log.warning("Timer #%s failed, will retry: %s", t["id"], e)
            return False

        if manual and t["repeat"] != "once":
            return True  # "Send now" on a repeating timer: extra copy, schedule unchanged
        nxt = None if manual else next_occurrence(t["fire_at"], t["repeat"], self.timer_tz(t), now,
                                                  t["options"].get("dom"))
        if nxt is None:
            await self.finish_timer(t)
        else:
            t["fire_at"] = nxt
            t["options"]["before_sent"] = []
            self.reset_before(t, now)
            await self.update_countdown(t)
            await self.sync_event(t, new=True)
            await self.save(t)
        return True

    # ------------------------------------------------------------ countdown message
    def countdown_embed(self, t: dict, state: str) -> discord.Embed:
        fa, label = t["fire_at"], label_of(t)
        if state == "started":
            return discord.Embed(title=f"✅ {label}", description=f"Started <t:{fa}:R>", color=0x57F287)
        if state == "paused":
            return discord.Embed(title=f"⏸️ {label}", description="Paused", color=0x95A5A6)
        desc = f"Starts **<t:{fa}:R>**\n<t:{fa}:F>"
        if t["repeat"] != "once":
            desc += f"\n🔁 Repeats {describe_repeat(t['repeat'])}"
        return discord.Embed(title=f"⏳ {label}", description=desc, color=t["options"].get("color", BLURPLE))

    async def update_countdown(self, t: dict, state: Optional[str] = None) -> None:
        o = t["options"]
        if not o.get("countdown") or t["ping"] == "dm":
            return
        state = state or ("paused" if o.get("paused") else "upcoming")
        embed = self.countdown_embed(t, state)
        try:
            channel = await self.get_chan(t["channel_id"])
            if o.get("countdown_msg"):
                try:
                    await channel.get_partial_message(o["countdown_msg"]).edit(embed=embed)
                    return
                except discord.NotFound:
                    pass
            if state != "started":
                msg = await channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
                o["countdown_msg"] = msg.id
        except discord.HTTPException as e:
            log.warning("Countdown update failed for #%s: %s", t["id"], e)

    async def delete_countdown(self, t: dict) -> None:
        mid = t["options"].pop("countdown_msg", None)
        if not mid:
            return
        try:
            channel = await self.get_chan(t["channel_id"])
            await channel.get_partial_message(mid).delete()
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ Discord scheduled events
    async def sync_event(self, t: dict, new: bool = False) -> None:
        o = t["options"]
        if not o.get("event") or t["ping"] == "dm":
            return
        guild = self.get_guild(t["guild_id"])
        if not guild:
            return
        start = datetime.fromtimestamp(t["fire_at"], timezone.utc)
        end = start + timedelta(hours=1)
        try:
            if o.get("event_id") and not new:
                try:
                    ev = guild.get_scheduled_event(o["event_id"]) or await guild.fetch_scheduled_event(o["event_id"])
                    await ev.edit(name=label_of(t)[:100], start_time=start, end_time=end,
                                  description=t["message"][:1000])
                    return
                except discord.NotFound:
                    pass
            channel = await self.get_chan(t["channel_id"])
            ev = await guild.create_scheduled_event(
                name=label_of(t)[:100], start_time=start, end_time=end,
                entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                location=f"#{getattr(channel, 'name', 'discord')}"[:100], description=t["message"][:1000],
            )
            o["event_id"] = ev.id
        except discord.HTTPException as e:
            log.warning("Discord event sync failed for #%s: %s", t["id"], e)

    async def delete_event(self, t: dict) -> None:
        eid = t["options"].pop("event_id", None)
        guild = self.get_guild(t["guild_id"])
        if not eid or not guild:
            return
        try:
            ev = guild.get_scheduled_event(eid) or await guild.fetch_scheduled_event(eid)
            if ev.status == discord.EventStatus.scheduled:
                await ev.delete()
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ pause / resume
    async def set_paused(self, t: dict, paused: bool) -> Optional[str]:
        o = t["options"]
        if paused:
            o["paused"] = True
        else:
            now = int(time.time())
            if t["fire_at"] <= now:
                nxt = next_occurrence(t["fire_at"], t["repeat"], self.timer_tz(t), now, o.get("dom"))
                if nxt is None:
                    return "Its time passed while it was paused. Use `/edit` to give it a new time, then resume."
                t["fire_at"] = nxt
                await self.sync_event(t)
            o.pop("paused", None)
            self.reset_before(t, now)
        await self.update_countdown(t)
        await self.save(t)
        return None

    # ------------------------------------------------------------ loops
    @tasks.loop(seconds=CHECK_EVERY_SECONDS)
    async def check_due(self) -> None:
        now = int(time.time())
        async with self.lock:
            for t in list(self.timers.values()):
                if t["id"] not in self.timers or t["options"].get("paused"):
                    continue
                try:
                    if t["fire_at"] <= now:
                        await self.fire(t, now)
                        continue
                    o = t["options"]
                    done = o.get("before_sent", [])
                    pending = [x for x in o.get("before", []) if x not in done and t["fire_at"] - x <= now]
                    if pending:
                        try:
                            await self.send_advance(t, min(pending))
                        except discord.HTTPException as e:
                            log.warning("Advance reminder for #%s failed: %s", t["id"], e)
                        o["before_sent"] = sorted(set(done) | set(pending))
                        await self.save(t)
                except Exception:
                    log.exception("Error while processing timer #%s", t["id"])

            for d in [d for d in self.deletions if d["delete_at"] <= now]:
                try:
                    channel = await self.get_chan(d["channel_id"])
                    await channel.get_partial_message(d["message_id"]).delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    continue  # try again next round
                self.deletions.remove(d)
                await asyncio.to_thread(store.remove_deletion, d["message_id"])

    @check_due.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def birthday_check(self) -> None:
        for gid in list(self.settings):
            s = self.gsettings(gid)
            if not s["birthday_channel"]:
                continue
            local = datetime.now(self.tz_for(gid))
            today = local.date().isoformat()
            if s.get("birthday_last") == today or local.hour < s["birthday_hour"]:
                continue
            await self.update_settings(gid, birthday_last=today)
            try:
                rows = await asyncio.to_thread(store.birthdays, gid)
                leap = calendar.isleap(local.year)
                users = [r["user_id"] for r in rows
                         if (r["day"], r["month"]) == (local.day, local.month)
                         or (not leap and (r["day"], r["month"]) == (29, 2) and (local.day, local.month) == (28, 2))]
                if users:
                    channel = await self.get_chan(s["birthday_channel"])
                    who = " ".join(f"<@{u}>" for u in users)
                    await channel.send(f"🎂 Happy birthday {who}! Have an amazing day 🎉🥳",
                                       allowed_mentions=discord.AllowedMentions(users=True))
            except Exception:
                log.exception("Birthday check failed for guild %s", gid)

    @birthday_check.before_loop
    async def _wait_ready_b(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=KEEPALIVE_MINUTES)
    async def keep_awake(self) -> None:
        """Visit our own public URL. Render counts this as incoming traffic, so the app stays awake."""
        try:
            async with self.session.get(KEEPALIVE_URL, timeout=aiohttp.ClientTimeout(total=30)) as r:
                log.debug("Keep-alive ping: %s", r.status)
        except Exception as e:
            log.warning("Keep-alive ping failed: %s", e)

    # ------------------------------------------------------------ build / validate a timer
    async def prepare(self, interaction: discord.Interaction, base: dict, *, is_new: bool,
                      when=None, message=None, channel=None, ping=None, role=None, ping_going_from=None,
                      repeat=None, remind_before=None, title=None, image_url=None, color=None,
                      rsvp=None, countdown=None, thread=None, delete_after=None, discord_event=None):
        """Return (timer, warnings) with the changes applied, or raise UserError/ValueError."""
        t = copy.deepcopy(base)
        o = t["options"]
        warnings: list[str] = []
        tz = self.timer_tz(t)
        now_dt = datetime.now(tz)
        now = int(now_dt.timestamp())

        if message is not None:
            t["message"] = message.replace("\\n", "\n").strip()
            if not t["message"]:
                raise UserError("The message can't be empty.")
        if channel is not None:
            t["channel_id"] = channel.id
        if repeat is not None:
            t["repeat"] = parse_repeat(repeat)

        dt = None
        if when is not None:
            dt = parse_when(when, now_dt)
        elif repeat is not None and not is_new:
            dt = datetime.fromtimestamp(t["fire_at"], tz)
        if dt is not None:
            dt = align_first(dt, t["repeat"])
            if dt <= now_dt:
                raise UserError("That time is in the past.")
            t["fire_at"] = int(dt.timestamp())
            o["dom"] = dt.day

        if role is not None:
            t["ping"] = f"role:{role.id}"
        elif ping_going_from is not None:
            src = self.timers.get(ping_going_from)
            ok = src is not None and src["guild_id"] == interaction.guild_id and src["options"].get("rsvp")
            if not ok:
                rec = await asyncio.to_thread(store.latest_rsvp_message, ping_going_from)
                ok = rec is not None and rec["guild_id"] == interaction.guild_id
            if not ok:
                raise UserError(f"Timer #{ping_going_from} doesn't have RSVP buttons in this server.")
            t["ping"] = f"rsvp:{ping_going_from}"
        elif ping is not None:
            t["ping"] = ping

        def clearing(v: str) -> bool:
            return v.strip().lower() in ("", "-", "none", "off", "no", "remove", "clear")

        if remind_before is not None:
            if clearing(remind_before):
                o.pop("before", None)
            else:
                offs = parse_duration_list(remind_before)
                if any(x < 60 or x > MAX_OFFSET for x in offs):
                    raise UserError("Advance reminders must be between 1 minute and 30 days before.")
                o["before"] = offs
        if title is not None:
            if clearing(title):
                o.pop("title", None)
            else:
                o["title"] = title.strip()[:256]
        if image_url is not None:
            if clearing(image_url):
                o.pop("image", None)
            elif not re.match(r"https?://\S+$", image_url.strip()):
                raise UserError("The image must be a link starting with http:// or https://")
            else:
                o["image"] = image_url.strip()
        if color is not None:
            if clearing(color):
                o.pop("color", None)
            else:
                o["color"] = parse_color(color)
        if delete_after is not None:
            if clearing(delete_after):
                o.pop("delete_after", None)
            else:
                secs = parse_duration(delete_after)
                if not secs or secs < 60 or secs > MAX_OFFSET:
                    raise UserError("Auto-delete must be a duration between 1 minute and 30 days, like `2h`.")
                o["delete_after"] = secs
        for value, key in ((rsvp, "rsvp"), (countdown, "countdown"), (thread, "thread"), (discord_event, "event")):
            if value is not None:
                if value:
                    o[key] = True
                else:
                    o.pop(key, None)

        if when is not None or remind_before is not None or repeat is not None:
            self.reset_before(t, now)

        # Can the bot actually do all this in that channel?
        if t["ping"] != "dm":
            guild = interaction.guild
            target = guild.get_channel_or_thread(t["channel_id"]) or interaction.channel
            perms = target.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages):
                raise UserError(f"I can't send messages in {target.mention}. Give me View Channel + Send Messages there.")
            if (has_embed(t) or o.get("countdown")) and not perms.embed_links:
                raise UserError(f"I need the **Embed Links** permission in {target.mention} for titles, images, colours and countdowns.")
            needs_mention = t["ping"] in ("everyone", "here") or t["ping"].startswith("role:")
            if needs_mention and not perms.mention_everyone:
                role_obj = guild.get_role(int(t["ping"][5:])) if t["ping"].startswith("role:") else None
                if not (role_obj and role_obj.mentionable):
                    warnings.append("I don't have **Mention @everyone, @here, and All Roles** there, "
                                    "so the message will post but nobody will get pinged.")
            if o.get("thread"):
                if not isinstance(target, discord.TextChannel):
                    o.pop("thread")
                    warnings.append("Threads only work in normal text channels, so I turned that off.")
                elif not perms.create_public_threads:
                    o.pop("thread")
                    warnings.append("I don't have **Create Public Threads** there, so I turned threads off.")
            if o.get("event"):
                gp = guild.me.guild_permissions
                if not (gp.manage_events or getattr(gp, "create_events", False)):
                    o.pop("event")
                    warnings.append("I don't have the **Create Events** permission, so no Discord event was made.")
        return t, warnings

    async def commit_edit(self, old: dict, new: dict) -> None:
        oo, no = old["options"], new["options"]
        moved = old["channel_id"] != new["channel_id"]
        if oo.get("countdown_msg") and (not no.get("countdown") or moved):
            await self.delete_countdown(old)
            no.pop("countdown_msg", None)
        if oo.get("event_id") and not no.get("event"):
            await self.delete_event(old)
            no.pop("event_id", None)
        self.timers[new["id"]] = new
        await self.update_countdown(new)
        await self.sync_event(new)
        await self.save(new)


bot = TimerBot()


# ---------------------------------------------------------------- buttons
def timer_controls(t: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for action in ("send", "edit", "resume" if t["options"].get("paused") else "pause", "cancel"):
        v.add_item(TimerButton(action, t["id"]))
    return v


def rsvp_view(yes: int, no: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(RsvpButton("yes", yes))
    v.add_item(RsvpButton("no", no))
    v.add_item(RsvpButton("list"))
    return v


class TimerButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"timer:(?P<action>send|edit|pause|resume|cancel):(?P<id>\d+)"):
    STYLES = {
        "send": ("Send now", "📤", discord.ButtonStyle.primary),
        "edit": ("Edit", "✏️", discord.ButtonStyle.secondary),
        "pause": ("Pause", "⏸️", discord.ButtonStyle.secondary),
        "resume": ("Resume", "▶️", discord.ButtonStyle.success),
        "cancel": ("Cancel", "🗑️", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, timer_id: int):
        label, emoji, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"timer:{action}:{timer_id}"))
        self.action, self.timer_id = action, timer_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        t = bot.timers.get(self.timer_id)
        if not t:
            return await interaction.response.send_message(
                "This timer doesn't exist anymore (it was already sent or cancelled).", ephemeral=True)
        if not can_modify(interaction.user, t):
            return await interaction.response.send_message(
                "Only the person who made this timer, or a moderator, can change it.", ephemeral=True)

        if self.action == "edit":
            return await interaction.response.send_modal(EditModal(t))

        if self.action == "cancel":
            await interaction.response.edit_message(view=None)
            async with bot.lock:
                await bot.cancel_timer(t)
            await interaction.followup.send(f"🗑️ Timer #{t['id']} cancelled.", ephemeral=True)
            if t["ping"] != "dm":
                await bot.log_event(t["guild_id"], f"🗑️ {interaction.user} cancelled timer #{t['id']} ({label_of(t)})")
            return

        if self.action in ("pause", "resume"):
            await interaction.response.defer()  # acknowledge the click instantly
            async with bot.lock:
                err = await bot.set_paused(t, self.action == "pause")
            if err:
                return await interaction.followup.send(f"❌ {err}", ephemeral=True)
            await interaction.edit_original_response(view=timer_controls(t))
            word = "paused ⏸️" if self.action == "pause" else f"resumed ▶️, next run <t:{t['fire_at']}:R>"
            await interaction.followup.send(f"Timer #{t['id']} {word}.", ephemeral=True)
            if t["ping"] != "dm":
                await bot.log_event(t["guild_id"], f"{'⏸️' if self.action == 'pause' else '▶️'} {interaction.user} "
                                                   f"{self.action}d timer #{t['id']}")
            return

        if self.action == "send":
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with bot.lock:
                ok = await bot.fire(t, int(time.time()), manual=True)
            if ok:
                extra = "" if t["repeat"] == "once" else " Its regular schedule is unchanged."
                await interaction.followup.send(f"📤 Sent timer #{t['id']} now.{extra}", ephemeral=True)
                if t["ping"] != "dm":
                    await bot.log_event(t["guild_id"], f"📤 {interaction.user} sent timer #{t['id']} early")
            else:
                await interaction.followup.send("❌ Couldn't send it. Check my permissions in that channel.",
                                                ephemeral=True)


class RsvpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rsvp:(?P<choice>yes|no|list)"):
    def __init__(self, choice: str, count: int = 0):
        if choice == "yes":
            button = discord.ui.Button(label=f"Going ({count})", emoji="✅",
                                       style=discord.ButtonStyle.success, custom_id="rsvp:yes")
        elif choice == "no":
            button = discord.ui.Button(label=f"Not going ({count})", emoji="❌",
                                       style=discord.ButtonStyle.secondary, custom_id="rsvp:no")
        else:
            button = discord.ui.Button(label="Who's going?", emoji="👥",
                                       style=discord.ButtonStyle.secondary, custom_id="rsvp:list")
        super().__init__(button)
        self.choice = choice

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["choice"])

    async def callback(self, interaction: discord.Interaction):
        mid = interaction.message.id
        if self.choice == "list":
            await interaction.response.defer(ephemeral=True, thinking=True)
            users = await asyncio.to_thread(store.rsvp_users, mid)
            yes = ", ".join(f"<@{u}>" for u in users["yes"]) or "nobody yet"
            no = ", ".join(f"<@{u}>" for u in users["no"]) or "nobody"
            e = discord.Embed(title="👥 RSVPs", color=BLURPLE)
            e.add_field(name=f"✅ Going ({len(users['yes'])})", value=yes[:1024], inline=False)
            e.add_field(name=f"❌ Not going ({len(users['no'])})", value=no[:1024], inline=False)
            return await interaction.followup.send(embed=e, ephemeral=True)
        await interaction.response.defer()  # acknowledge the click instantly, then save
        await asyncio.to_thread(store.set_rsvp, mid, interaction.user.id, self.choice)
        users = await asyncio.to_thread(store.rsvp_users, mid)
        await interaction.edit_original_response(view=rsvp_view(len(users["yes"]), len(users["no"])))


class EditModal(discord.ui.Modal):
    def __init__(self, t: dict):
        super().__init__(title=f"Edit timer #{t['id']}", timeout=900)
        self.timer_id = t["id"]
        self.orig_when = datetime.fromtimestamp(t["fire_at"], bot.timer_tz(t)).strftime("%Y-%m-%d %H:%M")
        self.orig_title = t["options"].get("title", "")
        self.when = discord.ui.TextInput(label="When", default=self.orig_when, max_length=100)
        self.msg = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph,
                                        default=t["message"], max_length=1800)
        self.title_in = discord.ui.TextInput(label="Title (optional, makes it an embed)", required=False,
                                             default=self.orig_title, max_length=256)
        for item in (self.when, self.msg, self.title_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        t = bot.timers.get(self.timer_id)
        if not t:
            return await interaction.response.send_message("That timer doesn't exist anymore.", ephemeral=True)
        when = self.when.value.strip()
        title = self.title_in.value.strip()
        await apply_edit(interaction, t,
                         when=None if when == self.orig_when else when,
                         message=self.msg.value,
                         title=None if title == self.orig_title else (title or "-"))


# ---------------------------------------------------------------- shared command logic
async def apply_edit(interaction: discord.Interaction, t: dict, **changes):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with bot.lock:
            current = bot.timers.get(t["id"])
            if not current:
                return await interaction.followup.send("That timer doesn't exist anymore.", ephemeral=True)
            new, warnings = await bot.prepare(interaction, current, is_new=False, **changes)
            await bot.commit_edit(current, new)
    except (UserError, ValueError) as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)
    text = f"✏️ Timer **#{new['id']}** updated. Next run <t:{new['fire_at']}:F> (<t:{new['fire_at']}:R>)."
    if new["options"].get("paused"):
        text += " It's still paused."
    if warnings:
        text += "\n" + "\n".join(f"⚠️ {w}" for w in warnings)
    await interaction.followup.send(text, ephemeral=True)
    if new["ping"] != "dm":
        await bot.log_event(new["guild_id"], f"✏️ {interaction.user} edited timer #{new['id']} ({label_of(new)})")


def find_timer(interaction: discord.Interaction, timer_id: int) -> dict:
    t = bot.timers.get(timer_id)
    if not t or (t["ping"] == "dm" and t["author_id"] != interaction.user.id) or \
            (t["ping"] != "dm" and t["guild_id"] != interaction.guild_id):
        raise UserError(f"No timer #{timer_id} here. Use `/timers` or `/myreminders` to see the numbers.")
    if not can_modify(interaction.user, t):
        raise UserError("Only the person who made this timer, or a moderator, can change it.")
    return t


async def timer_id_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for t in sorted(bot.timers.values(), key=lambda x: x["fire_at"]):
        mine_dm = t["ping"] == "dm" and t["author_id"] == interaction.user.id
        here = t["ping"] != "dm" and t["guild_id"] == interaction.guild_id and can_modify(interaction.user, t)
        if not (mine_dm or here):
            continue
        when = datetime.fromtimestamp(t["fire_at"], bot.timer_tz(t)).strftime("%d %b %H:%M")
        name = f"#{t['id']} · {when} · {'DM · ' if t['ping'] == 'dm' else ''}{label_of(t)}"
        if current and current.lower() not in name.lower():
            continue
        out.append(app_commands.Choice(name=name[:100], value=t["id"]))
        if len(out) == 25:
            break
    return out


async def repeat_autocomplete(_interaction, current: str):
    return [app_commands.Choice(name=s, value=s) for s in REPEAT_SUGGESTIONS if current.lower() in s][:25]


async def color_autocomplete(_interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in COLORS if current.lower() in n][:25]


PING_CHOICES = [
    app_commands.Choice(name="@everyone", value="everyone"),
    app_commands.Choice(name="@here (online only)", value="here"),
    app_commands.Choice(name="No ping", value="none"),
]
SHARED_DESCRIBE = dict(
    channel="Where to send it (defaults to this channel)",
    ping="Who to ping (default: @everyone)",
    role="Ping a specific role instead",
    ping_going_from="Ping only people who clicked ✅ Going on this timer's RSVP message",
    repeat="once, daily, weekly, monthly, weekdays, every 2h, mon,thu ...",
    remind_before="Extra heads-up messages before, e.g. '1h, 10m' ('none' to remove)",
    title="Title. Makes the message a nice embed ('none' to remove)",
    image_url="Image link shown in the embed ('none' to remove)",
    color="Embed colour: red, green, gold, #ff8800 ...",
    rsvp="Add ✅ Going / ❌ Not going buttons",
    countdown="Post a live countdown message now that ticks down to the time",
    thread="Open a discussion thread under the message",
    delete_after="Auto-delete the messages after e.g. '2h' ('none' to keep)",
    discord_event="Also create an event in the server's Events tab",
)


# ---------------------------------------------------------------- /schedule
@bot.tree.command(name="schedule", description="Schedule a message that pings the server at a set time")
@app_commands.describe(when="e.g. 'in 30m', '18:30', 'friday 8pm', 'tomorrow 9am', '5 nov 8pm'",
                       message="What to send. Use \\n for a new line.", **SHARED_DESCRIBE)
@app_commands.choices(ping=PING_CHOICES)
@app_commands.autocomplete(repeat=repeat_autocomplete, color=color_autocomplete)
@app_commands.guild_only()
async def schedule(
    interaction: discord.Interaction,
    when: str,
    message: app_commands.Range[str, 1, 1800],
    channel: Optional[discord.TextChannel] = None,
    ping: Optional[app_commands.Choice[str]] = None,
    role: Optional[discord.Role] = None,
    ping_going_from: Optional[int] = None,
    repeat: Optional[str] = None,
    remind_before: Optional[str] = None,
    title: Optional[str] = None,
    image_url: Optional[str] = None,
    color: Optional[str] = None,
    rsvp: Optional[bool] = None,
    countdown: Optional[bool] = None,
    thread: Optional[bool] = None,
    delete_after: Optional[str] = None,
    discord_event: Optional[bool] = None,
):
    member = interaction.user
    if not bot.can_schedule(member):
        return await interaction.response.send_message(
            "❌ You don't have permission to schedule server pings. Ask an admin "
            "(they can allow a role with `/settings scheduler_role`). You can still use `/remindme`.",
            ephemeral=True)
    s = bot.gsettings(interaction.guild_id)
    if s["max_timers"] and not is_manager(member):
        mine = sum(1 for t in bot.timers.values()
                   if t["guild_id"] == interaction.guild_id and t["author_id"] == member.id and t["ping"] != "dm")
        if mine >= s["max_timers"]:
            return await interaction.response.send_message(
                f"❌ You already have {mine} timers here (the limit is {s['max_timers']}). Cancel one first.",
                ephemeral=True)

    # Reply to Discord right away (it allows only 3 seconds); the slow work happens after this.
    await interaction.response.defer(ephemeral=True, thinking=True)

    base = {"guild_id": interaction.guild_id, "channel_id": (channel or interaction.channel).id,
            "author_id": member.id, "message": "", "ping": "everyone", "fire_at": 0, "repeat": "once",
            "options": {"tz": s["timezone"]}}
    try:
        t, warnings = await bot.prepare(
            interaction, base, is_new=True, when=when, message=message, ping=ping.value if ping else None,
            role=role, ping_going_from=ping_going_from, repeat=repeat or "once", remind_before=remind_before,
            title=title, image_url=image_url, color=color, rsvp=rsvp, countdown=countdown, thread=thread,
            delete_after=delete_after, discord_event=discord_event)
    except (UserError, ValueError) as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)

    try:
        async with bot.lock:
            t = await bot.create_timer(t)
    except Exception:
        log.exception("Could not save timer")
        return await interaction.followup.send("❌ Couldn't save the timer (database error). Try again.",
                                               ephemeral=True)

    fa = t["fire_at"]
    text = (f"⏰ {member.mention} set timer **#{t['id']}** for <t:{fa}:F> (<t:{fa}:R>) in <#{t['channel_id']}>"
            f" → {ping_label(t['ping'])}.")
    icons = feature_icons(t)
    if icons:
        text += f"\n{icons}"
    # Public confirmation in the channel (with buttons); private notes only for the person who made it.
    try:
        await interaction.channel.send(text, view=timer_controls(t), allowed_mentions=NO_MENTIONS)
        private = f"✅ Timer #{t['id']} scheduled."
    except discord.HTTPException:
        private = text
    if warnings:
        private += "\n" + "\n".join(f"⚠️ {w}" for w in warnings)
    await interaction.followup.send(private, ephemeral=True, allowed_mentions=NO_MENTIONS,
                                    view=None if private != text else timer_controls(t))
    await bot.log_event(t["guild_id"], f"📝 {member} scheduled timer #{t['id']} for <t:{fa}:f> in "
                                       f"<#{t['channel_id']}>: {label_of(t)}")


# ---------------------------------------------------------------- /edit
@bot.tree.command(name="edit", description="Change a scheduled timer (only fill in what you want to change)")
@app_commands.describe(timer_id="Which timer", when="New time", message="New message", **SHARED_DESCRIBE)
@app_commands.choices(ping=PING_CHOICES)
@app_commands.autocomplete(timer_id=timer_id_autocomplete, repeat=repeat_autocomplete, color=color_autocomplete)
async def edit(
    interaction: discord.Interaction,
    timer_id: int,
    when: Optional[str] = None,
    message: Optional[app_commands.Range[str, 1, 1800]] = None,
    channel: Optional[discord.TextChannel] = None,
    ping: Optional[app_commands.Choice[str]] = None,
    role: Optional[discord.Role] = None,
    ping_going_from: Optional[int] = None,
    repeat: Optional[str] = None,
    remind_before: Optional[str] = None,
    title: Optional[str] = None,
    image_url: Optional[str] = None,
    color: Optional[str] = None,
    rsvp: Optional[bool] = None,
    countdown: Optional[bool] = None,
    thread: Optional[bool] = None,
    delete_after: Optional[str] = None,
    discord_event: Optional[bool] = None,
):
    try:
        t = find_timer(interaction, timer_id)
        if t["ping"] == "dm" and any(v is not None for v in (channel, ping, role, ping_going_from, rsvp, countdown,
                                                              thread, discord_event)):
            raise UserError("Personal reminders can only change time, message, repeat, reminders and embed look.")
    except UserError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    await apply_edit(interaction, t, when=when, message=message, channel=channel,
                     ping=ping.value if ping else None, role=role, ping_going_from=ping_going_from,
                     repeat=repeat, remind_before=remind_before, title=title, image_url=image_url, color=color,
                     rsvp=rsvp, countdown=countdown, thread=thread, delete_after=delete_after,
                     discord_event=discord_event)


# ---------------------------------------------------------------- /pause /resume /cancel
async def _pause_resume(interaction: discord.Interaction, timer_id: int, pause: bool):
    try:
        t = find_timer(interaction, timer_id)
    except UserError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    if bool(t["options"].get("paused")) == pause:
        return await interaction.response.send_message(
            f"Timer #{timer_id} is already {'paused' if pause else 'running'}.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.lock:
        err = await bot.set_paused(t, pause)
    if err:
        return await interaction.followup.send(f"❌ {err}", ephemeral=True)
    msg = "⏸️ Paused" if pause else f"▶️ Resumed, next run <t:{t['fire_at']}:F> (<t:{t['fire_at']}:R>)"
    await interaction.followup.send(f"{msg}: timer #{timer_id}.", ephemeral=True)
    if t["ping"] != "dm":
        await bot.log_event(t["guild_id"], f"{'⏸️' if pause else '▶️'} {interaction.user} "
                                           f"{'paused' if pause else 'resumed'} timer #{timer_id}")


@bot.tree.command(name="pause", description="Pause a timer without deleting it")
@app_commands.autocomplete(timer_id=timer_id_autocomplete)
async def pause(interaction: discord.Interaction, timer_id: int):
    await _pause_resume(interaction, timer_id, True)


@bot.tree.command(name="resume", description="Resume a paused timer")
@app_commands.autocomplete(timer_id=timer_id_autocomplete)
async def resume(interaction: discord.Interaction, timer_id: int):
    await _pause_resume(interaction, timer_id, False)


@bot.tree.command(name="cancel", description="Delete a timer")
@app_commands.autocomplete(timer_id=timer_id_autocomplete)
async def cancel(interaction: discord.Interaction, timer_id: int):
    try:
        t = find_timer(interaction, timer_id)
    except UserError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.lock:
        await bot.cancel_timer(t)
    await interaction.followup.send(f"🗑️ Timer #{timer_id} cancelled.", ephemeral=True)
    if t["ping"] != "dm":
        await bot.log_event(t["guild_id"], f"🗑️ {interaction.user} cancelled timer #{timer_id} ({label_of(t)})")


# ---------------------------------------------------------------- lists
def timers_embed(rows: list[dict], title: str) -> discord.Embed:
    e = discord.Embed(title=title, color=BLURPLE)
    for t in rows[:25]:
        fa = t["fire_at"]
        state = "⏸️ PAUSED · " if t["options"].get("paused") else ""
        where = "" if t["ping"] == "dm" else f" in <#{t['channel_id']}> → {ping_label(t['ping'])}"
        icons = feature_icons(t)
        value = f"{state}<t:{fa}:f> (<t:{fa}:R>){where}"
        if icons:
            value += f"\n{icons}"
        e.add_field(name=f"#{t['id']} · {label_of(t)}"[:256], value=value[:1024], inline=False)
    if len(rows) > 25:
        e.set_footer(text=f"Showing 25 of {len(rows)}")
    return e


@bot.tree.command(name="timers", description="List scheduled timers in this server")
@app_commands.guild_only()
async def list_timers(interaction: discord.Interaction):
    rows = sorted((t for t in bot.timers.values() if t["guild_id"] == interaction.guild_id and t["ping"] != "dm"),
                  key=lambda t: t["fire_at"])
    if not rows:
        return await interaction.response.send_message("No timers scheduled. Make one with `/schedule`.",
                                                       ephemeral=True)
    await interaction.response.send_message(embed=timers_embed(rows, "⏰ Upcoming timers"), ephemeral=True)


@bot.tree.command(name="myreminders", description="List your personal DM reminders")
async def my_reminders(interaction: discord.Interaction):
    rows = sorted((t for t in bot.timers.values() if t["ping"] == "dm" and t["author_id"] == interaction.user.id),
                  key=lambda t: t["fire_at"])
    if not rows:
        return await interaction.response.send_message("You have no personal reminders. Try `/remindme`.",
                                                       ephemeral=True)
    await interaction.response.send_message(embed=timers_embed(rows, "🔔 Your reminders"), ephemeral=True)


# ---------------------------------------------------------------- /remindme
@bot.tree.command(name="remindme", description="Get a personal reminder in your DMs (no server ping)")
@app_commands.describe(when="e.g. 'in 2h', 'tomorrow 9am', 'friday 6pm'", message="What to remind you about",
                       repeat="once, daily, weekly, weekdays, every 2h ...",
                       remind_before="Extra heads-up DMs before, e.g. '1h, 10m'")
@app_commands.autocomplete(repeat=repeat_autocomplete)
async def remindme(interaction: discord.Interaction, when: str, message: app_commands.Range[str, 1, 1800],
                   repeat: Optional[str] = None, remind_before: Optional[str] = None):
    mine = sum(1 for t in bot.timers.values() if t["ping"] == "dm" and t["author_id"] == interaction.user.id)
    if mine >= MAX_PERSONAL_REMINDERS:
        return await interaction.response.send_message(
            f"❌ You already have {mine} reminders. Cancel some with `/cancel` first.", ephemeral=True)
    base = {"guild_id": interaction.guild_id or 0, "channel_id": 0, "author_id": interaction.user.id,
            "message": "", "ping": "dm", "fire_at": 0, "repeat": "once",
            "options": {"tz": bot.gsettings(interaction.guild_id)["timezone"]}}
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        t, _ = await bot.prepare(interaction, base, is_new=True, when=when, message=message,
                                 repeat=repeat or "once", remind_before=remind_before)
    except (UserError, ValueError) as e:
        return await interaction.followup.send(f"❌ {e}", ephemeral=True)
    async with bot.lock:
        t = await bot.create_timer(t)
    fa = t["fire_at"]
    text = f"🔔 Got it! I'll DM you <t:{fa}:F> (<t:{fa}:R>): {t['message'][:200]}"
    icons = feature_icons(t)
    if icons:
        text += f"\n{icons}"
    text += "\n-# Make sure you allow DMs from server members, or I can't reach you."
    await interaction.followup.send(text, view=timer_controls(t), ephemeral=True)


# ---------------------------------------------------------------- /settings (admins)
settings_group = app_commands.Group(name="settings", description="Server settings for the timer bot",
                                    guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


def settings_embed(guild: discord.Guild) -> discord.Embed:
    s = bot.gsettings(guild.id)
    e = discord.Embed(title="⚙️ Timer bot settings", color=BLURPLE)
    e.add_field(name="Timezone", value=s["timezone"])
    e.add_field(name="Who can schedule pings",
                value="Mods (Mention @everyone / Manage Server)" +
                      (f" + <@&{s['scheduler_role']}>" if s["scheduler_role"] else ""), inline=False)
    e.add_field(name="Max timers per member", value=str(s["max_timers"] or "Unlimited"))
    e.add_field(name="Log channel", value=f"<#{s['log_channel']}>" if s["log_channel"] else "Off")
    e.add_field(name="Birthday channel",
                value=f"<#{s['birthday_channel']}> at {s['birthday_hour']:02d}:00" if s["birthday_channel"] else "Off")
    return e


@settings_group.command(name="show", description="Show current settings")
async def settings_show(interaction: discord.Interaction):
    await interaction.response.send_message(embed=settings_embed(interaction.guild), ephemeral=True)


@settings_group.command(name="timezone", description="Timezone used to read times like '18:30'")
@app_commands.describe(timezone="e.g. Asia/Kolkata, Europe/London, America/New_York")
async def settings_timezone(interaction: discord.Interaction, timezone: str):
    name = TZ_ALIASES.get(timezone.strip().lower(), timezone.strip())
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return await interaction.response.send_message(f"❌ Unknown timezone `{timezone}`. Pick one from the list.",
                                                       ephemeral=True)
    await bot.update_settings(interaction.guild_id, timezone=name)
    now = datetime.now(ZoneInfo(name)).strftime("%H:%M")
    await interaction.response.send_message(
        f"🌍 Timezone set to **{name}** (it's {now} there now). New timers use it; existing timers keep their times.",
        ephemeral=True)


@settings_timezone.autocomplete("timezone")
async def _tz_autocomplete(_interaction, current: str):
    cur = current.strip().lower()
    out = [app_commands.Choice(name=f"{k.upper()} → {v}", value=v) for k, v in TZ_ALIASES.items() if cur and k.startswith(cur)]
    out += [app_commands.Choice(name=z, value=z) for z in ALL_TZS if cur in z.lower()][: 25 - len(out)]
    return out[:25]


@settings_group.command(name="scheduler_role", description="Let members with this role use /schedule (empty = mods only)")
async def settings_scheduler_role(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    await bot.update_settings(interaction.guild_id, scheduler_role=role.id if role else None)
    msg = f"✅ Members with {role.mention} can now schedule pings." if role else "✅ Only mods can schedule pings now."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_MENTIONS)


@settings_group.command(name="max_timers", description="Max active timers per member (0 = unlimited; mods are exempt)")
async def settings_max_timers(interaction: discord.Interaction, limit: app_commands.Range[int, 0, 100]):
    await bot.update_settings(interaction.guild_id, max_timers=limit)
    await interaction.response.send_message(f"✅ Limit set to {limit or 'unlimited'}.", ephemeral=True)


@settings_group.command(name="log_channel", description="Channel that records who scheduled/edited/cancelled what (empty = off)")
async def settings_log_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await bot.update_settings(interaction.guild_id, log_channel=channel.id if channel else None)
    await interaction.response.send_message(f"✅ Log channel: {channel.mention if channel else 'off'}.", ephemeral=True)


@settings_group.command(name="birthday_channel", description="Where birthday wishes are posted (empty = off)")
@app_commands.describe(hour="Hour of the day to post wishes (0-23, server timezone). Default 9")
async def settings_birthday_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                                    hour: app_commands.Range[int, 0, 23] = 9):
    await bot.update_settings(interaction.guild_id, birthday_channel=channel.id if channel else None,
                              birthday_hour=hour)
    msg = (f"🎂 Birthday wishes will go to {channel.mention} at {hour:02d}:00. Members add theirs with `/birthday set`."
           if channel else "✅ Birthday wishes turned off.")
    await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------- /birthday
birthday_group = app_commands.Group(name="birthday", description="Birthday wishes", guild_only=True)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@birthday_group.command(name="set", description="Save your birthday so the bot wishes you")
async def birthday_set(interaction: discord.Interaction, day: app_commands.Range[int, 1, 31],
                       month: app_commands.Range[int, 1, 12]):
    try:
        datetime(2000, month, day)  # 2000 is a leap year, so 29 Feb is allowed
    except ValueError:
        return await interaction.response.send_message("❌ That date doesn't exist.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await asyncio.to_thread(store.set_birthday, interaction.guild_id, interaction.user.id, day, month)
    s = bot.gsettings(interaction.guild_id)
    extra = "" if s["birthday_channel"] else "\n-# An admin still needs to pick a channel with `/settings birthday_channel`."
    await interaction.followup.send(f"🎂 Saved: **{day} {MONTHS[month - 1]}**.{extra}", ephemeral=True)


@birthday_group.command(name="remove", description="Remove your birthday")
async def birthday_remove(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    removed = await asyncio.to_thread(store.remove_birthday, interaction.guild_id, interaction.user.id)
    await interaction.followup.send("🗑️ Removed." if removed else "You hadn't set a birthday.", ephemeral=True)


@birthday_group.command(name="list", description="Upcoming birthdays in this server")
async def birthday_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    rows = await asyncio.to_thread(store.birthdays, interaction.guild_id)
    if not rows:
        return await interaction.followup.send("No birthdays saved yet. Add yours with `/birthday set`.")
    today = datetime.now(bot.tz_for(interaction.guild_id)).date()

    def days_until(r):
        for y in (today.year, today.year + 1):
            d = min(r["day"], calendar.monthrange(y, r["month"])[1])
            cand = today.replace(year=y, month=r["month"], day=d)
            if cand >= today:
                return (cand - today).days
        return 999

    rows.sort(key=days_until)
    lines = []
    for r in rows[:30]:
        n = days_until(r)
        when = "🎉 today!" if n == 0 else ("tomorrow" if n == 1 else f"in {n} days")
        lines.append(f"<@{r['user_id']}> · {r['day']} {MONTHS[r['month'] - 1]} · {when}")
    e = discord.Embed(title="🎂 Upcoming birthdays", description="\n".join(lines), color=0xEB459E)
    await interaction.followup.send(embed=e, allowed_mentions=NO_MENTIONS)


# ---------------------------------------------------------------- /status
@bot.tree.command(name="status", description="Bot health and stats")
async def status(interaction: discord.Interaction):
    up = format_duration(int(time.time() - STARTED_AT))
    here = [t for t in bot.timers.values() if t["guild_id"] == interaction.guild_id and t["ping"] != "dm"]
    active = [t for t in here if not t["options"].get("paused")]
    mine = sum(1 for t in bot.timers.values() if t["ping"] == "dm" and t["author_id"] == interaction.user.id)
    e = discord.Embed(title="🤖 Timer bot status", color=0x57F287)
    e.add_field(name="Uptime", value=up)
    e.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms")
    e.add_field(name="Storage", value=store.kind)
    if interaction.guild_id:
        e.add_field(name="Timers here", value=f"{len(active)} active, {len(here) - len(active)} paused")
        if active:
            nxt = min(active, key=lambda t: t["fire_at"])
            e.add_field(name="Next up", value=f"#{nxt['id']} <t:{nxt['fire_at']}:R>", inline=True)
        e.add_field(name="Timezone", value=bot.gsettings(interaction.guild_id)["timezone"])
    e.add_field(name="Your DM reminders", value=str(mine))
    await interaction.response.send_message(embed=e, ephemeral=True)


# ---------------------------------------------------------------- errors
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        text = "❌ You don't have permission to use that."
    else:
        log.exception("Command error", exc_info=error)
        text = "❌ Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (in .env locally, or in your host's environment variables).")
    bot.run(TOKEN, log_handler=None)
