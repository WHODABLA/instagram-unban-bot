"""
Instagram Unban Monitor — Discord Bot (Final)
--------------------------------------
- Only server owner can use commands
- Sends notifications ONLY when an account recovers (not on startup)
- Fixed all errors with proper error handling
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
from instaloader.exceptions import (
    ProfileNotExistsException, 
    LoginRequiredException,
    ConnectionException,
    QueryReturnedBadRequestException,
    TooManyRequestsException
)

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY", "")

IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")
PROXY_URL = os.getenv("PROXY_URL", "")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Global Instagram loader instance
L = None


def create_instaloader_instance():
    """Create a fresh Instaloader instance with optimal settings"""
    return instaloader.Instaloader(
        quiet=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
    """Check if the user is the server owner"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ---------- Instagram client init ----------

async def fetch_ig_session_from_d1() -> Optional[bytes]:
    """Fetch Instagram session from D1 database"""
    try:
        config = await api_get_config()
        session_b64 = config.get("ig_session")
        if not session_b64:
            print("No Instagram session found in database.")
            return None
        return base64.b64decode(session_b64)
    except Exception as e:
        print(f"Error fetching session from D1: {e}")
        return None


def init_instagram_client(session_bytes: Optional[bytes] = None) -> bool:
    """Initialize Instagram client with session or login credentials"""
    global L
    
    L = create_instaloader_instance()
    
    # Apply proxy if configured
    if PROXY_URL:
        try:
            L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
            print("Using proxy for Instagram requests.")
        except:
            pass
    
    # Try session first
    if session_bytes:
        try:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(session_bytes)
                session_path = f.name
            
            try:
                L.load_session_from_file(IG_USERNAME, filename=session_path)
                verified_username = L.test_login()
                if verified_username:
                    print(f"✅ Instagram session loaded and verified as @{verified_username}.")
                    return True
                else:
                    print("Session verification failed.")
            finally:
                try:
                    os.unlink(session_path)
                except:
                    pass
        except Exception as e:
            print(f"Session loading failed: {e}")
    
    # Fallback to login with credentials
    if IG_USERNAME and IG_PASSWORD:
        try:
            print(f"Attempting login as @{IG_USERNAME}...")
            L.login(IG_USERNAME, IG_PASSWORD)
            print("✅ Instagram login successful.")
            
            # Save session for future use
            try:
                with tempfile.NamedTemporaryFile(delete=False) as f:
                    L.save_session_to_file(f.name)
                    with open(f.name, 'rb') as session_file:
                        session_bytes = session_file.read()
                        # Store session in D1 (async, fire and forget)
                        asyncio.create_task(api_set_config("ig_session", base64.b64encode(session_bytes).decode('ascii')))
                    os.unlink(f.name)
            except Exception as e:
                print(f"Could not save session: {e}")
            
            return True
        except Exception as e:
            print(f"Login failed: {e}")
            return False
    
    print("⚠️ No valid session or credentials available.")
    return False


# ---------- keep-alive web server ----------

keep_alive_app = Flask(__name__)

@keep_alive_app.route("/")
def home():
    return "Instagram Unban Monitor is running."

@keep_alive_app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

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
    """Generic API request handler with retries"""
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
                    else:
                        text = await resp.text()
                        print(f"API error {resp.status}: {text[:200]}")
                        if attempt == 2:
                            return {}
        except asyncio.TimeoutError:
            print(f"API timeout (attempt {attempt + 1})")
        except Exception as e:
            print(f"API request error (attempt {attempt + 1}): {e}")
            if attempt == 2:
                return {}
        await asyncio.sleep(1 * (attempt + 1))
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


# ---------- Instagram status check ----------

