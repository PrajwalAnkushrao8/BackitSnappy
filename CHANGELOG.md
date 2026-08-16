# Changelog

## v0.2.2

### Added

- **Photos are now backed up and removed on separate schedules.** Everything
  uploads as soon as it's found, but an item is only removed from the Photos
  library once it's older than a configurable window (default 30 days).

  This exists because iCloud Photos is a sync service, not a backup one: there
  is one library mirrored across devices, so deleting on the Mac also deletes
  from the iPhone, and Apple exposes no way to drop a photo from iCloud while
  keeping it on the phone. Delaying removal is the only way to have recent
  photos stay on your phone while still reclaiming space from the long tail.

- **"Remove backed-up photos now"** in Settings — deletes everything already
  confirmed in Telegram, ignoring the age window, for reclaiming space on
  demand.

### Changed

- Each poll cycle now re-examines the whole backlog of backed-up-but-not-removed
  items rather than only that cycle's uploads, so a photo is cleared out on the
  cycle it ages past the window, with no extra scheduling.

## v0.2.1

Fixes the two Automatic Photos Backup problems v0.2.0 shipped with: photos
were never actually removed from the library, and the "backed up so far"
counter sat at zero even while uploads were succeeding.

### Fixed

- **Deleting from Photos now works.** v0.2.0 failed on every item with an
  AppleEvent `-10000` error, which was documented as a known issue. The cause
  turned out to be structural rather than a bug in the script: Photos.app's
  scripting dictionary defines `delete` as *"Only albums and folders can be
  deleted,"* with a direct parameter accepting exactly the types `album` and
  `folder`. A media item is not an accepted type, so the handler rejected it —
  no amount of rewriting that AppleScript could ever have worked.

  Deletion now goes through PhotoKit (`PHAssetChangeRequest.deleteAssets:`)
  instead. Export and listing stay on AppleScript, since that's what exposes
  `export ... usingOriginals` for forcing an iCloud original to download. The
  two interfaces identify items with the same string — a Photos scripting `id`
  and a PhotoKit `localIdentifier` are identical — so nothing already stored
  needed migrating.

- **"Backed up so far" counts real uploads again.** The audit-log row was only
  written *after* a successful delete, so the broken delete step above kept the
  counter pinned at zero and, worse, kept every already-uploaded item looking
  "new" to the next poll cycle. Rows are now written the moment an upload is
  confirmed, with the deletion timestamp filled in separately afterward.

### Changed

- **Deletion is batched per poll cycle, and macOS will ask you to confirm.**
  PhotoKit shows a confirmation dialog for every change request and an
  unbundled app cannot suppress it, so a cycle's items are deleted in a single
  request — one dialog per cycle instead of one per photo. Declining it leaves
  everything in the library, still backed up and still counted.

### Added

- `pyobjc-framework-Photos` dependency, required for the PhotoKit deletion path.

## v0.2.0

The release where BackitSnappy becomes a place you actually browse your
library, not just a place files go. Videos play instantly instead of
downloading, your Photos library can back itself up and free the space, and
your Mac's disk usage stays bounded no matter how large the library gets.

**Headline:** unlimited storage for photos and videos, in original quality.
Nothing is transcoded, re-compressed, or stripped on the way up.

### Added

- **Automatic Photos Backup.** Polls your macOS Photos library on a schedule,
  uploads new items, and — only after the upload is confirmed — moves them to
  Photos' Recently Deleted, so iCloud storage frees up. Off by default, with a
  live status readout, an Automation-permission check, and an audit log of
  everything backed up. Replaces the iCloud Offload Folder feature.
- **Video streaming.** Videos play immediately, served from Telegram over HTTP
  range requests instead of being downloaded first. Seeking fetches from the
  new position; closing the player cancels the transfer. Nothing is written to
  the local cache.
- **Thumbnails from partial reads.** Videos whose metadata sits at the end of
  the file (typical of camera originals) now get thumbnails by fetching just
  the head and tail and splicing them, rather than downloading the whole file.
  A one-time background pass backfills existing videos.
