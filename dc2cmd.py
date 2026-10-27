"""Mirror recent Discord messages into a StreamElements custom command.

Run it with a config file:

    python dc2cmd.py --config myconfig     # loads .env.myconfig
    python dc2cmd.py --config path/to/env  # loads the file at that path
    python dc2cmd.py                       # loads .env
    python dc2cmd.py --debug               # show debug logging

Each run creates a .cache directory next to the script with state and lookup
files, one set per config profile (e.g. .cache/myconfig-state.txt).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

CONFIG = {}
# Path of the loaded .env file and the profile name inferred from it
CONFIG_PATH = None
PROFILE = "default"
CACHE_DIR = ".cache"

REQUIRED_KEYS = [
    "DISCORD_BOT_TOKEN",
    "SE_BEARER_TOKEN",
    "DISCORD_CHANNEL_ID",
    "SE_COMMAND_NAME",
]

# StreamElements caps command replies; stay below that with room to spare.
SE_REPLY_LIMIT = 470


def parse_env_file(path):
    """Parses a .env style file into a dict.

    Comments, quotes and `export` are supported.
    """
    config = {}
    with open(path, encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").lstrip()
            if "=" not in line:
                logging.warning(
                    f"{path}:{line_number}: ignoring line without '='"
                )
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                logging.warning(
                    f"{path}:{line_number}: ignoring line without a key"
                )
                continue

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ('"', "'")
            ):
                quote = value[0]
                value = value[1:-1]
                if quote == '"':
                    value = (
                        value.replace("\\n", "\n")
                        .replace('\\"', '"')
                        .replace("\\\\", "\\")
                    )
            else:
                # Drop trailing comments, but only when separated by whitespace
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

            config[key] = value
    logging.debug(f"Parsed {len(config)} setting(s) from '{path}'.")
    return config


def infer_profile(config_path):
    """Derives a cache profile from the config name.

    `.env` -> `default`, `.env.prod` -> `prod`.
    """
    name = os.path.basename(config_path)
    if name == ".env":
        profile = "default"
    elif name.startswith(".env."):
        profile = name.removeprefix(".env.")
    else:
        profile = os.path.splitext(name)[0] or "default"
    # Keep the profile safe to use as part of a file name
    profile = re.sub(r"[^A-Za-z0-9_.-]", "_", profile)
    return profile or "default"


def find_config_file(value):
    """Finds the config file.

    `--config` accepts a path to a config file or a profile name. An existing
    file is used as-is; otherwise a plain name means `.env.<name>`.
    """
    if value is None:
        candidates = [".env"]
    elif os.path.isfile(value):
        candidates = [value]
    else:
        candidates = [value]
        if not os.path.dirname(value) and not os.path.basename(
            value
        ).startswith(".env"):
            candidates.append(f".env.{value}")

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate, candidates
    return None, candidates


def load_config():
    """Parses command line arguments and loads the .env configuration file."""
    global CONFIG, CONFIG_PATH, PROFILE
    parser = argparse.ArgumentParser(
        description="Discord to StreamElements Sync Bot"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help=(
            "Config file or profile to load, e.g. prod, .env.prod or "
            "path/to/config (defaults to .env)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (defaults to INFO)",
    )
    args = parser.parse_args()

    # basicConfig installed the handler; only the root level needs lowering.
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path, candidates = find_config_file(args.config)
    if config_path is None:
        print(f"Error: Config file not found. Tried: {', '.join(candidates)}")
        sys.exit(1)

    try:
        CONFIG = parse_env_file(config_path)
    except OSError as e:
        print(f"Error: Could not read config file '{config_path}': {e}")
        sys.exit(1)

    missing = [key for key in REQUIRED_KEYS if not CONFIG.get(key)]
    if missing:
        print(
            f"Error: Missing required setting(s) in '{config_path}': "
            f"{', '.join(missing)}"
        )
        sys.exit(1)

    try:
        CONFIG["MESSAGE_LIMIT"] = int(CONFIG.get("MESSAGE_LIMIT") or 5)
    except ValueError:
        print(
            "Error: MESSAGE_LIMIT must be an integer, got "
            f"'{CONFIG['MESSAGE_LIMIT']}'"
        )
        sys.exit(1)

    # Optional settings and their defaults. An empty TIMEZONE means the
    # environment's local timezone is used.
    CONFIG["TIMEZONE"] = CONFIG.get("TIMEZONE") or ""
    CONFIG["TIMEZONE_NAME"] = CONFIG.get("TIMEZONE_NAME") or ""

    if not CONFIG.get("SE_CHANNEL_ID") and not CONFIG.get("TWITCH_CHANNEL"):
        print(
            "Error: Set either SE_CHANNEL_ID (StreamElements id) or "
            "TWITCH_CHANNEL (Twitch name/id)."
        )
        sys.exit(1)

    CONFIG_PATH = config_path
    PROFILE = infer_profile(config_path)
    logging.debug(
        f"Loaded config from {config_path} (cache profile: {PROFILE})"
    )


def replace_discord_timestamps(text, now=None):
    """Converts Discord <t:timestamp:format> to StreamElements format.

    The StreamElements format uses `$(time.until ...)` for countdowns.
    `now` is an optional aware datetime, mainly for deterministic tests.
    """
    tz_string = CONFIG.get("TIMEZONE") or ""
    tz_name = CONFIG.get("TIMEZONE_NAME") or (
        f"{tz_string} time" if tz_string else ""
    )
    now_utc = now or datetime.now(UTC)

    # Regex matches <t:1788195600> or <t:1788195600:f>
    pattern = re.compile(r"<t:(\d+)(?::[a-zA-Z])?>")

    def match_to_string(match):
        ts = int(match.group(1))
        # Base UTC time from the Unix timestamp
        dt_utc = datetime.fromtimestamp(ts, tz=UTC)

        # Convert to the configured timezone, or the environment's local one
        if tz_string:
            try:
                tz = ZoneInfo(tz_string)
                dt_local = dt_utc.astimezone(tz)
            except Exception as e:
                logging.warning(
                    f"Invalid timezone {tz_string}: {e}. Using local timezone."
                )
                dt_local = dt_utc.astimezone()
        else:
            dt_local = dt_utc.astimezone()

        # Label defaults to the local zone abbreviation (e.g. "EST time")
        label = tz_name or f"{dt_local.tzname() or 'local time'}"

        # Calculate how far away the date is
        # (absolute handles both past and future)
        time_diff = abs(dt_utc - now_utc)

        # Format the human readable portion
        if time_diff > timedelta(days=6):
            # More than 6 days: "Monday, August 31, 20:00"
            # Using dt_local.day prevents cross-platform leading-zero issues
            # (e.g. August 05)
            day_time = (
                f"{dt_local.strftime('%A, %B')} {dt_local.day}, "
                f"{dt_local.strftime('%H:%M')}"
            )
        else:
            # 6 days or less: "Monday 20:00"
            day_time = dt_local.strftime("%A %H:%M")

        # If the target time is less than 2 minutes away (or already in the
        # past), omit the StreamElements countdown
        if dt_utc - timedelta(minutes=2) < now_utc:
            return f"{day_time} {label}"

        # If safely in the future, format the RFC 3339 timestamp for
        # StreamElements. .isoformat() safely produces
        # '2026-08-31T20:00:00+03:00'
        rfc3339_str = dt_local.isoformat(timespec="seconds")

        return f"{day_time} {label} (in$(time.until {rfc3339_str}))"

    return pattern.sub(match_to_string, text)


def shorten_attachment_urls(attachments, url_cache):
    """Shortens attachment URLs, utilizing the cache to avoid duplicate
    calls.
    """
    short_urls = []
    failed = False
    cache_updated = False

    for att in attachments:
        raw_url = att.get("url", "")
        if not raw_url:
            continue

        base_url = raw_url

        if base_url in url_cache:
            logging.debug("Reused cached short link for attachment.")
            short_urls.append(url_cache[base_url])
        else:
            payload = json.dumps({"url": base_url}).encode("utf-8")
            req = urllib.request.Request(
                "https://api.potat.app/redirect", data=payload, method="POST"
            )
            req.add_header("Content-Type", "application/json")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )

            try:
                with urllib.request.urlopen(req) as resp:
                    if resp.status in (200, 201, 204):
                        resp_data = json.loads(resp.read().decode("utf-8"))
                        shortened = None
                        try:
                            shortened = resp_data["data"][0]["url"]
                        except (KeyError, IndexError, TypeError):
                            logging.error(
                                "Unexpected JSON format from Potat API: "
                                f"{resp_data}"
                            )

                        if shortened:
                            url_cache[base_url] = shortened
                            cache_updated = True
                            short_urls.append(shortened)
                            logging.debug(
                                f"Shortened attachment URL to '{shortened}'."
                            )
                        else:
                            failed = True
                    else:
                        failed = True
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8")
                logging.error(
                    f"URL Shortener HTTP {e.code} for {base_url}: {err_body}"
                )
                failed = True
            except Exception as e:
                logging.error(f"URL Shortener failed for {base_url}: {e}")
                failed = True

    return short_urls, failed, cache_updated


def process_discord_messages(data, url_cache):
    """Formats raw Discord messages into the StreamElements reply text."""
    if not data:
        return "Waiting", False

    processed_msgs = []
    any_shortening_failed = False
    is_cache_updated = False
    skipped_empty = 0
    author_owner = CONFIG.get("AUTHOR_OWNER", "")

    # Discord returns newest first; process oldest to newest.
    for msg in reversed(data):
        author = msg.get("author", {}).get("username", "Unknown")
        content = msg.get("content", "")

        # 1. Strip emotes, user/role mentions, and channel mentions
        content = re.sub(r"<a?:\w+:\d+>|<@[!&]?\d+>|<#\d+>", "", content)

        # 2. Convert Discord timestamps to StreamElements format
        content = replace_discord_timestamps(content)

        # 3. Handle attachments
        attachments = msg.get("attachments", [])
        short_urls, failed, cache_updated = shorten_attachment_urls(
            attachments, url_cache
        )

        if failed:
            any_shortening_failed = True
        if cache_updated:
            is_cache_updated = True

        # 4. Combine text and attachments
        msg_text = (content + " " + " ".join(short_urls)).strip()
        msg_text = msg_text.replace("\n", " ").strip()

        if not msg_text:
            skipped_empty += 1
            continue  # Skip entirely empty messages

        # Save to our temporary list
        processed_msgs.append((author, msg_text))

    # Check if every valid message we collected belongs to the owner
    # (If AUTHOR_OWNER isn't set in config, this defaults to False)
    all_by_owner = bool(author_owner) and all(
        a == author_owner for a, _ in processed_msgs
    )
    if skipped_empty:
        logging.debug(
            f"Skipped {skipped_empty} empty message(s) after cleanup."
        )
    if all_by_owner:
        logging.debug(
            f"All {len(processed_msgs)} message(s) are from "
            "AUTHOR_OWNER; omitting author names."
        )

    # 5. Format Authorship
    formatted_parts = []
    prev_author = None

    for author, msg_text in processed_msgs:
        if all_by_owner:
            # Skip authorship prepending entirely
            formatted_parts.append(msg_text)
        else:
            # Keep original logic if there are mixed authors
            if author != prev_author:
                formatted_parts.append(f"{author}: {msg_text}")
                prev_author = author
            else:
                formatted_parts.append(msg_text)

    # 6. Join with literal ⏎ and append fallback message if needed
    final_text = " ⏎ ".join(formatted_parts)
    if any_shortening_failed:
        final_text += " (attachments not included)"

    if final_text == "":
        final_text = "Waiting"

    logging.debug(
        f"Built reply text from {len(processed_msgs)} message(s), "
        f"{len(final_text)} character(s)."
    )
    return final_text, is_cache_updated


def get_discord_messages(url_cache):
    """Fetches messages from Discord and returns the formatted reply text."""
    channel_id = CONFIG.get("DISCORD_CHANNEL_ID")
    limit = CONFIG.get("MESSAGE_LIMIT", 5)
    token = CONFIG.get("DISCORD_BOT_TOKEN")

    url = (
        "https://discord.com/api/v10/channels/"
        f"{channel_id}/messages?limit={limit}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "DiscordBot (MyCustomBot, 1.0)")

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
        logging.debug(f"Fetched {len(data)} Discord message(s).")
        return process_discord_messages(data, url_cache)
    except Exception as e:
        logging.error(f"Failed to fetch or process Discord messages: {e}")
        return None, False


def truncate_se_reply(text):
    """Caps the reply at SE_REPLY_LIMIT, marking truncation with an ellipsis."""
    if len(text) <= SE_REPLY_LIMIT:
        return text
    truncated = text[: SE_REPLY_LIMIT - 3].rstrip() + "..."
    logging.debug(
        f"Truncated StreamElements reply from {len(text)} to "
        f"{len(truncated)} character(s)."
    )
    return truncated


def load_json_file(path, default):
    """Loads a JSON cache file, returning `default` when it is missing or
    invalid.
    """
    if not os.path.exists(path):
        logging.debug(f"No cache file at '{path}'.")
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        logging.debug(f"Loaded cache '{path}'.")
        return data
    except (OSError, json.JSONDecodeError) as e:
        logging.warning(f"Could not read cache '{path}': {e}")
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    logging.debug(f"Saved cache '{path}'.")


def hash_config_key(*parts):
    """Builds a stable cache key from configuration values."""
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def se_api_get(path):
    """GETs a StreamElements kappa API endpoint and returns the parsed JSON
    body.
    """
    token = CONFIG.get("SE_BEARER_TOKEN")
    url = f"https://api.streamelements.com/kappa/v2/{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {token}")

    logging.debug(f"StreamElements GET '{path}'.")
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logging.error(f"StreamElements API HTTP {e.code} for '{path}': {body}")
    except Exception as e:
        logging.error(f"StreamElements API request failed for '{path}': {e}")
    return None


def resolve_se_channel_id(cache):
    """Returns (channel_id, cache_updated), resolving TWITCH_CHANNEL when
    SE_CHANNEL_ID is absent.
    """
    configured = CONFIG.get("SE_CHANNEL_ID")
    if configured:
        logging.debug("Using explicit SE_CHANNEL_ID.")
        return configured, False

    twitch_channel = CONFIG.get("TWITCH_CHANNEL")
    cache_key = hash_config_key("channel", twitch_channel)
    cached = cache.get("channel") or {}
    if cached.get("key") == cache_key and cached.get("id"):
        logging.debug(
            f"Using cached StreamElements channel id for '{twitch_channel}'."
        )
        return cached["id"], False
    if cached:
        logging.debug(
            "Cached StreamElements channel id is stale; resolving it again."
        )

    data = se_api_get(
        f"channels/{urllib.parse.quote(twitch_channel, safe='')}"
    )
    channel_id = data.get("_id") if isinstance(data, dict) else None
    if not channel_id:
        return None, False

    cache["channel"] = {"key": cache_key, "id": channel_id}
    logging.info(
        f"Resolved Twitch channel '{twitch_channel}' to StreamElements id "
        f"{channel_id}."
    )
    return channel_id, True


def resolve_se_command_id(channel_id, cache):
    """Returns (command_id, cache_updated), resolving SE_COMMAND_NAME when
    SE_COMMAND_ID is absent.
    """
    configured = CONFIG.get("SE_COMMAND_ID")
    if configured:
        logging.debug("Using explicit SE_COMMAND_ID.")
        return configured, False

    command_name = CONFIG.get("SE_COMMAND_NAME")
    channel_key = CONFIG.get("SE_CHANNEL_ID") or CONFIG.get("TWITCH_CHANNEL")
    cache_key = hash_config_key("command", channel_key, command_name)
    cached = cache.get("command") or {}
    if cached.get("key") == cache_key and cached.get("id"):
        logging.debug(
            f"Using cached StreamElements command id for '{command_name}'."
        )
        return cached["id"], False
    if cached:
        logging.debug(
            "Cached StreamElements command id is stale; resolving it again."
        )

    commands = se_api_get(f"bot/commands/{channel_id}")
    if not isinstance(commands, list):
        return None, False

    wanted = command_name.strip().lstrip("!").lower()
    for command in commands:
        name = str(command.get("command", "")).strip().lstrip("!").lower()
        command_id = command.get("_id")
        if name == wanted and command_id:
            cache["command"] = {"key": cache_key, "id": command_id}
            logging.info(
                f"Resolved StreamElements command '{command_name}' to id "
                f"{command_id}."
            )
            return command_id, True

    logging.error(
        f"Command '{command_name}' was not found in StreamElements for "
        f"channel {channel_id}. Create it in StreamElements or set "
        "SE_COMMAND_ID explicitly."
    )
    return None, False


def update_streamelements(new_message_content, se_chan_id, se_cmd_id):
    se_cmd_name = CONFIG.get("SE_COMMAND_NAME")
    se_token = CONFIG.get("SE_BEARER_TOKEN")

    se_api_url = (
        "https://api.streamelements.com/kappa/v2/bot/commands/"
        f"{se_chan_id}/{se_cmd_id}"
    )

    payload = {
        "accessLevel": 100,
        "aliases": [],
        "channel": f"{se_chan_id}",
        "command": f"{se_cmd_name}",
        "cooldown": {"global": 5, "user": 15},
        "cost": 0,
        "enabled": True,
        "enabledOffline": True,
        "enabledOnline": True,
        "hidden": False,
        "keywords": [],
        "regex": "",
        "reply": f"{new_message_content}",
        "type": "mention",
    }

    json_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(se_api_url, data=json_data, method="PUT")
    req.add_header("Accept", "application/json; charset=utf-8")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {se_token}")

    logging.debug(
        f"Sending StreamElements PUT for command '{se_cmd_name}' "
        f"({len(new_message_content)} character(s))."
    )
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                return True
    except Exception as e:
        logging.error(f"Failed to update StreamElements: {e}")
    return False


def main():
    # 1. Initialize configuration from command line argument
    load_config()

    # Cache files are derived from the config name:
    # .env.prod -> .cache/prod-state.txt
    state_file = os.path.join(CACHE_DIR, f"{PROFILE}-state.txt")
    url_cache_file = os.path.join(CACHE_DIR, f"{PROFILE}-discord.json")
    se_cache_file = os.path.join(CACHE_DIR, f"{PROFILE}-se-ids.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    logging.debug(
        f"Using cache files: state='{state_file}', "
        f"links='{url_cache_file}', ids='{se_cache_file}'."
    )

    # 2. Resolve StreamElements channel/command ids (cached, keyed by config)
    se_cache = load_json_file(se_cache_file, {})
    if not isinstance(se_cache, dict):
        se_cache = {}

    se_channel_id, channel_cache_updated = resolve_se_channel_id(se_cache)
    if not se_channel_id:
        logging.error("Could not determine the StreamElements channel id.")
        sys.exit(1)

    se_command_id, command_cache_updated = resolve_se_command_id(
        se_channel_id, se_cache
    )
    if not se_command_id:
        logging.error("Could not determine the StreamElements command id.")
        sys.exit(1)

    if channel_cache_updated or command_cache_updated:
        save_json_file(se_cache_file, se_cache)

    # 3. Load URL Cache
    url_cache = load_json_file(url_cache_file, {})
    if not isinstance(url_cache, dict):
        url_cache = {}

    # 4. Fetch Discord
    new_text, cache_updated = get_discord_messages(url_cache)

    # 5. Save URL Cache if it changed
    if cache_updated:
        save_json_file(url_cache_file, url_cache)

    if new_text is None:
        logging.debug(
            "Discord fetch failed; leaving StreamElements untouched."
        )
        return

    new_text = truncate_se_reply(new_text)

    # 6. Check previous state
    old_text = ""
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            old_text = f.read()
    else:
        logging.debug(
            "No previous state file found; treating cached text as empty."
        )

    if new_text == old_text:
        logging.debug(
            "Generated text matches cached state; skipping StreamElements "
            "update."
        )
        return

    # 7. Push to SE
    logging.info("Change detected. Updating StreamElements...")
    logging.info(f"Old text: '{old_text}'")
    logging.info(f"New text: '{new_text}'")

    if update_streamelements(new_text, se_channel_id, se_command_id):
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(new_text)
        logging.debug(f"Saved state to '{state_file}'.")
        logging.info("Successfully updated StreamElements.")
    else:
        logging.error("Failed to update StreamElements.")


if __name__ == "__main__":
    main()