def _sync_check_instagram(username: str) -> Optional[Dict[str, Any]]:
    """Blocking call — always run this through asyncio.to_thread()."""
    global L
    
    if L is None:
        print("Instaloader not initialized")
        return None
    
    try:
        # Small delay to avoid rate limiting
        time.sleep(0.3)
        
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
        # Account doesn't exist - treat as banned/unavailable
        return None
    except TooManyRequestsException:
        print(f"Rate limited for @{username}")
        return None
    except (ConnectionException, QueryReturnedBadRequestException) as e:
        print(f"Connection error for @{username}: {e}")
        return None
    except LoginRequiredException:
        print(f"Session expired, trying to re-login...")
        # Try to re-login
        if IG_USERNAME and IG_PASSWORD:
            try:
                L = create_instaloader_instance()
                if PROXY_URL:
                    try:
                        L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
                    except:
                        pass
                L.login(IG_USERNAME, IG_PASSWORD)
                # Retry once after re-login
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
            except Exception as login_error:
                print(f"Re-login failed: {login_error}")
                return None
        return None
    except Exception as e:
        print(f"Instagram check error for @{username}: {type(e).__name__}: {e}")
        return None

async def check_instagram_status(username: str) -> Optional[Dict[str, Any]]:
    """Returns None if unreachable/banned, else a dict of profile stats."""
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_check_instagram, username), 
            timeout=25
        )
        return result
    except asyncio.TimeoutError:
        print(f"Instagram check for @{username} timed out.")
        return None
    except Exception as e:
        print(f"Unexpected error checking @{username}: {e}")
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
        
        return " ".join(parts)
    except:
        return "unknown"


