"""
Instagram Unban Monitor — Discord Bot
--------------------------------------
Tracks Instagram accounts you flag as "banned" and posts a Discord
notification with full stats the moment they become reachable again.

Storage: a Cloudflare Worker + D1 database (see cloudflare-worker/), so
tracked accounts survive restarts even on free hosting tiers.

Instagram checking: uses a logged-in session via instaloader (lightweight,
pure-Python, easy to install anywhere including Termux) rather than
anonymous requests, since Instagram aggressively rate-limits/blocks
anonymous checks from datacenter IPs.

Commands (slash commands):
  /track username        - start tracking an account (starts the timer)
  /untrack username      - stop tracking an account
  /list                  - show everything currently being tracked
  /setchannel            - set the channel this bot posts recovery alerts to
                            (run this once in the channel you want alerts in)
  /checknow username     - debug: immediately check one account and show the result

Setup: see README.md
"""

import os
import json
import base64
import asyncio
import tempfile
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

import instaloader
from instaloader.exceptions import ProfileNotExistsException, LoginRequiredException

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY", "")

IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

L = instaloader.Instaloader(
    quiet=True,
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
    compress_json=False,
)


# ---------- Instagram client init (runs once at startup) ----------

async def fetch_ig_session_from_d1() -> bytes:
    config = await api_get_config()
    session_b64 = config.get("ig_session")
    if not session_b64:
        raise SystemExit(
            "No Instagram session found in the database. Run instagram_login.py "
            "locally first — it uploads the session directly, no copy/paste needed. "
            "See README.md."
        )
    return base64.b64decode(session_b64)


def init_instagram_client(session_bytes: bytes):
    if not IG_USERNAME:
        raise SystemExit("IG_USERNAME must be set (the account the session belongs to).")
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(session_bytes)
        session_path = f.name
    try:
        L.load_session_from_file(IG_USERNAME, filename=session_path)
        # cheap authenticated call to confirm the session is valid
        L.test_login()
        if not L.context.username:
            raise LoginRequiredException("session did not authenticate")
        print(f"Instagram session loaded and verified as @{L.context.username}.")
    except (LoginRequiredException, FileNotFoundError, Exception) as e:
        if IG_USERNAME and IG_PASSWORD:
            print(f"Session invalid/expired ({e}) — attempting fresh login...")
            L.login(IG_USERNAME, IG_PASSWORD)
            print("Instagram login succeeded.")
        else:
            raise SystemExit(
                f"Instagram session invalid and IG_USERNAME/IG_PASSWORD not set "
                f"for fallback login. Re-run instagram_login.py. Error: {e}"
            )
    finally:
        os.unlink(session_path)


# ---------- keep-alive web server (for Render free tier + UptimeRobot) ----------

keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def home():
    return "Instagram Unban Monitor is running."


def run_keep_alive():
    keep_alive_app.run(host="0.0.0.0", port=PORT)


def start_keep_alive():
    t = Thread(target=run_keep_alive)
    t.daemon = True
    t.start()


# ---------- D1 storage (via Cloudflare Worker API) ----------

def _d1_headers():
    return {
        "Authorization": f"Bearer {D1_API_KEY}",
        "Content-Type": "application/json",
    }


async def api_get_tracked() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{D1_WORKER_URL}/tracked", headers=_d1_headers()) as resp:
            rows = await resp.json()
            return {
                row["username"]: {
                    "start_time": row["start_time"],
                    "recovered": bool(row["recovered"]),
                    "recovered_at": row.get("recovered_at"),
                }
                for row in rows
            }


async def api_add_tracked(username: str, start_time: str):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{D1_WORKER_URL}/tracked",
            headers=_d1_headers(),
            json={"username": username, "start_time": start_time},
        )


async def api_mark_recovered(username: str, recovered_at: str):
    async with aiohttp.ClientSession() as session:
        await session.patch(
            f"{D1_WORKER_URL}/tracked/{username}",
            headers=_d1_headers(),
            json={"recovered_at": recovered_at},
        )


async def api_remove_tracked(username: str):
    async with aiohttp.ClientSession() as session:
        await session.delete(f"{D1_WORKER_URL}/tracked/{username}", headers=_d1_headers())


async def api_get_config() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{D1_WORKER_URL}/config", headers=_d1_headers()) as resp:
            return await resp.json()


async def api_set_config(key: str, value):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{D1_WORKER_URL}/config", headers=_d1_headers(), json={key: value}
        )


# ---------- Instagram status check (logged-in via instaloader) ----------

def _sync_check_instagram(username: str):
    """Blocking call — always run this through asyncio.to_thread()."""
    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except ProfileNotExistsException:
        return None
    except Exception as e:
        print(f"Instagram check error for @{username}: {type(e).__name__}: {e}")
        return None

    return {
        "username": profile.username,
        "full_name": profile.full_name or "",
        "followers": profile.followers,
        "following": profile.followees,
        "posts": profile.mediacount,
        "profile_pic_url": profile.profile_pic_url or "",
        "is_verified": bool(profile.is_verified),
    }


