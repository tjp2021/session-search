# Changelog

## 0.2.2

### Added

- Added Linux support for indexing, search, ranking, and session cards.
- Expanded CI coverage across macOS and Linux.
- Added Linux discovery through `XDG_CONFIG_HOME` and storage through
  `XDG_DATA_HOME`.
- Added height-aware dashboard pages that keep choices and the prompt visible.
- Added installed-package acceptance checks for supported Python versions.
- Added public card, retrieval, performance, privacy, and lockfile gates to CI.

### Changed

- A full index scan now removes stale text and embeddings after a successful
  source scan.
- `fresh` now completes one scan before it searches.
- `ss index --reset` now clears database-derived SS state before rebuilding it.
- `ss status` now returns a nonzero result when the index is missing.
- SS now restricts its storage directories and sensitive files to the owner.
- Secret redaction now covers AWS session tokens, Azure account keys, and npm
  tokens.

### Fixed

- Damaged database quarantine now uses a unique path instead of overwriting an
  existing file.
- Database health checks no longer quarantine a healthy database during a lock.
- Dashboard project pages no longer overflow short terminal windows.
- Installed evaluation commands now resolve package data correctly.
- The command parser now accepts `--no-refresh` in supported positions.

### Known release limit

The five-person usability pilot remains incomplete. Automated checks don't
prove human usability.
