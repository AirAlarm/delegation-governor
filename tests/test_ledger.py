"""Task Ledger: ids, dependencies, lifecycle, concurrent claiming, migration."""
from __future__ import annotations

import sqlite3
import threading

from base import DGTest

from dg import store


class TestLedger(DGTest):
    def test_creation_and_stable_ids(self):
        a, b = self.task("first"), self.task("second")
        self.assertEqual((a, b), ("DG-1", "DG-2"))
        self.assertEqual(store.get_task(self.con, a)["title"], "first")

    def test_ids_never_reused_after_delete(self):
        a = self.task("gone")
        self.con.execute("DELETE FROM tasks WHERE id=?", (a,))
        self.assertEqual(self.task("new"), "DG-2")

    def test_dependency_must_exist(self):
        with self.assertRaises(ValueError):
            self.task("orphan", deps=["DG-999"])

    def test_mode_is_validated(self):
        with self.assertRaises(ValueError):
            self.task("bad", mode="SIDEWAYS")

    def test_status_is_validated(self):
        a = self.task()
        with self.assertRaises(ValueError):
            store.set_status(self.con, a, "NONSENSE")

    def test_lifecycle_to_integrated(self):
        a = self.task()
        for s in ("QUEUED", "RUNNING", "SUCCEEDED", "INTEGRATED"):
            store.set_status(self.con, a, s)
            self.assertEqual(store.get_task(self.con, a)["status"], s)

    def test_claim_is_exclusive(self):
        a = self.task()
        self.assertTrue(store.claim(self.con, a, "session-1", "codex"))
        self.assertFalse(store.claim(self.con, a, "session-2", "codex"))
        self.assertEqual(store.get_task(self.con, a)["sessionId"], "session-1")

    def test_release_returns_to_planned(self):
        a = self.task()
        store.claim(self.con, a, "s1", "codex")
        store.release(self.con, a)
        self.assertEqual(store.get_task(self.con, a)["status"], "PLANNED")

    def test_concurrent_claim_only_one_winner(self):
        """Two live connections race for one task; exactly one may win."""
        a = self.task()
        wins: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def contend(n: int):
            con = store.connect()
            try:
                barrier.wait(timeout=10)
                got = store.claim(con, a, f"session-{n}", "codex")
                with lock:
                    wins.append(got)
            finally:
                con.close()

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sum(wins), 1, f"expected exactly one winner, got {wins}")

    def test_concurrent_writes_from_two_connections(self):
        a = self.task()
        other = store.connect()
        try:
            store.set_status(self.con, a, "RUNNING")
            store.set_status(other, a, "SUCCEEDED")
            self.assertEqual(store.get_task(self.con, a)["status"], "SUCCEEDED")
        finally:
            other.close()

    def test_attempt_history_survives_fallback(self):
        a = self.task()
        first = store.add_attempt(self.con, a, "codex")
        store.finish_attempt(self.con, first, "QUOTA_FAILED", "quota exhausted")
        second = store.add_attempt(self.con, a, "cc-delegate")
        store.finish_attempt(self.con, second, "SUCCEEDED")
        atts = store.attempts_for(self.con, a)
        self.assertEqual([x["worker"] for x in atts], ["codex", "cc-delegate"])
        self.assertEqual([x["status"] for x in atts], ["QUOTA_FAILED", "SUCCEEDED"])
        # The failed Codex attempt stays visible -- it is never hidden (spec 32).
        self.assertEqual(atts[0]["error_kind"], "quota exhausted")

    def test_live_attempt_tracks_only_running(self):
        a = self.task()
        att = store.add_attempt(self.con, a, "codex")
        self.assertIsNotNone(store.live_attempt(self.con, a))
        store.finish_attempt(self.con, att, "SUCCEEDED")
        self.assertIsNone(store.live_attempt(self.con, a))

    def test_blocks_map_reverses_edges(self):
        a = self.task("a")
        b = self.task("b", deps=[a])
        c = self.task("c", deps=[a])
        m = store.blocks_map(store.all_tasks(self.con))
        self.assertEqual(sorted(m[a]), sorted([b, c]))
        self.assertEqual(m[b], [])

    def test_no_credentials_are_stored(self):
        self.task("x")
        blob = self.home.joinpath("governor.db").read_bytes().lower()
        for secret in (b"sk-", b"api_key", b"authorization", b"bearer "):
            self.assertNotIn(secret, blob)

    def test_migration_rejects_newer_schema(self):
        self.con.execute("UPDATE meta SET value='999' WHERE key='schemaVersion'")
        self.con.close()
        with self.assertRaises(RuntimeError):
            store.connect()

    def test_migration_upgrades_older_schema(self):
        self.con.execute("UPDATE meta SET value='0' WHERE key='schemaVersion'")
        self.con.close()
        con = store.connect()
        row = con.execute("SELECT value FROM meta WHERE key='schemaVersion'").fetchone()
        self.assertEqual(int(row["value"]), store.SCHEMA_VERSION)
        con.close()

    def test_foreign_key_cascade(self):
        a = self.task()
        store.add_attempt(self.con, a, "codex")
        self.con.execute("DELETE FROM tasks WHERE id=?", (a,))
        self.assertEqual(store.attempts_for(self.con, a), [])

    def test_kv_roundtrip_and_age(self):
        store.kv_set(self.con, "k", {"a": 1})
        self.assertEqual(store.kv_get(self.con, "k"), {"a": 1})
        self.assertLess(store.kv_age(self.con, "k"), 5)
        self.assertIsNone(store.kv_age(self.con, "missing"))
