# BackitSnappy

A macOS desktop app that uses your personal Telegram account as backup storage
for photos, videos, and files — with auto-backup from a watched Mac folder and
from your iPhone (via iOS Shortcuts over Tailscale), and shareable albums.

Every install is fully independent — see [Security](#security) below. Before
you set up, note that **the security of everything you back up depends
entirely on the security of your Telegram account** — see the note in
[Security](#security) for what that means in practice.

**Docs**: [SETUP.md](SETUP.md) for first-time setup from scratch, [USAGE.md](USAGE.md)
for how to actually use it day-to-day (Albums, sharing, Settings, and a
troubleshooting section) once you're up and running.

## About

Cloud photo storage keeps getting more expensive, and most of it locks your
own files behind someone else's subscription. BackitSnappy takes a different
approach: it turns Telegram — a service you already have a free account
on, with no meaningful storage cap — into your personal backup destination,
with a real native Mac app on top instead of a chat interface. Your files
live in private channels on *your own* Telegram account, not on a
BackitSnappy server, so there's nothing centralized to shut down, get
breached, or start charging for later.

See it in more detail on the [landing page](https://prajwalankushrao8.github.io/BackitSnappy-site/).

## Setup

**New here? See [SETUP.md](SETUP.md) for a full first-time walkthrough**,
including the security setup you should do before backing anything up. Quick
version:

1. **Get Telegram API credentials.** Go to https://my.telegram.org/apps, log
   in with your phone number, and create an app. Note the `api_id` and
   `api_hash`. (There's a button to jump straight to this page from inside
   the app's own setup screen too.)
2. **Run the app:**
   ```sh
   ./run.sh
   ```
   This creates/activates the venv and installs dependencies — including a
   bundled ffmpeg (for video thumbnails), so there's nothing to install
   separately — then launches BackitSnappy.
3. On first launch, enter your `api_id`/`api_hash`, then your phone number,
   then the login code Telegram sends you (and your two-step verification
   password, if you have one set). Your session is saved to the macOS
   Keychain — you won't need to sign in again.
4. A private "BackitSnappy Storage" channel and an "iPhone Backup" album are
   created automatically on your Telegram account the first time you sign in.
5. A short, skippable welcome tour walks through Albums and Settings the
   first time you open the app. Replay it anytime from **Settings → Help →
   Replay welcome tour**.

## Auto-backup from your Mac

Point BackitSnappy at a folder and forget about it — anything that lands in
it gets backed up with zero manual steps.

In **Settings**, set an "Auto-backup folder" (e.g. `~/Pictures/BackitSnappy`).
Any file added to it — including in subfolders, watched recursively — is
uploaded automatically, with no dragging into the app required. It uses
real file-system events (not polling), and waits until a file is fully
finished writing before uploading, so it never grabs a half-copied file.

This is useful for more than manual drops: point it at wherever another app
or script already saves things — a screenshot folder, a scanner's output
folder, an export folder from some other tool — and everything that shows up
there gets backed up automatically. It's also a simpler alternative to the
iPhone Shortcut flow below if you only need *this Mac's* files backed up,
without touching Tailscale at all.

## Auto-backup from your iPhone (via Tailscale + iOS Shortcuts)

> **Photos only, not videos** — see the note below. For the full walkthrough
> (Tailscale setup on both devices, step by step) see
> [SETUP.md §7](SETUP.md#7-set-up-iphone-auto-backup-optional); this is the
> quick version.

1. Install [Tailscale](https://tailscale.com) on both your Mac and iPhone,
   signed into the same account on both so they land on the same tailnet.
2. In BackitSnappy's **Settings**, turn on "Allow uploads over Tailscale,"
   then restart the app.
3. Open **Settings → iOS Shortcut setup**. This one card has everything your
   Shortcut needs, each with a Copy button:
   - **Upload URL** — auto-detected from your Mac's current Tailscale IP; hit
     "Refresh" if it ever changes.
   - **Pairing Token** — tap "Reveal" first, then "Copy."
   - **iPhone Backup Album ID** — the auto-created album your phone uploads
     land in; open it under the **Albums** tab to browse them.
4. **Import the shortcut:** https://www.icloud.com/shortcuts/cf76c9d6cbb14d8da6297d0a95ff15be —
   tap **Add Shortcut**, then **edit it once** to paste your own Upload URL,
   Pairing Token, and Album ID into the placeholder fields (it ships with
   placeholder text, not a live prompt — see
   [SETUP.md §7e](SETUP.md#7e-import-and-set-up-the-shortcut) for exactly
   which fields, with a screenshot). It remembers your values after that.

   If you'd rather build it by hand, create a "Get Contents of URL" action:
   - **URL:** the Upload URL from step 3
   - **Method:** POST
   - **Headers:** `X-Pairing-Token: <your pairing token>`
   - **Request Body:** Form, with a `file` field set to the photo and an
     `album_id` field set to the album ID from step 3

While a phone upload is in progress, BackitSnappy automatically keeps your
Mac awake (with a notification) so the transfer doesn't get interrupted by
sleep, and automatically syncs with Telegram once the batch settles.

**Videos reliably fail to attach through the Shortcut** — this was tested
thoroughly (correct field binding, full Photos access, forcing local
download, explicit file-type conversion — all still failed) and looks like
a genuine iOS Shortcuts limitation, not a BackitSnappy one. For videos,
connect your iPhone to your Mac and drag the file into the album directly
instead — that path is fully reliable. See
[USAGE.md's troubleshooting section](USAGE.md#troubleshooting) for the
full detail.

## Albums

Create an album in the **Albums** tab, drag files into it, and invite another
Telegram user by username. If Telegram's privacy settings block a direct
invite (common for accounts that haven't messaged you before), BackitSnappy
falls back to a shareable invite link automatically. Each album opens into a
Photos.app-style grid — click any item to view it full-size, download it, or
delete it.

## Security

> **The security of everything you back up depends entirely on the security
> of your Telegram account.** BackitSnappy doesn't hold your data itself —
> it uploads to private channels on *your own* Telegram account, with no
> separate BackitSnappy password or server in between. Anyone who can get
> into your Telegram account can see everything you've backed up. Turn on
> [Two-Step Verification](https://telegram.org/faq#two-step-verification) in
> Telegram's own settings before relying on this — see
> [SETUP.md](SETUP.md#before-you-start-read-this) for the full rundown.

- **Every install is fully independent.** Your own Telegram session, your own
  Tailscale network, your own local database — nothing is shared or
  centralized between users of this project. Running BackitSnappy doesn't
  connect you to anyone else's instance in any way.
- The local API only binds to `127.0.0.1` by default; the Tailscale listener
  (if enabled) binds only to your Mac's Tailscale IP — never your LAN or
  `0.0.0.0`.
- Every API request requires the `X-Pairing-Token` header.
- Your Telegram session, API credentials, and pairing token are stored in the
  macOS Keychain, never written to disk in plaintext.
- File size caps follow your Telegram account: 2GB, or 4GB with Telegram
  Premium — detected automatically after sign-in.

## License

AGPL-3.0 — see [LICENSE](LICENSE).