def build_recovery_embed(info: Dict[str, Any], start_iso: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎉 Account Recovered! @{info['username']}",
        description=(
            f"**The account is back online!**\n\n"
            f"📊 **Current Stats:**\n"
            f"• Followers: {info['followers']:,}\n"
            f"• Following: {info['following']:,}\n"
            f"• Posts: {info['posts']:,}\n\n"
            f"⏱️ **Banned for:** {format_elapsed(start_iso)}"
        ),
        color=discord.Color.green(),
        url=f"https://instagram.com/{info['username']}",
    )
    
    if info.get("full_name"):
        embed.add_field(name="Full Name", value=info["full_name"], inline=True)
    
    if info.get("is_verified"):
        embed.add_field(name="Verified", value="✅", inline=True)
    
    if info.get("profile_pic_url"):
        embed.set_thumbnail(url=info["profile_pic_url"])
    
    embed.set_footer(text=f"Recovered at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed


# ---------- background check loop ----------

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_accounts():
    try:
        tracked = await api_get_tracked()
        if not tracked:
            return

        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            return

        channel = bot.get_channel(int(channel_id))
        if channel is None:
            print(f"Channel {channel_id} not found")
            return

        # Filter to only check unrecovered accounts
        unrecovered = {u: m for u, m in tracked.items() if not m.get("recovered")}
        
        for username, meta in unrecovered.items():
            info = await check_instagram_status(username)
            if info is not None:
                # Account is back! Send notification
                embed = build_recovery_embed(info, meta["start_time"])
                try:
                    await channel.send(content=f"📢 **@{username}** is back!", embed=embed)
                    await api_mark_recovered(username, datetime.now(timezone.utc).isoformat())
                    print(f"✅ Recovered and notified: @{username}")
                except discord.HTTPException as e:
                    print(f"Failed to send Discord message for @{username}: {e}")
                except Exception as e:
                    print(f"Error processing recovery for @{username}: {e}")
            # If None, still banned - do nothing
                
    except Exception as e:
        print(f"Error in check_tracked_accounts: {e}")

@check_tracked_accounts.before_loop
async def before_check():
    await bot.wait_until_ready()
    print("Background check loop started.")


# ---------- Discord slash commands ----------

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        check_tracked_accounts.start()
        print(f"✅ Logged in as {bot.user}")
        print(f"✅ Checking every {CHECK_INTERVAL_MINUTES} minutes")
        print(f"✅ Bot is in {len(bot.guilds)} guild(s)")
    except Exception as e:
        print(f"Error in on_ready: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[command error] /{interaction.command.name if interaction.command else '?'}: {error!r}")
    
    if isinstance(error, app_commands.CheckFailure):
        # Already handled by is_owner check
        pass
    else:
        message = f"⚠️ Something went wrong: `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


@bot.tree.command(name="checknow", description="Debug: immediately check an account")
@app_commands.describe(username="Instagram username (without @)")
@is_owner()
async def checknow(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    await interaction.response.defer(thinking=True)
    
    info = await check_instagram_status(username)
    if info:
        await interaction.followup.send(
            f"✅ **@{username}** is live!\n"
            f"• Followers: {info['followers']:,}\n"
            f"• Following: {info['following']:,}\n"
            f"• Posts: {info['posts']:,}"
        )
    else:
        await interaction.followup.send(
            f"❌ **@{username}** is NOT reachable (banned/unavailable)."
        )


@bot.tree.command(name="track", description="Start tracking an Instagram account")
@app_commands.describe(username="Instagram username (without @)")
@is_owner()
async def track(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    
    tracked = await api_get_tracked()
    if username in tracked and not tracked[username].get("recovered"):
        await interaction.response.send_message(f"⚠️ Already tracking **@{username}**.", ephemeral=True)
        return
    
    # If previously recovered, remove old entry first
    if username in tracked:
        await api_remove_tracked(username)
    
    await api_add_tracked(username, datetime.now(timezone.utc).isoformat())
    await interaction.response.send_message(f"⏱️ Started tracking **@{username}**. Will notify when recovered!")


@bot.tree.command(name="untrack", description="Stop tracking an Instagram account")
@app_commands.describe(username="Instagram username (without @)")
@is_owner()
async def untrack(interaction: discord.Interaction, username: str):
    username = username.lstrip("@").strip()
    
    tracked = await api_get_tracked()
    if username in tracked:
        await api_remove_tracked(username)
        await interaction.response.send_message(f"✅ Stopped tracking **@{username}**.")
    else:
        await interaction.response.send_message(f"❌ **@{username}** isn't being tracked.", ephemeral=True)


@bot.tree.command(name="list", description="List all tracked accounts")
@is_owner()
async def list_tracked(interaction: discord.Interaction):
    tracked = await api_get_tracked()
    if not tracked:
        await interaction.response.send_message("📭 Nothing is being tracked right now.")
        return
    
    active = []
    recovered = []
    
    for username, meta in tracked.items():
        if meta.get("recovered"):
            recovered.append(f"**@{username}** ✅")
        else:
            active.append(f"**@{username}** ⏳ {format_elapsed(meta['start_time'])}")
    
    lines = []
    if active:
        lines.append(f"**Currently tracking ({len(active)}):**")
        lines.extend(active)
    if recovered:
        lines.append(f"\n**Previously recovered ({len(recovered)}):**")
        lines.extend(recovered[:10])
        if len(recovered) > 10:
            lines.append(f"*...and {len(recovered) - 10} more*")
    
    if len(lines) > 25:
        # Split into multiple messages if too long
        chunks = [lines[i:i+25] for i in range(0, len(lines), 25)]
        for chunk in chunks:
            await interaction.response.send_message("\n".join(chunk))
    else:
        await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="setchannel", description="Set this channel for recovery notifications")
@is_owner()
async def setchannel(interaction: discord.Interaction):
    await api_set_config("notify_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(f"✅ Notifications will be posted in {interaction.channel.mention}.")


@bot.tree.command(name="sync", description="Sync slash commands (owner only)")
@is_owner()
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        await bot.tree.sync()
        await interaction.followup.send("✅ Slash commands synced successfully!")
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}")


# ---------- Main execution ----------

if __name__ == "__main__":
    print("🚀 Starting Instagram Unban Monitor (Final Version)...")
    
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN not set.")
    if not D1_WORKER_URL or not D1_API_KEY:
        raise SystemExit("❌ D1_WORKER_URL and D1_API_KEY must be set.")
    
    # Initialize Instagram client
    session_bytes = asyncio.run(fetch_ig_session_from_d1())
    if not init_instagram_client(session_bytes):
        print("⚠️ Warning: Instagram client not initialized. Will retry on each check.")
    
    # Start keep-alive web server
    start_keep_alive()
    print("✅ Web server started on port", PORT)
    
    # Run Discord bot
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid Discord token. Please check DISCORD_TOKEN environment variable.")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")