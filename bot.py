"""
Instagram Unban Monitor — Discord Bot (CLEAN)
--------------------------------------
Minimal bot with only essential commands.
"""

import os
import json
import base64
import asyncio
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

import instaloader
from instaloader.exceptions import ProfileNotExistsException

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY", "")

IG_USERNAME = os.getenv("IG_USERNAME", "")
PROXY_URL = os.getenv("PROXY_URL", "")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

L = None


def create_instaloader():
    return instaloader.Instaloader(
        quiet=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=2,
        request_timeout=20,
    )


# ---------- Owner-only check ----------

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ---------- keep-alive web server ----------

keep_alive_app = Flask(__name__)

@keep_alive_app.route("/")
def home():
    return "Instagram Unban Monitor is running."

def run_keep_alive():
    keep_alive_app.run(host="0.0.0.0", port=PORT, debug=False)

def start_keep_alive():
    t = Thread(target=run_keep_alive)
    t.daemon = True
    t.start()


# ---------- D1 storage API ----------

def _d1_headers():
    return {
        "Authorization": f"Bearer {D1_API_KEY}",
        "Content-Type": "application/json",
    }

async def api_request(method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
    url = f"{D1_WORKER_URL}/{endpoint}"
    headers = _d1_headers()
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=headers, json=data, timeout=10) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        return {}
        except:
            await asyncio.sleep(1)
    return {}

async def api_get_tracked() -> Dict:
    result = await api_request("GET", "tracked")
    if result and isinstance(result, list):
        return {
            row["username"]: {
                "start_time": row["start_time"],
                "recovered": bool(row.get("recovered", False)),
                "recovered_at": row.get("recovered_at"),
            }
            for row in result
        }
    return {}

async def api_add_tracked(username: str, start_time: str):
    await api_request("POST", "tracked", {"username": username, "start_time": start_time})

async def api_mark_recovered(username: str, recovered_at: str):
    await api_request("PATCH", f"tracked/{username}", {"recovered_at": recovered_at})

async def api_remove_tracked(username: str):
    await api_request("DELETE", f"tracked/{username}")

async def api_get_config() -> Dict:
    result = await api_request("GET", "config")
    return result if isinstance(result, dict) else {}

async def api_set_config(key: str, value):
    await api_request("POST", "config", {key: value})


# ---------- Session management ----------

async def load_session_from_d1() -> bool:
    global L
    
    config = await api_get_config()
    session_b64 = config.get("ig_session")
    if not session_b64:
        return False
    
    try:
        session_bytes = base64.b64decode(session_b64)
        
        fd, path = tempfile.mkstemp(prefix='ig_session_', suffix='.dat')
        os.write(fd, session_bytes)
        os.close(fd)
        
        L = create_instaloader()
        if PROXY_URL:
            L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        
        L.load_session_from_file(IG_USERNAME, filename=path)
        os.unlink(path)
        
        test = L.test_login()
        if test:
            print(f"✅ Session loaded as @{test}")
            return True
    except Exception as e:
        print(f"Session load failed: {e}")
    
    return False


# ---------- Instagram check ----------

def _check_instagram(username: str):
    global L
    if L is None:
        return None
    
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        return {
            "username": profile.username,
            "full_name": profile.full_name or "",
            "followers": profile.followers,
            "following": profile.followees,
            "posts": profile.mediacount,
            "profile_pic_url": profile.profile_pic_url or "",
            "is_verified": bool(profile.is_verified),
        }
    except ProfileNotExistsException:
        return None
    except Exception as e:
        print(f"Check error for @{username}: {e}")
        return None

async def check_instagram_status(username: str):
    if not await load_session_from_d1():
        return None
    
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_check_instagram, username), 
            timeout=25
        )
    except:
        return None


def format_elapsed(start_iso: str) -> str:
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts) if parts else "0s"
    except:
        return "unknown"


