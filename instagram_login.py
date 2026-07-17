"""
Run this ONCE, on your own device (Termux is fine, or a computer) — NOT
something you deploy to Render.

This logs into Instagram interactively using instaloader (a lightweight,
pure-Python library — installs in seconds, no compiling required), then
uploads the resulting session DIRECTLY to your Cloudflare D1 database over
the network. No copy/pasting of long text into Render is needed at all.

Use a spare/secondary Instagram account for this, not your main personal
account — automated login and lookups carry some risk of that account
getting flagged or temporarily limited by Instagram.

Usage:
    pip install instaloader requests python-dotenv
    python instagram_login.py

Requires D1_WORKER_URL and D1_API_KEY — it will read them from a .env file
in this same folder if present (the same .env your bot uses), or ask you
to paste them in if not found.
"""

import os
import base64
import getpass
import requests
from dotenv import load_dotenv
import instaloader

load_dotenv()


def get_d1_settings():
    worker_url = os.getenv("D1_WORKER_URL", "").rstrip("/")
    api_key = os.getenv("D1_API_KEY", "")
    if not worker_url:
        worker_url = input("D1_WORKER_URL (your Cloudflare Worker URL): ").strip().rstrip("/")
    if not api_key:
        api_key = getpass.getpass("D1_API_KEY (your worker's secret): ")
    return worker_url, api_key


def main():
    worker_url, api_key = get_d1_settings()

    proxy_url = os.getenv("PROXY_URL", "")
    if not proxy_url:
        proxy_url = input(
            "\nPROXY_URL (leave blank if not using one — "
            "but use the SAME proxy here as on Render, if any): "
        ).strip()

    username = input("\nInstagram username to log in with (spare account recommended): ").strip()
    password = getpass.getpass("Instagram password: ")

    L = instaloader.Instaloader(quiet=True)
    if proxy_url:
        L.context._session.proxies = {"http": proxy_url, "https": proxy_url}
        print("Using proxy for login.")

    print("\nLogging in... if Instagram sends a verification code, enter it when asked.")
    try:
        L.login(username, password)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        code = input("Enter the 2FA code Instagram sent you: ").strip()
        L.two_factor_login(code)

    print("✅ Logged in successfully.")

    session_path = "ig_session.bin"
    L.save_session_to_file(filename=session_path)

    with open(session_path, "rb") as f:
        session_bytes = f.read()
    session_b64 = base64.b64encode(session_bytes).decode("utf-8")

    print("\nUploading session to your database...")
    resp = requests.post(
        f"{worker_url}/config",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"ig_session": session_b64},
        timeout=15,
    )

    if resp.status_code == 200:
        print("✅ Session uploaded successfully. Your bot will pick it up automatically on next start.")
        print("\nNext steps:")
        print("1. In Render -> Environment, add:")
        print(f"     IG_USERNAME = {username}")
        print("     IG_PASSWORD = <the password you just typed>")
        print("   (these are just a fallback in case the session ever expires)")
        print("2. Redeploy your Render service (or just wait for it to restart).")
        print(f"\nYou can safely delete {session_path} from this device now.")
    else:
        print(f"❌ Upload failed — HTTP {resp.status_code}: {resp.text[:300]}")
        print("Double check D1_WORKER_URL and D1_API_KEY are correct.")


if __name__ == "__main__":
    main()
