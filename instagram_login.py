"""
Run this ONCE, on your own device (phone Termux is fine, or a computer) —
NOT something you deploy to Render. This logs into Instagram interactively
(so it can handle any 2FA/verification prompt normally) and saves a session
file you'll then upload to Render as an environment variable.

Use a spare/secondary Instagram account for this, not your main personal
account — automated login and lookups carry some risk of that account
getting flagged or temporarily limited by Instagram.

Usage:
    pip install instagrapi
    python instagram_login.py

If Instagram sends a verification code (email/SMS), instagrapi will pause
and ask you to type it in right here — just enter it when prompted.
"""

import getpass
import json
import base64
from instagrapi import Client

def main():
    username = input("Instagram username to log in with (spare account recommended): ").strip()
    password = getpass.getpass("Instagram password: ")

    cl = Client()
    print("\nLogging in... if Instagram sends a verification code, enter it when asked.")
    cl.login(username, password)

    cl.dump_settings("ig_session.json")
    print("\n✅ Logged in. Session saved to ig_session.json")

    with open("ig_session.json", "r") as f:
        session_json = f.read()
    b64 = base64.b64encode(session_json.encode("utf-8")).decode("utf-8")

    with open("ig_session_b64.txt", "w") as f:
        f.write(b64)

    print("\n✅ Base64-encoded session saved to ig_session_b64.txt")
    print("\nNext steps:")
    print("1. Open ig_session_b64.txt and copy its full contents")
    print("2. On Render -> your service -> Environment, add:")
    print("     IG_SESSION_B64 = <paste the contents here>")
    print("3. Also add these two as a fallback in case the session expires later:")
    print(f"     IG_USERNAME = {username}")
    print("     IG_PASSWORD = <the password you just typed>")
    print("\nKeep ig_session.json and ig_session_b64.txt private — delete them")
    print("from this device once you've copied the value into Render.")


if __name__ == "__main__":
    main()