- **A local cache cap.** Settings shows disk usage broken down by database,
  thumbnails, and cached media, with a configurable maximum. Over the limit,
  least-recently-used files are evicted and re-download on demand — so a
  library far larger than your disk works fine.
- **Per-phone-number API credentials.** Your `api_id`/`api_hash` are bound to
  the phone number you sign in with, so you're only asked once per number. An
  `api_id` already bound to a different number is refused, so two accounts can
  never silently share an app identity.

### Changed

- **Login now asks for your phone number first**, and only requests API
  credentials if that number hasn't been seen before. Includes a "use a
  different number" way back.
- **Logging out no longer deletes your local index.** Signing back in with the
  same number is instant. A genuine *account switch* is detected at login and
  is what triggers the index wipe and the reset of account-scoped settings —
  which is the case that actually matters, since auto-upload must never carry
  over to a different account.
- **Sync indexes metadata instead of downloading everything.** Files are
  fingerprinted from their first and last 64KB plus exact size, so a full
  rebuild no longer means pulling every byte. Imports also run with bounded
  concurrency rather than one at a time.
- Videos autoplay on open, muted, with the player's own control to unmute.

### Removed

- **Tailscale support and the iOS Shortcuts upload path.** The Shortcut could
  never reliably attach videos — an iOS limitation, not a fixable one — and
  Automatic Photos Backup covers the same need without a second device, a VPN,
  or a network listener. This also removes the app's only non-loopback
  listener.

### Fixed

- Duplicate "iPhone Backup" channels created when an existing album wasn't
  reused during discovery.
- Orphaned cache files left on disk after an index wipe (2.8 GB in practice) —
  wipes now prune the filesystem too.
- `LimitInvalidError` during video playback: Telegram requires each request's
  byte offset to be a multiple of the chunk size, which an arbitrary seek
  position isn't. Offsets are now aligned and the overlap trimmed.
- Closing a video no longer leaves the full download running in the background.
- Videos failing to autoplay inside pywebview's WKWebView, which ships with a
  stricter media policy than a normal browser tab.

### Security

A full source audit was run against this release; all ten findings are fixed.
The two most serious shared one root cause — a filename chosen by whoever
uploaded a file to Telegram was trusted as both HTML and as a filesystem path,
reachable by anyone invited to a shared album:

- **Stored XSS via Telegram filenames.** The HTML escaper round-tripped through
  `textContent`, which by spec does not escape quotes, and its output was used
  inside attributes — so a crafted filename became an event handler with access
  to the app's JS bridge. Now escaped explicitly.
- **Path traversal via Telegram filenames.** Untrusted names were joined onto
  directories in three places (download, upload, thumbnail capture) and escaped
  them — reaching `~/Library/LaunchAgents`. All names are now normalized to a
  single inert path component.
- **`api_hash` stored in plaintext** in the local SQLite index (a regression
  introduced while building per-number credentials). Migrated back to the
  Keychain, with the old value vacuumed out of the database file and the
  database tightened to `0600`.
- Custom "Save As" destinations are validated against the home folder and
  auto-run locations are refused.
- A Content-Security-Policy was added, `Host` headers are validated to close
  DNS rebinding, the pairing token rotates on logout, and a latent AppleScript
  injection in the notification helper was quoted.

Verified clean in the same audit: SQL is fully parameterized, no shell
execution anywhere, constant-time token comparison, loopback-only binding, no
secrets in logs, and all dependencies past their known-CVE versions.

### Known issues

- Automatic Photos Backup can fail to delete from Photos on some libraries with
  an AppleEvent `-10000` error. Uploads are unaffected — items are backed up
  but stay in the library. Under investigation. *(Fixed in v0.2.1.)*

## v0.1.0

Initial release: Telegram-backed storage with albums, sharing, a watched
auto-backup folder, and iPhone uploads over Tailscale via iOS Shortcuts.
