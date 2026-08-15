# First-time setup

A complete walkthrough for getting BackitSnappy running for the first time.
For a quick reference once you're already set up, see the main
[README](README.md).

## Before you start: read this

**The security of everything you back up depends entirely on the security
of your Telegram account.**

BackitSnappy doesn't store your photos and videos itself — it uploads them
to private channels on *your own Telegram account*. There's no separate
BackitSnappy password, no separate BackitSnappy server holding your data.
Whoever can get into your Telegram account can see everything you've backed
up. That means:

- **Turn on Telegram's Two-Step Verification** (Telegram app → Settings →
  Privacy and Security → Two-Step Verification) if you haven't already. This
  is the single most effective thing you can do — it means your login code
  alone isn't enough for someone else to get in.
- **Use a strong, unique password** for that Two-Step Verification — not
  one reused from another account.
- **Watch for phishing.** Telegram will never ask for your login code
  outside the Telegram app itself. If a website or message asks you to type
  your code in, it's not really Telegram.
- **Periodically check Active Sessions** (Settings → Devices in the
  Telegram app) and log out anything you don't recognize.
- This app's own local pieces (the Keychain-stored session and credentials,
  the pairing token, the loopback-only listener) are all designed to keep
  your *Mac* secure — but none of that matters if your underlying Telegram
  account itself is compromised. Telegram account security is the actual
  foundation everything else sits on.

With that said, here's how to get set up.

## 1. Prerequisites

- A Mac running macOS.
- Python 3 (from [python.org](https://python.org) or Homebrew, if you don't
  already have it).

That's it — `run.sh` handles the rest, including ffmpeg (used for video
thumbnails), which is installed automatically as a Python dependency. No
separate `brew install` step needed.

## 2. Get Telegram API credentials (one-time per phone number)

Every app that talks to Telegram's API needs its own credentials — this is
Telegram's requirement, not BackitSnappy's.

1. Go to https://my.telegram.org/apps (or use the "Open my.telegram.org/apps"
   button that appears in the app's own setup screen in step 4 below — same
   link, whichever's easier).
2. Log in with your phone number and the code Telegram sends you.
3. Fill in "App title" and "Short name" with anything you like (e.g.
   "BackitSnappy") — the rest of the form can be left as defaults.
4. You'll be shown an `api_id` (a number) and an `api_hash` (a long string of
   letters/numbers). Keep this page open, or copy both values somewhere —
   you'll paste them into BackitSnappy in a minute.

BackitSnappy binds these to the phone number you sign in with and remembers
them, so **you'll only ever be asked once per number**. Sign out and back in
with the same number and it goes straight to the login code.

If you later sign in with a *different* phone number, you'll be asked for
credentials again for that number. Each `api_id` can only be bound to one
number — reusing one that already belongs to another number is refused, so two
accounts can never quietly share an app identity.

## 3. Run the app

```sh
git clone https://github.com/PrajwalAnkushrao8/BackitSnappy.git
cd BackitSnappy
./run.sh
```

This creates a Python virtual environment, installs dependencies, and
launches the app. It'll open its own window — this isn't a website, nothing
here needs a browser.

## 4. First-launch wizard

1. **Sign in** — enter your phone number first, with country code (e.g.
   `+15551234567`).
2. **Connect Telegram** — shown only if this app hasn't seen that number
   before. Paste in the `api_id` and `api_hash` from step 2. There's a
   "← Use a different number" button here if you mistyped the number.
3. **Enter code** — Telegram sends the login code as a message *inside the
   Telegram app itself* (usually to a device you're already signed into),
   not as a text message. Check there.
4. **Two-step verification** — only shown if you have it enabled (you
   should!). Enter that password.

Your session is saved to the macOS Keychain after this — you won't need to
sign in again on future launches, unless you manually log the session out
from Telegram itself (nothing is lost if that happens; you just sign back in).

## 5. What happens automatically after you sign in

- A private **"BackitSnappy Storage"** channel is created on your Telegram
  account — this is where general backups (not in any specific album) land.
- Any BackitSnappy channels **already on your account** are discovered and
  indexed, so signing in on a new Mac (or after signing out) brings your whole
  library back without re-uploading anything.
- A short, skippable **welcome tour** walks through the Albums tab and
  Settings. Replay it anytime from Settings → Help.

Only file *metadata* is indexed during this pass, not the files themselves —
so it's fast even for a large library, and your Mac's disk usage stays bounded
regardless of how much is stored in Telegram (see step 8).

## 6. Set up Mac auto-backup (optional)

Point BackitSnappy at a folder once, and never think about backing that
folder up again. In **Settings**, set an "Auto-backup folder" (e.g.
`~/Pictures/BackitSnappy`) — any file added to it, or any of its
subfolders, uploads automatically once it's finished writing to disk. No
dragging into the app required.

This works well for more than manual drops, too: point it at wherever
another app or script already saves files (a screenshots folder, a
scanner's output, an export folder), and everything that lands there gets
backed up on its own.

## 7. Turn on Automatic Photos Backup (optional)

This is the feature that actually frees up space: BackitSnappy watches your
macOS Photos library, uploads anything new to Telegram, and then removes it
from Photos.

1. Open **Settings → Automatic Photos Backup**.
2. Set **"Check for new photos every"** to whatever interval suits you
   (default 10 minutes, minimum 5).
3. Flip **"Enable Automatic Photos Backup"** on.
4. macOS will ask for **Automation permission** for Photos.app the first time
   it runs. You must allow this — the feature can't read your library
   otherwise. If you dismissed the prompt, re-enable it under **System
   Settings → Privacy & Security → Automation → BackitSnappy → Photos**;
   the Settings panel has a button that opens that page directly.

**What "deletes them from Photos" actually means:** items are moved to Photos'
own **Recently Deleted**, Apple's 30-day safety window. Nothing is destroyed
immediately, and you can restore anything from there. iCloud storage frees up
once that window passes, or right away if you empty Recently Deleted yourself.

The upload is always confirmed *before* the delete — an item is never removed
from Photos unless it's verifiably in Telegram first.

> **You'll be asked to confirm each batch.** Deletion goes through PhotoKit,
> which always shows a macOS confirmation dialog — there's no way for an app to
> suppress it. BackitSnappy deletes a whole cycle's worth of items in one
> request, so you get **one dialog per cycle** rather than one per photo. If
> you're away from the machine, the cycle simply waits; declining leaves
> everything in your library, already safely backed up.

Turning the toggle off stops the whole loop immediately. It's off by default,
and it's the only feature in the app that deletes anything from Photos.

## 8. Set your local cache limit (optional)

Your Telegram library can be far bigger than your Mac's disk — BackitSnappy
only keeps a bounded local cache, not a full copy.

**Settings** shows what the app is currently using, split into database,
thumbnails, and cached media. Set **"Max local cache size"** to whatever you're
willing to give it (default 5 GB). When cached media exceeds that, the
least-recently-opened files are deleted first; they re-download automatically
the next time you open them.

Thumbnails are never evicted — they're tiny, and they're what makes browsing
work without pulling full files.

## You're done

At this point BackitSnappy is fully set up: Telegram connected, your Storage
channel created, your existing albums indexed, and whichever backup paths you
chose running. Everything from here is day-to-day use — see
[USAGE.md](USAGE.md) for how Albums, sharing, video playback, downloads, and
sync work, plus troubleshooting.
