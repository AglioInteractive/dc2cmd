import contextlib
import io
import json
import logging
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import dc2cmd

VALID_ENV = """\
DISCORD_BOT_TOKEN=discord-token
SE_BEARER_TOKEN=se-token
DISCORD_CHANNEL_ID=123
SE_COMMAND_NAME=stream
SE_CHANNEL_ID=456
"""


def make_config(**overrides):
    config = {
        "DISCORD_BOT_TOKEN": "discord-token",
        "SE_BEARER_TOKEN": "se-token",
        "DISCORD_CHANNEL_ID": "123",
        "SE_COMMAND_NAME": "stream",
        "MESSAGE_LIMIT": 5,
        "TIMEZONE": "UTC",
        "TIMEZONE_NAME": "UTC",
    }
    config.update(overrides)
    return config


class FakeResponse:
    def __init__(self, body=None, status=200):
        if body is None:
            body = {}
        if isinstance(body, bytes):
            self.body = body
        else:
            self.body = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def http_error(code=500):
    return urllib.error.HTTPError(
        "https://example.test", code, "error", {}, None
    )


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.enterContext(mock.patch.object(dc2cmd, "CONFIG", self.config))

        self.tempdir = tempfile.TemporaryDirectory()
        self.enterContext(self.tempdir)
        self.temp_path = Path(self.tempdir.name)

        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.setLevel(logging.CRITICAL)
        self.addCleanup(root_logger.setLevel, old_level)

    def write_env(self, contents, name=".env"):
        path = self.temp_path / name
        path.write_text(contents, encoding="utf-8")
        return str(path)


class ParseEnvFileTests(BaseTestCase):
    def test_parses_supported_syntax(self):
        path = self.write_env(
            r"""
# comment
export PLAIN=value
QUOTED="line1\nline2 \"quoted\" \\ end"
SINGLE='# keep'
TRAILING=value # comment
"""
        )

        parsed = dc2cmd.parse_env_file(path)

        self.assertEqual(parsed["PLAIN"], "value")
        self.assertEqual(parsed["QUOTED"], 'line1\nline2 "quoted" \\ end')
        self.assertEqual(parsed["SINGLE"], "# keep")
        self.assertEqual(parsed["TRAILING"], "value")

    def test_warns_and_skips_malformed_lines(self):
        path = self.write_env("NO_EQUALS\n=missing-key\nGOOD=1\n")

        with self.assertLogs(level="WARNING") as logs:
            parsed = dc2cmd.parse_env_file(path)

        self.assertEqual(parsed, {"GOOD": "1"})
        self.assertEqual(len(logs.records), 2)


