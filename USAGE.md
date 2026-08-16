# Using BackitSnappy

Everything you can actually do with the app, once it's set up. If you
haven't installed and signed in yet, start with [SETUP.md](SETUP.md) — this
guide assumes you're already through the first-launch wizard.

## The interface

Two tabs in the sidebar:

- **Albums** — everything lives here. Every backup, organized into folders.
- **Settings** — backup automation (watched folder and Photos), local storage
  usage, sync, and account controls.

## Albums

### Browsing

Open the **Albums** tab and you'll see a folder grid — each album is a
private Telegram channel, shown as a folder icon (or a phone icon for the
auto-created **iPhone Backup** album, so it's easy to spot). Double-click any
folder to open it into a Photos.app-style grid of everything inside.

### Creating an album

Click **+ New Folder** at the top of the Albums view, name it, and it's
created immediately — both locally and as a new private Telegram channel.

### Uploading

Inside an album, there's no separate "upload" button to hunt for — just
**drag files onto the grid**, or click the **+** button in the top-right to
pick files via the native file picker.

- **Large batches** (more than 10 files, or over 2GB total) get a
  confirmation prompt first, showing the count and total size before
  anything starts.
- Files upload in **batches of 25** — this caps how many temp copies sit on
  disk at once. For very large batches (300+ files), you'll see a running
  "Batch X of Y — N/Total uploaded…" summary instead of one flat counter.
- A **Pause** button appears on that summary for large batches — pausing
  finishes whatever's currently mid-flight, then holds before starting the
  next one, so nothing gets left half-uploaded.
- Duplicate content (by file hash, not filename) is **automatically
  deduplicated** — dropping the same file twice, or a file that's already in
  another album, doesn't re-upload it or waste Telegram bandwidth.
- If your Mac goes to sleep mid-upload, or the app quits, a **persistent
  queue** resumes any not-yet-finished uploads automatically the next time
  the app starts.

### Viewing a file

Click any thumbnail to open it full-size in the lightbox:

- **←/→** arrow keys (or the on-screen chevrons) move between files.
- **Download** saves to `~/Downloads/BackitSnappy/` (Finder-style collision
  handling — `photo.jpg`, `photo (1).jpg`, etc.).
- **Save As…** opens the native macOS save panel to pick a different
  location.
- **Delete** removes it from both Telegram and the local index.
- **Esc**, clicking outside the image, or the **Close** button all dismiss
  it.

### Watching a video

Videos **stream** rather than download. Click one and it starts playing right
away, pulling only the parts needed as you watch — you can open a 2GB clip
without waiting for a 2GB transfer, and seeking just fetches from the new
position. Nothing is written to your cache by streaming.

Playback starts **muted** so opening a video never blasts audio unexpectedly;
use the player's own volume control to unmute.

Closing the lightbox stops the transfer immediately. (Images still download
fully before display — they're small enough that a complete local copy is
cheaper than streaming, and it means reopening one needs no network at all.)

Videos without a thumbnail get one generated in the background after your
first sign-in on this version — see [Good to know](#good-to-know).

### Downloading

Every thumbnail also has a small download button that appears on hover — no
need to open the lightbox first for a quick save. Downloads show up in a
floating tray in the corner with live progress, and each one has a **×**
button: while it's still running, that cancels it; once it's done (or
failed), the same button dismisses the row.

### Selecting multiple files

The **Select** button appears in an album's toolbar (only when the album
actually has content — an empty album won't show it). Turn it on to:

- **Select All** the currently-visible files.
- **Download** everything selected at once — asks where to save once, via the
  native folder picker, then saves every file there (Finder-style collision
  handling for name clashes).
- **Delete** everything selected at once — capped at **10 files per bulk
  delete**. For more than that, delete them directly in the Telegram app
  instead, then hit **Sync with Telegram** in Settings to bring the local
  index back in line.

### Sharing an album

Tap the floating **invite button** (bottom-right, inside an album) and enter
a Telegram username. BackitSnappy tries a direct invite first; if the
other person's privacy settings block that (common for accounts that
haven't messaged you before), it automatically falls back to generating a
shareable invite link instead.

### Deleting an album

Right-click a folder in the top-level Albums grid for a context menu with
**Delete Album** — this permanently destroys the underlying Telegram channel
and everything in it. There's a confirmation prompt first; there's no undo
after that.

## Settings

### Auto-backup folder

Point this at any folder on your Mac to have anything added to it (recursed
into subfolders too) uploaded automatically — see the
[README](README.md#auto-backup-from-your-mac) for the full explanation of
how the file-system watching works.

### Automatic Photos Backup

Polls your macOS Photos library on a schedule, uploads anything new, and then
moves it to Photos' **Recently Deleted** (Apple's 30-day safety window) — so
your iCloud storage frees up without anything being destroyed outright.

- **Status** shows whether a poll cycle is running right now.
- **Automation permission** shows whether macOS has granted access to
  Photos.app, with a button that opens the right System Settings page.
- **Check for new photos every** sets the interval (5–120 minutes).
- **Remove from Photos once they're older than** sets the age window. Backup is
  never delayed by this — it only gates removal, so recent photos stay on your
  iPhone. 0 removes as soon as a backup is confirmed.
- **Enable Automatic Photos Backup** is the master switch. Off by default.
- **Recent Photos Backups** below lists what's been backed up so far.

Under **Free Up iCloud Storage**, "Remove backed-up photos now" clears
everything already in Telegram regardless of age, for when you want the space
back sooner than the window allows.

The upload is confirmed before anything is deleted — an item never leaves your
library unless it's verifiably in Telegram first. macOS shows one confirmation
dialog per cycle before deleting (see
[SETUP.md §7](SETUP.md#7-turn-on-automatic-photos-backup-optional)).

### Storage used on this Mac

Shows what BackitSnappy is using locally, split into database, thumbnails, and
cached media, plus a **Max local cache size** you can set.

Your Telegram library can be much larger than your disk — only the cache lives
here. Once it exceeds the limit, the least-recently-opened files are removed
first and re-download on demand. Thumbnails are never evicted.

### Sync

**Sync with Telegram** reconciles the local index against what's actually
in Telegram — removing entries for anything deleted directly in Telegram
(outside the app) and importing anything added directly in Telegram (e.g. a
photo you sent manually to one of the channels). This also runs
automatically once at app launch, and automatically after a phone upload
batch finishes.

### Help

- **Replay welcome tour** — brings back the short first-run walkthrough
  anytime.
- **Open my.telegram.org/apps** — same page you needed during setup, handy
  if you ever need to check your app's details again.

### Account

**Log Out** signs this Mac out of Telegram. Nothing in Telegram itself is
touched, and — as of v0.2.0 — **your local index is kept**, so signing back in
with the same number is instant rather than triggering a full re-sync. Your
`api_id`/`api_hash` stay bound to that number too, so you go straight to the
login code.

Signing in with a **different** Telegram account is detected automatically, and
*that* is when the local index is cleared and account-scoped settings
(auto-backup folder, Automatic Photos Backup) are reset to off. This is
deliberate: those settings upload and delete on their own, and must never
silently carry over to someone else's account.

## Good to know

- **Videos stream; images download.** Opening a video starts playback almost
  immediately and transfers only what you watch. Images are fetched in full
  first, so reopening one later needs no network.
- **Thumbnails are backfilled once.** After upgrading to v0.2.0, the app makes
  a one-time background pass over videos that have no thumbnail, reading just
  the start and end of each file rather than downloading it. It's paced
  deliberately slowly so it never competes with what you're actively doing,
  and it resumes safely if you quit mid-way.
- **File size limits follow your Telegram account**: 2GB per file normally,
  4GB with Telegram Premium — detected automatically after sign-in. A file
  over the limit fails with a clear message rather than a raw error.
- **Duplicate detection doesn't need the whole file.** Files are fingerprinted
  from their first and last 64KB plus exact byte size, so re-adding something
  already stored is recognized without re-downloading it.
- **Every install is fully isolated** — see the
  [README's security section](README.md#security) for what that means.

## Troubleshooting

**Automatic Photos Backup uploads but never deletes** — most likely nobody
answered the confirmation dialog. macOS requires one for every PhotoKit
deletion and it can't be suppressed, so a cycle that runs while you're away
uploads everything and then waits. Nothing is at risk: the uploads are already
logged and counted, and the items stay in your library until a later cycle's
dialog is approved.

If you're on a build older than v0.2.1, this instead showed up as an AppleEvent
`-10000` error. That was a real bug — Photos.app's scripting interface only
supports deleting *albums and folders*, never individual media items, so the
delete could never have worked. It now goes through PhotoKit instead.

**Automatic Photos Backup does nothing at all** — check **Automation
permission** in Settings. If it says denied or unchecked, macOS never granted
access to Photos.app. Open **System Settings → Privacy & Security → Automation
→ BackitSnappy → Photos** (there's a button in Settings that goes straight
there) and enable it, then toggle the feature off and on.

**A video stalls or won't start playing** — playback pulls from Telegram live,
so it depends on Telegram's responsiveness at that moment. Close and reopen the
video to start a fresh transfer. If it happens consistently for one specific
file, use **Download** instead and play it locally.

**Thumbnails are missing for some videos** — the one-time backfill described
above may still be running; it takes a few hours for a large library. Some
files genuinely can't produce one from a partial read, in which case they stay
as icon-only until you open them once.

**Uploads seem to stall for hours overnight** — if your Mac sleeps, in-progress
uploads pause with it. BackitSnappy keeps the Mac awake automatically during
uploads it initiates, but if you're driving a long batch manually, keeping the
display awake (`caffeinate -d` in Terminal) avoids this.

**The local index doesn't match Telegram** — hit **Sync with Telegram** in
Settings. This reconciles both directions: it drops entries for anything
deleted directly in Telegram, and imports anything added there outside the app.
