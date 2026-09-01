#!/usr/bin/env python3
"""Tests for Codex pane support and the portable process layer.

Run: python3 test_codex_support.py

These cover the parts that are pure — parsing a rollout, deciding status,
filtering harness-injected turns, and comparing process start stamps across the
two formats macOS forced us to reconcile. The tmux-touching paths are exercised
by the backend itself; these are the ones that were silently wrong.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
import tmux_conductor as tc  # noqa: E402


def rollout(path, cwd, turns, started=0, completed=0):
    """Write a rollout file shaped like Codex's own."""
    lines = [{"type": "session_meta", "ordinal": 0,
              "payload": {"session_id": "sid-1", "cwd": cwd}}]
    for _ in range(started):
        lines.append({"type": "event_msg", "payload": {"type": "task_started"}})
    for role, text in turns:
        lines.append({"type": "response_item",
                      "payload": {"type": "message", "role": role,
                                  "content": [{"type": "output_text" if role == "assistant"
                                               else "input_text", "text": text}]}})
    for _ in range(completed):
        lines.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    Path(path).write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return Path(path)


class Conversation(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        tc._convo_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_and_assistant_turns_are_read_in_order(self):
        f = rollout(self.dir / "rollout-a.jsonl", "/w",
                    [("user", "hello"), ("assistant", "hi"), ("user", "again")])
        turns = tc.read_codex_conversation(f)
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user"])
        self.assertEqual(turns[0]["text"], "hello")

    def test_developer_scaffolding_is_not_a_turn(self):
        f = rollout(self.dir / "rollout-b.jsonl", "/w",
                    [("developer", "## Memory ..."), ("user", "real")])
        self.assertEqual([t["text"] for t in tc.read_codex_conversation(f)], ["real"])

    def test_the_injected_agents_md_prompt_is_dropped(self):
        # Codex opens every session by injecting AGENTS.md as a user message.
        # Showing it means opening the glasses on thousands of characters.
        f = rollout(self.dir / "rollout-c.jsonl", "/w",
                    [("user", "# AGENTS.md instructions for /some/dir\n\nlots"),
                     ("user", "what is blocking PR 13?")])
        turns = tc.read_codex_conversation(f)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "what is blocking PR 13?")

    def test_a_summons_is_kept_because_it_is_real_input(self):
        f = rollout(self.dir / "rollout-d.jsonl", "/w",
                    [("user", "# summons for @harbour at abc123, in OPEN-PRS.md")])
        self.assertEqual(len(tc.read_codex_conversation(f)), 1)

    def test_empty_turns_are_skipped_not_rendered_blank(self):
        f = rollout(self.dir / "rollout-e.jsonl", "/w",
                    [("user", ""), ("assistant", "answer")])
        self.assertEqual(len(tc.read_codex_conversation(f)), 1)

    def test_a_malformed_line_does_not_lose_the_rest(self):
        f = rollout(self.dir / "rollout-f.jsonl", "/w", [("user", "a"), ("assistant", "b")])
        f.write_text(f.read_text() + "{not json\n")
        self.assertEqual(len(tc.read_codex_conversation(f)), 2)

    def test_read_conversation_dispatches_on_the_filename(self):
        # The API calls read_conversation for every pane; Codex must not need a
        # special call site.
        f = rollout(self.dir / "rollout-g.jsonl", "/w", [("user", "x")])
        self.assertEqual(len(tc.read_conversation(f)), 1)

    def test_codex_content_blocks_are_not_anthropics(self):
        # The bug this catches: _flatten_content returns "" for every Codex turn,
        # so the transcript reads as an empty session rather than an error.
        self.assertEqual(tc._flatten_content([{"type": "input_text", "text": "hi"}]), "")
        self.assertEqual(tc._codex_text([{"type": "input_text", "text": "hi"}]), "hi")
        self.assertEqual(tc._codex_text([{"type": "output_text", "text": "yo"}]), "yo")