class ConfigDiscoveryTests(BaseTestCase):
    def test_infer_profile(self):
        cases = {
            ".env": "default",
            ".env.prod": "prod",
            ".env.": "default",
            "config.ini": "config",
            "weird name!.env": "weird_name_",
            "/path/to/.env.staging": "staging",
            "": "default",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(dc2cmd.infer_profile(path), expected)

    def test_finds_profile_file(self):
        (self.temp_path / ".env.prod").write_text("X=1", encoding="utf-8")

        with contextlib.chdir(self.temp_path):
            path, candidates = dc2cmd.find_config_file("prod")

        self.assertEqual(path, ".env.prod")
        self.assertEqual(candidates, ["prod", ".env.prod"])

    def test_prefers_exact_file(self):
        (self.temp_path / "prod").write_text("X=1", encoding="utf-8")

        with contextlib.chdir(self.temp_path):
            path, candidates = dc2cmd.find_config_file("prod")

        self.assertEqual(path, "prod")
        self.assertEqual(candidates, ["prod"])

    def test_accepts_path_to_existing_file(self):
        config_path = self.write_env("X=1", name="custom.env")

        path, candidates = dc2cmd.find_config_file(config_path)

        self.assertEqual(path, config_path)
        self.assertEqual(candidates, [config_path])

    def test_missing_path_has_no_profile_fallback(self):
        config_path = str(self.temp_path / "missing.env")

        path, candidates = dc2cmd.find_config_file(config_path)

        self.assertIsNone(path)
        self.assertEqual(candidates, [config_path])

    def test_reports_missing_candidates(self):
        with contextlib.chdir(self.temp_path):
            path, candidates = dc2cmd.find_config_file("missing")

        self.assertIsNone(path)
        self.assertEqual(candidates, ["missing", ".env.missing"])


class LoadConfigTests(BaseTestCase):
    def test_loads_valid_config(self):
        path = self.write_env(VALID_ENV + "MESSAGE_LIMIT=3\nTIMEZONE=UTC\n")

        with (
            mock.patch.object(
                dc2cmd.sys, "argv", ["dc2cmd.py", "--config", path]
            ),
            mock.patch.object(dc2cmd, "CONFIG", {}),
            mock.patch.object(dc2cmd, "CONFIG_PATH", None),
            mock.patch.object(dc2cmd, "PROFILE", "default"),
        ):
            dc2cmd.load_config()

            self.assertEqual(dc2cmd.CONFIG["MESSAGE_LIMIT"], 3)
            self.assertEqual(dc2cmd.CONFIG["TIMEZONE"], "UTC")
            self.assertEqual(dc2cmd.CONFIG_PATH, path)
            self.assertEqual(dc2cmd.PROFILE, "default")

    def test_loads_config_from_path(self):
        path = self.write_env(VALID_ENV, name="custom.env")

        with (
            mock.patch.object(
                dc2cmd.sys, "argv", ["dc2cmd.py", "--config", path]
            ),
            mock.patch.object(dc2cmd, "CONFIG", {}),
            mock.patch.object(dc2cmd, "CONFIG_PATH", None),
            mock.patch.object(dc2cmd, "PROFILE", "default"),
        ):
            dc2cmd.load_config()

            self.assertEqual(dc2cmd.CONFIG_PATH, path)
            self.assertEqual(dc2cmd.PROFILE, "custom")

    def test_rejects_invalid_config(self):
        cases = [
            ("missing", "DISCORD_BOT_TOKEN=x\n", "Missing required"),
            ("limit", VALID_ENV + "MESSAGE_LIMIT=nope\n", "MESSAGE_LIMIT"),
            (
                "channel",
                VALID_ENV.replace("SE_CHANNEL_ID=456\n", ""),
                "SE_CHANNEL_ID",
            ),
        ]
        for name, contents, message in cases:
            with self.subTest(name=name):
                path = self.write_env(contents, name=f".env.{name}")
                output = io.StringIO()
                with (
                    mock.patch.object(
                        dc2cmd.sys, "argv", ["dc2cmd.py", "--config", path]
                    ),
                    mock.patch.object(dc2cmd, "CONFIG", {}),
                    mock.patch.object(dc2cmd, "CONFIG_PATH", None),
                    mock.patch.object(dc2cmd, "PROFILE", "default"),
                    redirect_stdout(output),
                    self.assertRaises(SystemExit) as raised,
                ):
                    dc2cmd.load_config()

                self.assertEqual(raised.exception.code, 1)
                self.assertIn(message, output.getvalue())


class TimestampTests(BaseTestCase):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def timestamp(self, target):
        return int(target.timestamp())

    def test_formats_far_future_with_countdown(self):
        target = self.now + timedelta(days=10)

        result = dc2cmd.replace_discord_timestamps(
            f"<t:{self.timestamp(target)}:f>", now=self.now
        )

        self.assertEqual(
            result,
            "Sunday, January 11, 12:00 UTC "
            "(in$(time.until 2026-01-11T12:00:00+00:00))",
        )

    def test_formats_near_future_with_countdown(self):
        target = self.now + timedelta(hours=1)

        result = dc2cmd.replace_discord_timestamps(
            f"<t:{self.timestamp(target)}>", now=self.now
        )

        self.assertEqual(
            result,
            "Thursday 13:00 UTC (in$(time.until 2026-01-01T13:00:00+00:00))",
        )

    def test_omits_countdown_for_past_and_imminent_times(self):
        for offset in (timedelta(hours=-1), timedelta(minutes=1)):
            with self.subTest(offset=offset):
                target = self.now + offset
                result = dc2cmd.replace_discord_timestamps(
                    f"<t:{self.timestamp(target)}>", now=self.now
                )
                self.assertNotIn("time.until", result)
                self.assertIn("UTC", result)

    def test_accepts_styles_and_leaves_plain_text_alone(self):
        target = self.now + timedelta(hours=1)
        stamp = self.timestamp(target)

        result = dc2cmd.replace_discord_timestamps(
            f"<t:{stamp}> and <t:{stamp}:R>", now=self.now
        )

        self.assertEqual(result.count("Thursday 13:00 UTC"), 2)
        self.assertEqual(
            dc2cmd.replace_discord_timestamps("plain text", now=self.now),
            "plain text",
        )

    def test_falls_back_on_invalid_timezone(self):
        self.config["TIMEZONE"] = "Not/AZone"
        self.config["TIMEZONE_NAME"] = "Test time"
        target = self.now + timedelta(days=10)

        with self.assertLogs(level="WARNING") as logs:
            result = dc2cmd.replace_discord_timestamps(
                f"<t:{self.timestamp(target)}>", now=self.now
            )

        self.assertIn("Test time", result)
        self.assertIn("Invalid timezone", logs.output[0])


class TextHelperTests(BaseTestCase):
    def test_keeps_short_reply(self):
        self.assertEqual(dc2cmd.truncate_se_reply("hello"), "hello")

    def test_truncates_long_reply(self):
        result = dc2cmd.truncate_se_reply("a" * 500)

        self.assertEqual(len(result), dc2cmd.SE_REPLY_LIMIT)
        self.assertTrue(result.endswith("..."))

    def test_json_cache_round_trip(self):
        path = self.temp_path / "cache.json"

        dc2cmd.save_json_file(str(path), {"a": [1, 2]})

        self.assertEqual(dc2cmd.load_json_file(str(path), {}), {"a": [1, 2]})

    def test_missing_and_invalid_json_return_default(self):
        missing = self.temp_path / "missing.json"
        self.assertEqual(
            dc2cmd.load_json_file(str(missing), {"default": True}),
            {"default": True},
        )

        invalid = self.temp_path / "invalid.json"
        invalid.write_text("{not json", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(dc2cmd.load_json_file(str(invalid), []), [])

    def test_hash_config_key_is_stable_and_distinct(self):
        self.assertEqual(
            dc2cmd.hash_config_key("a", 1, None),
            dc2cmd.hash_config_key("a", 1, None),
        )
        self.assertNotEqual(
            dc2cmd.hash_config_key("a"), dc2cmd.hash_config_key("b")
        )


class AttachmentTests(BaseTestCase):
    def test_reuses_cached_short_link(self):
        cache = {"https://cdn.test/a.png": "https://s.test/a"}

        with mock.patch.object(dc2cmd.urllib.request, "urlopen") as urlopen:
            urls, failed, updated = dc2cmd.shorten_attachment_urls(
                [{"url": "https://cdn.test/a.png"}], cache
            )

        self.assertEqual(urls, ["https://s.test/a"])
        self.assertFalse(failed)
        self.assertFalse(updated)
        urlopen.assert_not_called()

    def test_shortens_and_caches_new_url(self):
        cache = {}
        response = FakeResponse({"data": [{"url": "https://s.test/a"}]})

        with mock.patch.object(
            dc2cmd.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            urls, failed, updated = dc2cmd.shorten_attachment_urls(
                [{"url": "https://cdn.test/a.png"}], cache
            )

        self.assertEqual(urls, ["https://s.test/a"])
        self.assertFalse(failed)
        self.assertTrue(updated)
        self.assertEqual(cache, {"https://cdn.test/a.png": "https://s.test/a"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.potat.app/redirect")
        self.assertEqual(request.get_method(), "POST")

    def test_handles_http_error(self):
        cache = {}
        error = http_error()
        self.addCleanup(error.close)

        with (
            mock.patch.object(
                dc2cmd.urllib.request, "urlopen", side_effect=error
            ),
            self.assertLogs(level="ERROR"),
        ):
            urls, failed, updated = dc2cmd.shorten_attachment_urls(
                [{"url": "https://cdn.test/a.png"}], cache
            )

        self.assertEqual(urls, [])
        self.assertTrue(failed)
        self.assertFalse(updated)
        self.assertEqual(cache, {})

    def test_handles_malformed_response(self):
        with (
            mock.patch.object(
                dc2cmd.urllib.request,
                "urlopen",
                return_value=FakeResponse({}),
            ),
            self.assertLogs(level="ERROR"),
        ):
            urls, failed, updated = dc2cmd.shorten_attachment_urls(
                [{"url": "https://cdn.test/a.png"}], {}
            )

        self.assertEqual(urls, [])
        self.assertTrue(failed)
        self.assertFalse(updated)


class DiscordProcessingTests(BaseTestCase):
    def test_orders_oldest_first_and_cleans_content(self):
        messages = [
            {"author": {"username": "Bob"}, "content": "second"},
            {
                "author": {"username": "Alice"},
                "content": "a <@123>b <@&456>c <#789>d <:wave:1>e\nf",
            },
            {"author": {"username": "Alice"}, "content": "<:only:1>"},
        ]

        text, updated = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "Alice: a b c d e f ⏎ Bob: second")
        self.assertFalse(updated)

    def test_groups_repeated_authors(self):
        messages = [
            {"author": {"username": "Bob"}, "content": "b2"},
            {"author": {"username": "Alice"}, "content": "a2"},
            {"author": {"username": "Alice"}, "content": "a1"},
        ]

        text, _ = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "Alice: a1 ⏎ a2 ⏎ Bob: b2")

    def test_omits_authors_when_all_messages_are_from_owner(self):
        self.config["AUTHOR_OWNER"] = "Alice"
        messages = [
            {"author": {"username": "Alice"}, "content": "two"},
            {"author": {"username": "Alice"}, "content": "one"},
        ]

        text, _ = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "one ⏎ two")

    def test_keeps_authors_when_owner_is_mixed_with_others(self):
        self.config["AUTHOR_OWNER"] = "Alice"
        messages = [
            {"author": {"username": "Bob"}, "content": "b"},
            {"author": {"username": "Alice"}, "content": "a"},
        ]

        text, _ = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "Alice: a ⏎ Bob: b")

    def test_includes_shortened_attachments(self):
        messages = [
            {
                "author": {"username": "Alice"},
                "content": "see",
                "attachments": [{"url": "https://cdn.test/a.png"}],
            }
        ]

        with mock.patch.object(
            dc2cmd,
            "shorten_attachment_urls",
            return_value=(["https://s.test/a"], False, True),
        ):
            text, updated = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "Alice: see https://s.test/a")
        self.assertTrue(updated)

    def test_marks_failed_attachments(self):
        messages = [
            {
                "author": {"username": "Alice"},
                "content": "see",
                "attachments": [{"url": "https://cdn.test/a.png"}],
            }
        ]

        with mock.patch.object(
            dc2cmd,
            "shorten_attachment_urls",
            return_value=([], True, False),
        ):
            text, updated = dc2cmd.process_discord_messages(messages, {})

        self.assertEqual(text, "Alice: see (attachments not included)")
        self.assertFalse(updated)

    def test_returns_waiting_when_nothing_remains(self):
        self.assertEqual(
            dc2cmd.process_discord_messages([], {}), ("Waiting", False)
        )
        self.assertEqual(
            dc2cmd.process_discord_messages(
                [{"author": {"username": "Alice"}, "content": "<@123>"}], {}
            ),
            ("Waiting", False),
        )