def build_recovery_embed(info: Dict, start_iso: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎉 Account Recovered! @{info['username']}",
        description=(
            f"**The account is back online!**\n\n"
            f"📊 **Stats:**\n"
            f"• Followers: {info['followers']:,}\n"
            f"• Following: {info['following']:,}\n"
            f"• Posts: {info['posts']:,}\n\n"
            f"⏱️ **Banned for:** {format_elapsed(start_iso)}"
        ),
        color=discord.Color.green(),
        url=f"https://instagram.com/{info['username']}",
    )
    if info.get("profile_pic_url"):
        embed.set_thumbnail(url=info["profile_pic_url"])
    embed.set_footer(text=f"Recovered at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed


# ---------- Background check ----------

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


# ---------- Discord commands ----------

@bot.event
async def on_ready():
    await bot.tree.sync()
    check_tracked_accounts.start()
    print(f"✅ Bot is ready")
    
    if await load_session_from_d1():
        print("✅ Instagram session loaded")
    else:
        print("❌ No session found - use /uploadsession")


@bot.tree.command(name="uploadsession", description="Upload Instagram session (base64)")
@app_commands.describe(session_b64="Base64 encoded session file")
@is_owner()
async def uploadsession(interaction: discord.Interaction, session_b64: str):
    await interaction.response.defer(thinking=True)
    
    try:
        session_bytes = base64.b64decode(session_b64)
        
        fd, path = tempfile.mkstemp(prefix='ig_session_', suffix='.dat')
        os.write(fd, session_bytes)
        os.close(fd)
        
        L = create_instaloader()
        if PROXY_URL:
            L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        
        L.load_session_from_file(IG_USERNAME, filename=path)
        os.unlink(path)
        
        test = L.test_login()
        if test:
            await api_set_config("ig_session", session_b64)
            await api_set_config("ig_username", test)
            await interaction.followup.send(f"✅ Session uploaded! Logged in as @{test}")
        else:
            await interaction.followup.send("❌ Invalid session")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@bot.tree.command(name="track", description="Start tracking an account")
@app_commands.describe(username="Instagram username")
@is_owner()
async def track(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    
    tracked = await api_get_tracked()
    if username in tracked and not tracked[username].get("recovered"):
        await interaction.response.send_message(f"⚠️ Already tracking @{username}", ephemeral=True)
        return
    
    if username in tracked:
        await api_remove_tracked(username)
    
    await api_add_tracked(username, datetime.now(timezone.utc).isoformat())
    await interaction.response.send_message(f"⏱️ Started tracking @{username}")


@bot.tree.command(name="untrack", description="Stop tracking an account")
@app_commands.describe(username="Instagram username")
@is_owner()
async def untrack(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    
    tracked = await api_get_tracked()
    if username in tracked:
        await api_remove_tracked(username)
        await interaction.response.send_message(f"✅ Stopped tracking @{username}")
    else:
        await interaction.response.send_message(f"❌ @{username} isn't being tracked", ephemeral=True)


@bot.tree.command(name="list", description="List tracked accounts")
@is_owner()
async def list_tracked(interaction: discord.Interaction):
    tracked = await api_get_tracked()
    if not tracked:
        await interaction.response.send_message("📭 Nothing is being tracked")
        return
    
    lines = ["**Tracked accounts:**"]
    for username, meta in tracked.items():
        status = "✅ recovered" if meta.get("recovered") else f"⏳ {format_elapsed(meta['start_time'])}"
        lines.append(f"@{username} — {status}")
    
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="setchannel", description="Set notification channel")
@is_owner()
async def setchannel(interaction: discord.Interaction):
    await api_set_config("notify_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(f"✅ Notifications in {interaction.channel.mention}")


@bot.tree.command(name="getsession", description="Get instructions for creating a session")
@is_owner()
async def getsession(interaction: discord.Interaction):
    instructions = (
        "**How to create an Instagram session:**\n\n"
        "1. Install instaloader:\n"
        "```bash\npip install instaloader\n```\n\n"
        "2. Run this Python script:\n"
        "```python\n"
        "import instaloader\n"
        "import base64\n\n"
        "L = instaloader.Instaloader()\n"
        "L.login('YOUR_USERNAME', 'YOUR_PASSWORD')\n\n"
        "L.save_session_to_file('session.dat')\n"
        "with open('session.dat', 'rb') as f:\n"
        "    print(base64.b64encode(f.read()).decode())\n"
        "```\n\n"
        "3. Copy the output and use `/uploadsession`"
    )
    await interaction.response.send_message(instructions)


@bot.tree.command(name="checknow", description="Check an account instantly")
@app_commands.describe(username="Instagram username")
@is_owner()
async def checknow(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    await interaction.response.defer(thinking=True)
    
    info = await check_instagram_status(username)
    if info:
        await interaction.followup.send(
            f"✅ **@{username}** is LIVE!\n"
            f"Followers: {info['followers']:,}\n"
            f"Posts: {info['posts']:,}"
        )
    else:
        await interaction.followup.send(f"❌ **@{username}** is NOT reachable")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN not set")
    if not D1_WORKER_URL or not D1_API_KEY:
        raise SystemExit("❌ D1_WORKER_URL and D1_API_KEY not set")
    
    start_keep_alive()
    bot.run(DISCORD_TOKEN)