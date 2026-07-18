"""
Instagram Unban Monitor — Discord Bot
--------------------------------------
Tracks Instagram accounts you flag as "banned" and posts a Discord
notification with full stats the moment they become reachable again.

- Only the server owner can use any command.
- Storage: Cloudflare Worker + D1 database (survives restarts).
- Instagram checking: ANONYMOUS public endpoint routed through a proxy.
  No Instagram account, no login, no checkpoints possible — the proxy
  alone is what avoids the datacenter-IP rate limiting.

Commands (slash commands, server owner only):
  /track username        - start tracking an account (starts the timer)
  /untrack username      - stop tracking an account
  /list                  - show everything currently being tracked
  /setchannel            - set the channel this bot posts recovery alerts to
  /checknow username     - debug: immediately check one account

Setup: see README.md
"""

import os
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY", "")

PROXY_URL = os.getenv("PROXY_URL", "")  # comma-separated list of proxy URLs, e.g.:
                                          # http://user:pass@ip1:port,http://user:pass@ip2:port


def _parse_proxy(proxy_url: str):
    """aiohttp does not auto-extract embedded user:pass from a proxy URL —
    they must be passed separately as proxy_auth, or every request silently
    fails proxy auth and gets treated as 'unreachable'."""
    if not proxy_url:
        return None, None
    from urllib.parse import urlsplit
    parts = urlsplit(proxy_url)
    if parts.username:
        auth = aiohttp.BasicAuth(parts.username, parts.password or "")
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        clean_url = f"{parts.scheme}://{netloc}"
        return clean_url, auth
    return proxy_url, None


PROXY_LIST = [_parse_proxy(p.strip()) for p in PROXY_URL.split(",") if p.strip()]


def get_random_proxy():
    """Rotate across multiple proxy IPs so no single IP takes all the
    traffic and gets rate-limited by Instagram on its own."""
    if not PROXY_LIST:
        return None, None
    import random
    return random.choice(PROXY_LIST)

IG_ENDPOINT = "https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
IG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- owner-only check ----------

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Only the server owner can use this bot.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


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


# ---------- Instagram status check (anonymous, via proxy) ----------

async def check_instagram_status(username: str):
    """Returns None if the account is unreachable (banned/suspended/not
    found). Returns a dict of profile stats if the account is live."""
    url = IG_ENDPOINT.format(username=username)
    proxy_clean, proxy_auth = get_random_proxy()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=IG_HEADERS,
                proxy=proxy_clean,
                proxy_auth=proxy_auth,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body_snippet = (await resp.text())[:300]
                    print(
                        f"Instagram check for @{username}: got HTTP {resp.status} — {body_snippet}",
                        flush=True,
                    )
                    return None
                data = await resp.json(content_type=None)
                user = (data or {}).get("data", {}).get("user")
                if not user:
                    print(f"Instagram check for @{username}: 200 OK but no user data in response", flush=True)
                    return None
                return {
                    "username": user.get("username", username),
                    "full_name": user.get("full_name", ""),
                    "followers": user["edge_followed_by"]["count"],
                    "following": user["edge_follow"]["count"],
                    "posts": user["edge_owner_to_timeline_media"]["count"],
                    "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
                    "is_verified": user.get("is_verified", False),
                }
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
        print(f"Instagram check error for @{username}: {type(e).__name__}: {e}", flush=True)
        return None
    except Exception as e:
        print(f"Instagram check UNEXPECTED error for @{username}: {type(e).__name__}: {e}", flush=True)
        return None


# ---------- timer UI ----------

