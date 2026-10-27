# dc2cmd

Agent/contributor notes for this repo. User docs are in [README.md](README.md).

The runtime lives in `dc2cmd.py` (Python 3.11+, standard library only). Tests live in `tests/` and use `unittest`; Ruff handles formatting and linting.

## Functionality

- **Config loading** - parses `--config` as a path to an existing config file, or as a profile name resolved to `.env.<name>` when no such file exists. Validates required keys and derives a cache profile from the config file name (`.env` - `default`, `.env.prod` - `prod`). The `--debug` flag lowers the root logger to DEBUG.
- **Discord fetch** - reads the newest `MESSAGE_LIMIT` messages via the REST API and reorders them oldest - newest.
- **Message cleanup** - removes custom emotes, user/role mentions, and channel mentions; collapses newlines; skips messages that end up empty.
- **Timestamp conversion** - turns `<t:unix:style>` tags into readable dates in `TIMEZONE`, with a StreamElements countdown when the time is more than 2 minutes ahead.
- **Attachment shortening** - replaces attachment URLs with Potat short links and caches the mapping; retries only when Discord rotates a URL.
- **Author formatting** - prefixes the author when it changes; omits all names when every message is from `AUTHOR_OWNER`.
- **Assembly** - joins messages with ` ⏎ `, appends ` (attachments not included)` if any shortening failed, and falls back to `Waiting` when nothing remains.
- **Reply truncation** - caps the reply sent to StreamElements at 470 characters, ending it with `...` when shortened.
- **StreamElements id resolution** - uses `SE_CHANNEL_ID` / `SE_COMMAND_ID` when set, otherwise looks them up by `TWITCH_CHANNEL` / `SE_COMMAND_NAME` and caches the result.
- **Change detection** - compares the generated text against the cached state and exits without calling StreamElements when unchanged.
- **StreamElements update** - overwrites the custom command's reply with a full `PUT`, then saves the state file only on success.
- **Caching** - `.cache/{profile}-state.txt` (last pushed text), `{profile}-discord.json` (short-link map), `{profile}-se-ids.json` (resolved ids).

## Behavior worth preserving

- Explicit `SE_CHANNEL_ID` / `SE_COMMAND_ID` always bypass lookups and caching.
- Cached ids are keyed by a hash of the relevant config values, so config changes invalidate them.
- The StreamElements command payload is rebuilt from scratch on every update; only `reply` comes from Discord.
- Failed Discord fetches or id lookups must leave the existing command untouched.
- The state file is written only after a successful StreamElements update.

## Development notes

- Keep it a single standard-library file; preserve Python 3.11 compatibility.
- Never hard-code or commit tokens. `.env*` (except `.env.example`), `.cache/`, and `*.log` are git-ignored.
- Fatal config errors use `print()` + `sys.exit(1)`; everything else uses `logging`.
- `--debug` is a troubleshooting aid for manual runs only; scheduled runs stay at INFO.
- Run tests with `python -m unittest discover -s tests -v`.
- Format and lint with `ruff format .` and `ruff check .`; install dev tools from `requirements-dev.txt`.
- CI runs tests on Python 3.11 and 3.14 plus the Ruff checks.

## Integrations

- [Discord message API](https://discord.com/developers/docs/resources/message#get-channel-messages)
- [StreamElements API](https://dev.streamelements.com/)
- [PotatBotat redirects](https://potat.app/redirects)
