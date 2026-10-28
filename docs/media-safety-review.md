# Media safety review remediation

Branch: `fix/media-safety-review`

This is a work tracker, not a claim that replacement is safe. Lossy viewing copies
are not preservation of the original media. Keep independent backups.

## Implemented

- Unspecified promotion modes preserve the original. The scheduler consumes the
  persisted per-job decision and never grants replacement authority itself.
- Replacement approval is persisted per job and bound to its source fingerprint
  and plan hash. Backup deletion needs distinct persisted approval; it is retained
  otherwise. Legacy jobs intentionally receive no replacement approval.
- Keep-original installation uses a destination-local hard link, which atomically
  fails when a destination already exists, rather than overwriting it.
- Recovery interprets SQLite's naive persisted lease timestamps as UTC.
- Destructive promotion fully decodes source and staged output and compares
  video frame/timestamp coverage plus selected audio timing before installation.
- Rollback checks persisted installed inode ownership and original-backup content
  digest before restoring, then verifies restoration before recording success.
- Restoration failures are recorded as `recovery_required`, retain the backup,
  and block automatic replay. Successful rollback returns the job to promotion.
- Regression tests execute real FFmpeg truncation, concurrent destination
  creation, post-install corruption, restoration failure, expired leases, real
  mixed post-encode batches, and interruption at persisted promotion phases.
- Renamed the queue-selection test so its name does not imply it executes a batch.

## Remaining required work

1. Extend real-media coverage to VFR, interlaced/HDR, multiple audio tracks,
   subtitle payloads, chapters, attachments, and unusual containers/codecs.
2. Define an explicit policy for allowed frame-rate-changing transformations;
   the current replacement gate intentionally fails closed.
3. Add a supported inspect/repair UX for `recovery_required`; it currently
   retains files safely but requires manual filesystem investigation.
4. Test real power loss and target filesystem behavior. Fault injection proves
   state-machine paths, not every durability property of physical storage.
5. Obtain an independent review before presenting replacement as suitable for
   the only copy of an irreplaceable collection.

## Verification

Safety pass: `uv run pytest -q`: 1,183 passed, four skipped. This is regression
coverage, not proof of complete media preservation. No complete Av1an encode was
run for this pass.