def format_elapsed(start_iso: str) -> str:
    """Short-form elapsed timer, e.g. '2h 15m 30s'."""
    start = datetime.fromisoformat(start_iso)
    delta = datetime.now(timezone.utc) - start
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def build_recovery_embed(info: dict, start_iso: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎉 Account Recovered! @{info['username']}",
        description=(
            f"**The account is back online!**\n\n"
            f"📊 **Stats:**\n"
            f"• Followers: {info['followers']:,}\n"
            f"• Following: {info['following']:,}\n"
            f"• Posts: {info['posts']:,}\n"
            + ("• Verified: ✅\n" if info.get("is_verified") else "")
            + f"\n⏱️ **Banned for:** {format_elapsed(start_iso)}"
        ),
        color=discord.Color.green(),
        url=f"https://instagram.com/{info['username']}",
    )
    if info.get("full_name"):
        embed.add_field(name="Name", value=info["full_name"], inline=True)
    if info.get("profile_pic_url"):
        embed.set_thumbnail(url=info["profile_pic_url"])
    embed.set_footer(text=f"Recovered at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
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
            await channel.send(content=f"📢 **@{username}** is back!", embed=embed)
            await api_mark_recovered(username, datetime.now(timezone.utc).isoformat())
            print(f"✅ Recovered: @{username}")


@check_tracked_accounts.before_loop
async def before_check():
    await bot.wait_until_ready()


# ---------- slash commands (server owner only) ----------

@bot.event
async def on_ready():
    await bot.tree.sync()
    check_tracked_accounts.start()
    print(f"Logged in as {bot.user} — checking every {CHECK_INTERVAL_MINUTES} min")
    if PROXY_LIST:
        print(f"Using {len(PROXY_LIST)} proxy IP(s), rotated per request.")
    else:
        print("WARNING: no PROXY_URL set — anonymous checks may get rate-limited by Instagram.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
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
@is_owner()
async def checknow(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    await interaction.response.defer(thinking=True)
    info = await check_instagram_status(username)
    if info:
        await interaction.followup.send(
            f"✅ **@{username}** is LIVE — Followers: {info['followers']:,} | Posts: {info['posts']:,}"
        )
    else:
        await interaction.followup.send(
            f"❌ **@{username}** is NOT reachable — would be treated as still banned."
        )


@bot.tree.command(name="track", description="Start tracking an Instagram account for unban recovery")
@app_commands.describe(username="Instagram username (without @)")
@is_owner()
async def track(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    tracked = await api_get_tracked()
    if username in tracked and not tracked[username].get("recovered"):
        await interaction.response.send_message(f"⚠️ Already tracking @{username}.", ephemeral=True)
        return
    await api_add_tracked(username, datetime.now(timezone.utc).isoformat())
    await interaction.response.send_message(f"⏱️ Started tracking **@{username}**. I'll post here when it's back.")


@bot.tree.command(name="untrack", description="Stop tracking an Instagram account")
@app_commands.describe(username="Instagram username (without @)")
@is_owner()
async def untrack(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    tracked = await api_get_tracked()
    if username in tracked:
        await api_remove_tracked(username)
        await interaction.response.send_message(f"✅ Stopped tracking @{username}.")
    else:
        await interaction.response.send_message(f"❌ @{username} isn't being tracked.", ephemeral=True)


@bot.tree.command(name="list", description="List all tracked accounts")
@is_owner()
async def list_tracked(interaction: discord.Interaction):
    tracked = await api_get_tracked()
    if not tracked:
        await interaction.response.send_message("📭 Nothing is being tracked right now.")
        return
    lines = ["**Tracked accounts:**"]
    for username, meta in tracked.items():
        status = "✅ recovered" if meta.get("recovered") else f"⏳ {format_elapsed(meta['start_time'])}"
        lines.append(f"@{username} — {status}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="setchannel", description="Set this channel as the recovery notification channel")
@is_owner()
async def setchannel(interaction: discord.Interaction):
    await api_set_config("notify_channel_id", interaction.channel_id)
    await interaction.response.send_message(f"✅ Recovery notifications will be posted in {interaction.channel.mention}.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    if not D1_WORKER_URL or not D1_API_KEY:
        raise SystemExit("D1_WORKER_URL and D1_API_KEY must be set. See README.md.")
    start_keep_alive()
    bot.run(DISCORD_TOKEN)