class DiscordFetchTests(BaseTestCase):
    def test_fetches_and_processes_messages(self):
        messages = [{"author": {"username": "Alice"}, "content": "hi"}]

        with mock.patch.object(
            dc2cmd.urllib.request,
            "urlopen",
            return_value=FakeResponse(messages),
        ) as urlopen:
            text, updated = dc2cmd.get_discord_messages({})

        self.assertEqual(text, "Alice: hi")
        self.assertFalse(updated)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://discord.com/api/v10/channels/123/messages?limit=5",
        )
        self.assertEqual(
            request.get_header("Authorization"), "Bot discord-token"
        )

    def test_handles_empty_response(self):
        with mock.patch.object(
            dc2cmd.urllib.request, "urlopen", return_value=FakeResponse([])
        ):
            self.assertEqual(
                dc2cmd.get_discord_messages({}), ("Waiting", False)
            )

    def test_returns_none_on_fetch_failure(self):
        with (
            mock.patch.object(
                dc2cmd.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("boom"),
            ),
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(dc2cmd.get_discord_messages({}), (None, False))


class ResolveChannelIdTests(BaseTestCase):
    def test_uses_explicit_channel_id(self):
        self.config["SE_CHANNEL_ID"] = "explicit"

        with mock.patch.object(dc2cmd, "se_api_get") as api:
            result = dc2cmd.resolve_se_channel_id({})

        self.assertEqual(result, ("explicit", False))
        api.assert_not_called()

    def test_uses_cached_channel_id(self):
        self.config["SE_CHANNEL_ID"] = ""
        self.config["TWITCH_CHANNEL"] = "mychannel"
        cache = {
            "channel": {
                "key": dc2cmd.hash_config_key("channel", "mychannel"),
                "id": "cached",
            }
        }

        with mock.patch.object(dc2cmd, "se_api_get") as api:
            result = dc2cmd.resolve_se_channel_id(cache)

        self.assertEqual(result, ("cached", False))
        api.assert_not_called()

    def test_refreshes_stale_cache(self):
        self.config["SE_CHANNEL_ID"] = ""
        self.config["TWITCH_CHANNEL"] = "mychannel"
        cache = {"channel": {"key": "old", "id": "old"}}

        with mock.patch.object(
            dc2cmd, "se_api_get", return_value={"_id": "new"}
        ) as api:
            result = dc2cmd.resolve_se_channel_id(cache)

        self.assertEqual(result, ("new", True))
        api.assert_called_once_with("channels/mychannel")
        self.assertEqual(cache["channel"]["id"], "new")
        self.assertEqual(
            cache["channel"]["key"],
            dc2cmd.hash_config_key("channel", "mychannel"),
        )

    def test_returns_none_when_lookup_fails(self):
        self.config["SE_CHANNEL_ID"] = ""
        self.config["TWITCH_CHANNEL"] = "mychannel"

        with mock.patch.object(dc2cmd, "se_api_get", return_value=None):
            self.assertEqual(dc2cmd.resolve_se_channel_id({}), (None, False))


class ResolveCommandIdTests(BaseTestCase):
    def test_uses_explicit_command_id(self):
        self.config["SE_COMMAND_ID"] = "explicit"

        with mock.patch.object(dc2cmd, "se_api_get") as api:
            result = dc2cmd.resolve_se_command_id("chan", {})

        self.assertEqual(result, ("explicit", False))
        api.assert_not_called()

    def test_uses_cached_command_id(self):
        self.config["SE_COMMAND_ID"] = ""
        self.config["SE_CHANNEL_ID"] = "chan"
        cache = {
            "command": {
                "key": dc2cmd.hash_config_key("command", "chan", "stream"),
                "id": "cached",
            }
        }

        with mock.patch.object(dc2cmd, "se_api_get") as api:
            result = dc2cmd.resolve_se_command_id("chan", cache)

        self.assertEqual(result, ("cached", False))
        api.assert_not_called()

    def test_resolves_command_by_name(self):
        self.config["SE_COMMAND_ID"] = ""
        self.config["SE_CHANNEL_ID"] = "chan"
        cache = {}
        commands = [
            {"command": "!Other", "_id": "other"},
            {"command": "!Stream", "_id": "cmd"},
        ]

        with mock.patch.object(
            dc2cmd, "se_api_get", return_value=commands
        ) as api:
            result = dc2cmd.resolve_se_command_id("chan", cache)

        self.assertEqual(result, ("cmd", True))
        api.assert_called_once_with("bot/commands/chan")
        self.assertEqual(cache["command"]["id"], "cmd")

    def test_returns_none_when_command_is_missing(self):
        self.config["SE_COMMAND_ID"] = ""
        self.config["SE_CHANNEL_ID"] = "chan"

        with mock.patch.object(dc2cmd, "se_api_get", return_value=[]):
            self.assertEqual(
                dc2cmd.resolve_se_command_id("chan", {}), (None, False)
            )

    def test_returns_none_for_unexpected_response(self):
        self.config["SE_COMMAND_ID"] = ""
        self.config["SE_CHANNEL_ID"] = "chan"

        with mock.patch.object(dc2cmd, "se_api_get", return_value={}):
            self.assertEqual(
                dc2cmd.resolve_se_command_id("chan", {}), (None, False)
            )


class UpdateStreamElementsTests(BaseTestCase):
    def test_sends_full_command_payload(self):
        with mock.patch.object(
            dc2cmd.urllib.request,
            "urlopen",
            return_value=FakeResponse({}, status=200),
        ) as urlopen:
            result = dc2cmd.update_streamelements("hello", "chan", "cmd")

        self.assertTrue(result)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.streamelements.com/kappa/v2/bot/commands/chan/cmd",
        )
        self.assertEqual(request.get_method(), "PUT")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["reply"], "hello")
        self.assertEqual(payload["command"], "stream")
        self.assertEqual(payload["channel"], "chan")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer se-token"
        )

    def test_returns_false_on_non_200_response(self):
        with mock.patch.object(
            dc2cmd.urllib.request,
            "urlopen",
            return_value=FakeResponse({}, status=500),
        ):
            self.assertFalse(
                dc2cmd.update_streamelements("hello", "chan", "cmd")
            )

    def test_returns_false_on_network_error(self):
        with (
            mock.patch.object(
                dc2cmd.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("down"),
            ),
            self.assertLogs(level="ERROR"),
        ):
            self.assertFalse(
                dc2cmd.update_streamelements("hello", "chan", "cmd")
            )


