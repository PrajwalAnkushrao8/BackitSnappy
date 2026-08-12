# BackitSnappy

A macOS desktop app that uses your personal Telegram account as backup storage
for photos, videos, and files — with auto-backup from a watched Mac folder and
from your iPhone (via iOS Shortcuts over Tailscale), and shareable albums.

## Setup

1. **Get Telegram API credentials.** Go to https://my.telegram.org/apps, log
   in with your phone number, and create an app. Note the `api_id` and
   `api_hash`.
2. **Run the app:**
   ```sh
   ./run.sh
   ```
   This creates/activates the venv, installs dependencies, and launches
   BackitSnappy.
3. On first launch, enter your `api_id`/`api_hash`, then your phone number,
   then the login code Telegram sends you (and your two-step verification
   password, if you have one set). Your session is saved to the macOS
   Keychain — you won't need to sign in again.
4. A private "BackitSnappy Storage" channel is created automatically on your
   Telegram account for backups.

## Auto-backup from your Mac

In **Settings**, set an "Auto-backup folder." Any file added to that folder
(recursively) is uploaded automatically once it's finished writing.

## Auto-backup from your iPhone (via Tailscale + iOS Shortcuts)

1. Install [Tailscale](https://tailscale.com) on both your Mac and iPhone,
   and make sure both are logged into the same tailnet.
2. In BackitSnappy's **Settings**, turn on "Allow uploads over Tailscale,"
   then restart the app.
3. In **Settings → iOS Shortcut pairing**, tap "Reveal" to see your pairing
   token.
4. In the iOS Shortcuts app, create an automation (e.g. "when I take a
   photo") with a "Get Contents of URL" action:
   - **URL:** `http://<your-mac-tailscale-ip>:8766/api/upload`
   - **Method:** POST
   - **Headers:** `X-Pairing-Token: <your pairing token>`
   - **Request Body:** Form, with a `file` field set to the photo/file

Your Mac's Tailscale IP is shown as `100.x.x.x` in the Tailscale app, or run
`tailscale ip -4` in Terminal.

## Albums

Create an album in the **Albums** tab, drag files into it, and invite another
Telegram user by username. If Telegram's privacy settings block a direct
invite (common for accounts that haven't messaged you before), BackitSnappy
falls back to a shareable invite link automatically.

## Security notes

- The local API only binds to `127.0.0.1` by default; the Tailscale listener
  (if enabled) binds only to your Mac's Tailscale IP — never your LAN or
  `0.0.0.0`.
- Every API request requires the `X-Pairing-Token` header.
- Your Telegram session, API credentials, and pairing token are stored in the
  macOS Keychain, never written to disk in plaintext.
- File size caps follow your Telegram account: 2GB, or 4GB with Telegram
  Premium — detected automatically after sign-in.