async def check_instagram_status(username: str):
    """Returns None if unreachable/banned, else a dict of profile stats."""
    return await asyncio.to_thread(_sync_check_instagram, username)


def format_elapsed(start_iso: str) -> str:
    start = datetime.fromisoformat(start_iso)
    delta = datetime.now(timezone.utc) - start
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} hours, {minutes} minutes, {seconds} seconds"


def build_recovery_embed(info: dict, start_iso: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Back From the Void | {info['username']}",
        description=(
            f"Followers: {info['followers']:,} | Following: {info['following']:,}\n"
            f"Elapsed Time: {format_elapsed(start_iso)}"
        ),
        color=discord.Color.green(),
        url=f"https://instagram.com/{info['username']}",
    )
    if info.get("full_name"):
        embed.add_field(name="Name", value=info["full_name"], inline=True)
    embed.add_field(name="Posts", value=f"{info['posts']:,}", inline=True)
    if info.get("is_verified"):
        embed.add_field(name="Verified", value="✅", inline=True)
    if info.get("profile_pic_url"):
        embed.set_thumbnail(url=info["profile_pic_url"])
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


# ---------- background loop ----------

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_accounts():
    tracked = await api_get_tracked()
    if not tracked:
        return

    config = await api_get_config()
    channel_id = config.get("notify_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    for username, meta in tracked.items():
        if meta.get("recovered"):
            continue
        info = await check_instagram_status(username)
        if info is not None:
            embed = build_recovery_embed(info, meta["start_time"])
            await channel.send(embed=embed)
            await api_mark_recovered(username, datetime.now(timezone.utc).isoformat())


@check_tracked_accounts.before_loop
async def before_check():
    await bot.wait_until_ready()


# ---------- slash commands ----------

@bot.event
async def on_ready():
    await bot.tree.sync()
    check_tracked_accounts.start()
    print(f"Logged in as {bot.user} — checking every {CHECK_INTERVAL_MINUTES} min")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[command error] /{interaction.command.name if interaction.command else '?'}: {error!r}")
    message = f"⚠️ Something went wrong: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.command(name="checknow", description="Debug: immediately check an account and show the raw result")
@app_commands.describe(username="Instagram username (without @)")
async def checknow(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    await interaction.response.defer(thinking=True)
    info = await check_instagram_status(username)
    if info:
        await interaction.followup.send(
            f"✅ Live — Followers: {info['followers']:,} | Posts: {info['posts']:,}"
        )
    else:
        await interaction.followup.send(
            "❌ Not reachable — would be treated as still banned. "
            "Check Render logs for the specific error if this looks wrong."
        )


@bot.tree.command(name="track", description="Start tracking an Instagram account for unban recovery")
@app_commands.describe(username="Instagram username (without @)")
async def track(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    tracked = await api_get_tracked()
    if username in tracked and not tracked[username].get("recovered"):
        await interaction.response.send_message(f"Already tracking @{username}.", ephemeral=True)
        return
    await api_add_tracked(username, datetime.now(timezone.utc).isoformat())
    await interaction.response.send_message(f"⏱️ Started tracking **@{username}**. I'll post here when it's back.")


@bot.tree.command(name="untrack", description="Stop tracking an Instagram account")
@app_commands.describe(username="Instagram username (without @)")
async def untrack(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    tracked = await api_get_tracked()
    if username in tracked:
        await api_remove_tracked(username)
        await interaction.response.send_message(f"Stopped tracking @{username}.")
    else:
        await interaction.response.send_message(f"@{username} isn't being tracked.", ephemeral=True)


@bot.tree.command(name="list", description="List all tracked accounts")
async def list_tracked(interaction: discord.Interaction):
    tracked = await api_get_tracked()
    if not tracked:
        await interaction.response.send_message("Nothing is being tracked right now.")
        return
    lines = []
    for username, meta in tracked.items():
        status = "✅ recovered" if meta.get("recovered") else f"⏳ {format_elapsed(meta['start_time'])}"
        lines.append(f"**@{username}** — {status}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="setchannel", description="Set this channel as the recovery notification channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction):
    await api_set_config("notify_channel_id", interaction.channel_id)
    await interaction.response.send_message(f"✅ Recovery notifications will be posted in {interaction.channel.mention}.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    if not D1_WORKER_URL or not D1_API_KEY:
        raise SystemExit("D1_WORKER_URL and D1_API_KEY must be set. See README.md.")
    ig_session_bytes = asyncio.run(fetch_ig_session_from_d1())
    init_instagram_client(ig_session_bytes)
    start_keep_alive()
    bot.run(DISCORD_TOKEN)
