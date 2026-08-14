# Using BackitSnappy

Everything you can actually do with the app, once it's set up. If you
haven't installed and signed in yet, start with [SETUP.md](SETUP.md) — this
guide assumes you're already through the first-launch wizard.

## The interface

Two tabs in the sidebar:

- **Albums** — everything lives here. Every backup, organized into folders.
- **Settings** — your Telegram connection, backup automation, the iOS
  Shortcut setup, and account controls.

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
- If your Mac goes to sleep mid-upload (rare for local drag-drop, more
  relevant for the iPhone Shortcut flow below), a **persistent queue**
  resumes any not-yet-finished uploads automatically the next time the app
  starts.

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
- **Download** everything selected at once.
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

### iPhone access (Tailscale)

Turn this on to open a second listener bound to your Mac's Tailscale IP,
letting your iPhone upload directly. Requires restarting the app after
toggling it.

### iOS Shortcut setup

Once Tailscale access is on, this card shows the three values your iPhone
Shortcut needs, each with a **Copy** button:

- **Upload URL** — auto-detected from your current Tailscale IP; hit
  **Refresh** if it ever changes (e.g. after reconnecting to Tailscale).
- **Pairing Token** — tap **Reveal** first, then **Copy**. You can also
  **Rotate token** here if you ever need to invalidate the old one (any
  device using it, including your Shortcut, will need updating afterward).
- **iPhone Backup Album ID** — the auto-created album your phone's uploads
  land in by default.

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

**Log Out** signs this Mac out of Telegram and clears the local index (your
`api_id`/`api_hash` are kept, so signing back in skips straight to the
phone-number step). Nothing in Telegram itself is touched — logging back
into an account that had BackitSnappy content on it automatically rebuilds
the local index by rediscovering its existing channels. Useful for handing
the Mac to someone else, or connecting a different Telegram account.

## Good to know

- **The iPhone Shortcut backs up photos, not videos.** This isn't a
  configuration issue — video files consistently fail to attach through
  iOS Shortcuts' photo picker no matter how the Shortcut is built (see
  [Troubleshooting](#troubleshooting) for what was tried). For videos:
  connect your iPhone to your Mac and drag the video into an album directly
  in the app — that path is fully reliable.
- **File size limits follow your Telegram account**: 2GB per file normally,
  4GB with Telegram Premium — detected automatically after sign-in. A file
  over the limit fails with a clear message rather than a raw error.
- **Every install is fully isolated** — see the
  [README's security section](README.md#security) for what that means.
- **Auto-sync after a phone batch** deliberately waits ~45 seconds after the
  last upload before running, so a burst of individual photo uploads
  doesn't trigger it repeatedly mid-batch.

## Troubleshooting

Real issues found while building and testing this, most relevant to the
iOS Shortcut flow:

**"Invalid HTTP request received" / the Shortcut times out** — check the
Shortcut's Headers list for a stray empty row (a `Key`/`Text` placeholder
that wasn't actually deleted). iOS Shortcuts sometimes sends it as a real,
blank header, which breaks the request before it's even parsed. Delete the
empty row.

**"Invalid or missing pairing token"** — the token got truncated when
copied into the Shortcut's header value (often cut off at the first
hyphen). Re-copy the full token from Settings → Reveal, and make sure you
delete the old value completely before pasting, rather than pasting over
part of it.

**A photo/video won't attach — "Field required" or an empty upload** — most
often a Photos permission issue: in iPhone Settings → Privacy & Security →
Photos → Shortcuts, make sure it's set to **Full Access**, not "Limited/
Selected Photos." A Shortcut that only has partial library access can fail
to hand over files it doesn't have full access to.

**Videos won't attach through the Shortcut at all** — this is expected;
see [Good to know](#good-to-know) above. What we actually tried, in order,
against a real 44MB video that consistently failed: confirming the `file`
Form field was correctly bound to the selected item (it was), confirming
Shortcuts' photo picker included videos (it did), fully downloading the
video in the Photos app first in case "Optimize iPhone Storage" left it as
an iCloud placeholder (still failed), and inserting an explicit "Get File
of Type" conversion step before the upload (produced a broken output, or
an outright conversion error). Every attempt still resulted in either no
real data being sent or a corrupted attachment — this points to a genuine
iOS Shortcuts limitation with video attachments in `Get Contents of URL`
requests, not something fixable through more configuration. Connect your
iPhone to your Mac and drag the video into the album directly instead;
that path always works.

**Uploads seem to stall for hours overnight** — if your Mac goes to sleep,
the browser-driven upload loop (for local drag-drop uploads) pauses with
it. Keeping the lid open/display awake (`caffeinate -d` in Terminal, or just
not letting it sleep) avoids this. Phone uploads via the Shortcut don't have
this problem — BackitSnappy automatically keeps the Mac awake for the
duration of any phone-sourced upload.