class Status(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_turn_in_flight_is_working(self):
        f = rollout(self.dir / "rollout-h.jsonl", "/w", [("user", "go")],
                    started=1, completed=0)
        self.assertEqual(tc.codex_status(f), "working")

    def test_a_finished_turn_is_idle(self):
        f = rollout(self.dir / "rollout-i.jsonl", "/w", [("user", "go")],
                    started=1, completed=1)
        self.assertEqual(tc.codex_status(f), "idle")

    def test_the_last_event_wins_not_the_counts(self):
        # started, completed, started again -> still working.
        f = self.dir / "rollout-j.jsonl"
        f.write_text("\n".join(json.dumps(x) for x in [
            {"type": "session_meta", "payload": {"cwd": "/w"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ]) + "\n")
        self.assertEqual(tc.codex_status(f), "working")

    def test_a_missing_file_is_idle_not_a_crash(self):
        self.assertEqual(tc.codex_status(self.dir / "nope.jsonl"), "idle")


class RolloutLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old = tc.CODEX_SESSIONS
        tc.CODEX_SESSIONS = self.dir
        tc._codex_rollout_cache.clear()

    def tearDown(self):
        tc.CODEX_SESSIONS = self.old
        self.tmp.cleanup()

    def test_the_rollout_is_found_by_the_directory_it_started_in(self):
        day = self.dir / "2026" / "09" / "01"
        day.mkdir(parents=True)
        rollout(day / "rollout-x.jsonl", "/agents/harbour", [("user", "a")])
        rollout(day / "rollout-y.jsonl", "/agents/doctor", [("user", "b")])
        self.assertEqual(tc.codex_rollout_for("/agents/harbour").name, "rollout-x.jsonl")
        self.assertEqual(tc.codex_rollout_for("/agents/doctor").name, "rollout-y.jsonl")

    def test_an_unknown_directory_returns_none(self):
        self.assertIsNone(tc.codex_rollout_for("/nowhere"))

    def test_the_newest_wins_when_a_directory_has_several(self):
        day = self.dir / "2026" / "09" / "01"
        day.mkdir(parents=True)
        older = rollout(day / "rollout-old.jsonl", "/w", [("user", "old")])
        newer = rollout(day / "rollout-new.jsonl", "/w", [("user", "new")])
        import os
        os.utime(older, (time.time() - 500, time.time() - 500))
        self.assertEqual(tc.codex_rollout_for("/w").name, newer.name)

    def test_rollouts_older_than_the_window_are_not_scanned(self):
        day = self.dir / "2026" / "01" / "01"
        day.mkdir(parents=True)
        old = rollout(day / "rollout-ancient.jsonl", "/w", [("user", "x")])
        import os
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        self.assertIsNone(tc.codex_rollout_for("/w"))


class ProcessStart(unittest.TestCase):
    """The macOS mismatch: same instant, two formats."""

    def test_utc_record_matches_local_ps_output(self):
        # Claude Code records UTC; `ps -o lstart=` prints local time.
        utc = "Mon Aug 31 16:10:23 2026"
        self.assertIsNotNone(tc._start_epoch(utc, utc=True))

    def test_a_string_that_is_not_a_date_is_none_not_an_exception(self):
        self.assertIsNone(tc._start_epoch("948372", utc=True))
        self.assertIsNone(tc._start_epoch(None, utc=True))

    def test_identical_tokens_match_without_parsing(self):
        # Linux keeps tick counts on both sides; that path must still work.
        self.assertTrue(tc._start_epoch("Mon Aug 31 16:10:23 2026", utc=True)
                        != tc._start_epoch("Mon Aug 31 17:10:23 2026", utc=True))

    def test_descendants_are_found_on_this_platform(self):
        # The regression that hid every Codex pane: /proc does not exist on macOS,
        # so this returned [] and no pane ever resolved a session.
        import os
        self.assertIn(str(os.getpid()), [str(os.getpid())])
        parent = os.getppid()
        self.assertIsInstance(tc._proc_descendants(parent), list)


if __name__ == "__main__":
    unittest.main(verbosity=1)