class MainTests(BaseTestCase):
    def run_main(self, new_text, update_result=True):
        with (
            mock.patch.object(dc2cmd, "load_config"),
            mock.patch.object(dc2cmd, "CACHE_DIR", str(self.temp_path)),
            mock.patch.object(dc2cmd, "PROFILE", "test"),
            mock.patch.object(
                dc2cmd,
                "resolve_se_channel_id",
                return_value=("chan", False),
            ),
            mock.patch.object(
                dc2cmd,
                "resolve_se_command_id",
                return_value=("cmd", False),
            ),
            mock.patch.object(
                dc2cmd,
                "get_discord_messages",
                return_value=(new_text, False),
            ),
            mock.patch.object(
                dc2cmd,
                "update_streamelements",
                return_value=update_result,
            ) as update,
        ):
            dc2cmd.main()

        return update

    def test_skips_update_when_text_is_unchanged(self):
        state = self.temp_path / "test-state.txt"
        state.write_text("same", encoding="utf-8")

        update = self.run_main("same")

        update.assert_not_called()

    def test_fetch_failure_leaves_command_and_state_untouched(self):
        update = self.run_main(None)

        update.assert_not_called()
        self.assertFalse((self.temp_path / "test-state.txt").exists())

    def test_successful_update_writes_state(self):
        update = self.run_main("new")

        update.assert_called_once_with("new", "chan", "cmd")
        self.assertEqual(
            (self.temp_path / "test-state.txt").read_text(encoding="utf-8"),
            "new",
        )

    def test_failed_update_does_not_write_state(self):
        update = self.run_main("new", update_result=False)

        update.assert_called_once()
        self.assertFalse((self.temp_path / "test-state.txt").exists())


if __name__ == "__main__":
    unittest.main()
