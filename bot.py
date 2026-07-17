"""
Instagram Unban Monitor — Discord Bot (FINAL FIXED)
--------------------------------------
Fixed Instagram authentication with proper session handling and 2FA support.
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
    TooManyRequestsException,
    BadResponseException,
    InvalidArgumentException,
    TwoFactorAuthRequiredException
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
pending_2fa = {}  # Store 2FA sessions


def create_instaloader_instance():
    """Create a fresh Instaloader instance with optimal settings"""
    loader = instaloader.Instaloader(
        quiet=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=3,
        request_timeout=30,
    )
    return loader


# ---------- Instagram authentication with 2FA support ----------

async def authenticate_instagram(username: str = None, password: str = None, twofa_code: str = None) -> bool:
    """Authenticate with Instagram using credentials with 2FA support"""
    global L, pending_2fa
    
    username = username or IG_USERNAME
    password = password or IG_PASSWORD
    
    print(f"🔄 Authenticating with Instagram as @{username}...")
    
    # Create new instance
    L = create_instaloader_instance()
    
    # Set proxy if configured
    if PROXY_URL:
        try:
            L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
            print(f"Using proxy: {PROXY_URL}")
        except Exception as e:
            print(f"Proxy setting failed: {e}")
    
    try:
        # Try to login
        L.login(username, password)
        
        # Verify login worked
        test_username = L.test_login()
        if test_username:
            print(f"✅ Successfully logged in as @{test_username}")
            
            # Save session for future use
            session_data = await get_session_bytes(L)
            if session_data:
                try:
                    await api_set_config("ig_session", base64.b64encode(session_data).decode('ascii'))
                    await api_set_config("ig_username", username)
                    print("✅ Session saved to D1")
                except Exception as e:
                    print(f"Could not save session to D1: {e}")
            
            return True
        else:
            print("❌ Login verification failed")
            return False
            
    except TwoFactorAuthRequiredException:
        print("📱 Two-factor authentication required!")
        # Store session for 2FA
        pending_2fa['context'] = L.context
        pending_2fa['username'] = username
        return False
        
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return False

async def authenticate_with_2fa(code: str) -> bool:
    """Complete 2FA authentication"""
    global L, pending_2fa
    
    if 'context' not in pending_2fa:
        return False
    
    try:
        # Submit 2FA code
        L.context.two_factor_login(code)
        
        # Verify login
        test_username = L.test_login()
        if test_username:
            print(f"✅ 2FA successful! Logged in as @{test_username}")
            
            # Save session
            session_data = await get_session_bytes(L)
            if session_data:
                try:
                    await api_set_config("ig_session", base64.b64encode(session_data).decode('ascii'))
                    await api_set_config("ig_username", pending_2fa['username'])
                    print("✅ Session saved to D1")
                except Exception as e:
                    print(f"Could not save session to D1: {e}")
            
            pending_2fa.clear()
            return True
        else:
            print("❌ 2FA verification failed")
            return False
            
    except Exception as e:
        print(f"❌ 2FA failed: {e}")
        return False

async def get_session_bytes(loader) -> Optional[bytes]:
    """Extract session bytes from Instaloader"""
    try:
        fd, path = tempfile.mkstemp(prefix='ig_session_', suffix='.dat')
        os.close(fd)
        
        try:
            loader.save_session_to_file(path)
            with open(path, 'rb') as f:
                session_data = f.read()
            return session_data
        finally:
            try:
                os.unlink(path)
            except:
                pass
    except Exception as e:
        print(f"Failed to extract session: {e}")
        return None

def load_session_from_bytes(session_bytes: bytes, username: str) -> bool:
    """Load Instagram session from bytes"""
    global L
    
    try:
        fd, path = tempfile.mkstemp(prefix='ig_session_', suffix='.dat')
        os.write(fd, session_bytes)
        os.close(fd)
        
        try:
            L.load_session_from_file(username, filename=path)
            return True
        finally:
            try:
                os.unlink(path)
            except:
                pass
    except Exception as e:
        print(f"Failed to load session: {e}")
        return False

async def ensure_instagram_auth() -> bool:
    """Ensure Instagram is authenticated, re-login if needed"""
    global L
    
    # If we have a loader, check if it's still valid
    if L is not None:
        try:
            test = L.test_login()
            if test:
                return True
            else:
                print("Session expired, re-authenticating...")
        except:
            print("Session check failed, re-authenticating...")
    
    # Try to load session from D1 first
    try:
        config = await api_get_config()
        session_b64 = config.get("ig_session")
        session_username = config.get("ig_username", IG_USERNAME)
        
        if session_b64 and session_username:
            session_bytes = base64.b64decode(session_b64)
            
            # Create new instance
            L = create_instaloader_instance()
            
            # Set proxy if configured
            if PROXY_URL:
                try:
                    L.context._session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
                except:
                    pass
            
            # Load session
            if load_session_from_bytes(session_bytes, session_username):
                test = L.test_login()
                if test:
                    print(f"✅ Session loaded successfully as @{test}")
                    return True
                else:
                    print("Session loaded but verification failed")
    except Exception as e:
        print(f"Session load from D1 failed: {e}")
    
    # Fall back to login with credentials
    if IG_USERNAME and IG_PASSWORD:
        return await authenticate_instagram()
    
    print("❌ No valid authentication method available")
    return False


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
        print("❌ Instaloader not initialized")
        return None
    
    try:
        print(f"🔍 Checking @{username}...")
        
        # Try to get profile
        profile = instaloader.Profile.from_username(L.context, username)
        
        result = {
            "username": profile.username,
            "full_name": profile.full_name or "",
            "followers": profile.followers,
            "following": profile.followees,
            "posts": profile.mediacount,
            "profile_pic_url": profile.profile_pic_url or "",
            "is_verified": bool(profile.is_verified),
        }
        print(f"✅ @{username} is live - {result['followers']} followers")
        return result
        
    except ProfileNotExistsException:
        print(f"❌ @{username} - Profile not found (banned/unavailable)")
        return None
    except TooManyRequestsException:
        print(f"⏳ @{username} - Rate limited, try again later")
        return None
    except LoginRequiredException:
        print(f"⚠️ @{username} - Login required, session expired")
        return None
    except Exception as e:
        print(f"❌ @{username} - Unexpected error: {type(e).__name__}: {e}")
        return None

async def check_instagram_status(username: str) -> Optional[Dict[str, Any]]:
    """Returns None if unreachable/banned, else a dict of profile stats."""
    try:
        if not await ensure_instagram_auth():
            print(f"⚠️ Could not authenticate for @{username}")
            return None
        
        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_check_instagram, username), 
            timeout=30
        )
        return result
    except asyncio.TimeoutError:
        print(f"⏳ @{username} - Check timed out (30s)")
        return None
    except Exception as e:
        print(f"❌ @{username} - Async check error: {e}")
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

        unrecovered = {u: m for u, m in tracked.items() if not m.get("recovered")}
        
        if unrecovered:
            print(f"📊 Checking {len(unrecovered)} accounts...")
        
        for username, meta in unrecovered.items():
            info = await check_instagram_status(username)
            if info is not None:
                embed = build_recovery_embed(info, meta["start_time"])
                try:
                    await channel.send(content=f"📢 **@{username}** is back!", embed=embed)
                    await api_mark_recovered(username, datetime.now(timezone.utc).isoformat())
                    print(f"✅ Recovered and notified: @{username}")
                except discord.HTTPException as e:
                    print(f"Failed to send Discord message for @{username}: {e}")
                
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
        
        print("🔐 Testing Instagram authentication...")
        if await ensure_instagram_auth():
            print("✅ Instagram authentication successful")
        else:
            print("❌ Instagram authentication failed")
            print(f"   Username: {IG_USERNAME}")
            print(f"   Password: {'Set' if IG_PASSWORD else 'Not set'}")
    except Exception as e:
        print(f"Error in on_ready: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[command error] /{interaction.command.name if interaction.command else '?'}: {error!r}")
    
    if isinstance(error, app_commands.CheckFailure):
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
    
    auth_success = await ensure_instagram_auth()
    if not auth_success:
        await interaction.followup.send(
            f"❌ Instagram authentication failed.\n"
            f"Please check credentials and try again.\n"
            f"If you have 2FA, use /setcreds2fa to authenticate."
        )
        return
    
    info = await check_instagram_status(username)
    if info:
        await interaction.followup.send(
            f"✅ **@{username}** is **LIVE!**\n"
            f"• Followers: {info['followers']:,}\n"
            f"• Following: {info['following']:,}\n"
            f"• Posts: {info['posts']:,}\n"
            f"• Verified: {'✅' if info.get('is_verified') else '❌'}"
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


@bot.tree.command(name="setcreds", description="Update Instagram credentials")
@app_commands.describe(
    username="Instagram username",
    password="Instagram password"
)
@is_owner()
async def setcreds(interaction: discord.Interaction, username: str, password: str):
    await interaction.response.defer(thinking=True)
    
    # Update environment variables
    os.environ["IG_USERNAME"] = username
    os.environ["IG_PASSWORD"] = password
    
    global IG_USERNAME, IG_PASSWORD
    IG_USERNAME = username
    IG_PASSWORD = password
    
    # Try authentication
    if await authenticate_instagram(username, password):
        await interaction.followup.send(
            f"✅ Credentials updated and authenticated successfully!\n"
            f"Logged in as: @{username}"
        )
    else:
        # Check if 2FA is required
        if pending_2fa:
            await interaction.followup.send(
                f"📱 **2FA Required!**\n"
                f"Please use `/setcreds2fa code:YOUR_CODE` to complete authentication.\n"
                f"Check your authenticator app for the code."
            )
        else:
            await interaction.followup.send(
                f"❌ Authentication failed.\n"
                f"Please check username and password.\n"
                f"If you have 2FA enabled, please use /setcreds2fa after setting credentials."
            )


@bot.tree.command(name="setcreds2fa", description="Complete Instagram authentication with 2FA code")
@app_commands.describe(code="The 2FA code from your authenticator app")
@is_owner()
async def setcreds2fa(interaction: discord.Interaction, code: str):
    await interaction.response.defer(thinking=True)
    
    if not pending_2fa:
        await interaction.followup.send(
            "❌ No pending 2FA authentication.\n"
            "Please use `/setcreds` first."
        )
        return
    
    if await authenticate_with_2fa(code):
        await interaction.followup.send(
            f"✅ 2FA authentication successful!\n"
            f"Logged in as: @{pending_2fa.get('username', 'unknown')}"
        )
        pending_2fa.clear()
    else:
        await interaction.followup.send(
            f"❌ 2FA authentication failed.\n"
            f"Please check your code and try again."
        )


@bot.tree.command(name="authstatus", description="Check Instagram authentication status")
@is_owner()
async def authstatus(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    
    status = await ensure_instagram_auth()
    
    if status:
        try:
            test = L.test_login() if L else None
            await interaction.followup.send(
                f"✅ **Instagram is authenticated!**\n"
                f"Logged in as: @{test if test else IG_USERNAME}\n"
                f"Proxy: {'✅' if PROXY_URL else '❌'}\n"
                f"2FA pending: {'✅' if pending_2fa else '❌'}"
            )
        except:
            await interaction.followup.send(
                f"✅ Instagram is authenticated!\n"
                f"Username: {IG_USERNAME}\n"
                f"Proxy: {'✅' if PROXY_URL else '❌'}"
            )
    else:
        await interaction.followup.send(
            f"❌ **Instagram authentication failed.**\n"
            f"Username: {IG_USERNAME}\n"
            f"Password: {'✅ Set' if IG_PASSWORD else '❌ Not set'}\n"
            f"Proxy: {'✅ Set' if PROXY_URL else '❌ Not set'}\n"
            f"2FA pending: {'✅' if pending_2fa else '❌'}\n\n"
            f"Use `/setcreds` to update credentials.\n"
            f"If you have 2FA, use `/setcreds2fa` after setting credentials."
        )


# ---------- Main execution ----------

if __name__ == "__main__":
    print("🚀 Starting Instagram Unban Monitor...")
    print(f"📋 Using username: {IG_USERNAME}")
    print(f"🔑 Password: {'Set' if IG_PASSWORD else 'Not set'}")
    print(f"🌐 Proxy: {'Set' if PROXY_URL else 'Not set'}")
    
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN not set.")
    if not D1_WORKER_URL or not D1_API_KEY:
        raise SystemExit("❌ D1_WORKER_URL and D1_API_KEY must be set.")
    
    start_keep_alive()
    print(f"✅ Web server started on port {PORT}")
    
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid Discord token.")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")