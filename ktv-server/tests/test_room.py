import os, shutil, threading, time, unittest

from server.room import Room, RoomError
from tests.helpers import SONGS, temp_dirs

ROWS = {s[0]: (s[0], s[1], s[2], s[4]) for s in SONGS}


class RoomTest(unittest.TestCase):
    def setUp(self):
        self.root, self.data, _ = temp_dirs()
        self.ready = set()
        self.path = os.path.join(self.data, "room.json")
        self.room = Room(self.path, ROWS.get, lambda n: n in self.ready)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_ready_song_starts_right_away(self):
        self.ready.add("20019055")
        self.room.add("20019055")
        self.assertEqual("20019055", self.room.current["number"])
        self.assertEqual("/media/20019055", self.room.snapshot()["current"]["media"])

    def test_song_waits_for_its_download_then_starts(self):
        self.room.add("10006055")
        self.assertIsNone(self.room.current)
        self.ready.add("10006055")
        self.room.song_ready("10006055")
        self.assertEqual("10006055", self.room.current["number"])

    def test_a_ready_song_plays_before_an_earlier_one_still_downloading(self):
        self.room.add("10006055")
        self.ready.add("20012901")
        self.room.add("20012901")
        self.assertEqual("20012901", self.room.current["number"])
        self.assertEqual(["10006055"], [e["number"] for e in self.room.queue])

    def test_duplicates_and_unknown_songs_are_rejected(self):
        self.room.add("10006055")
        with self.assertRaises(RoomError) as e: self.room.add("10006055")
        self.assertEqual(409, e.exception.status)
        with self.assertRaises(RoomError) as e: self.room.add("99999999")
        self.assertEqual(404, e.exception.status)

    def test_finish_moves_on_and_ignores_stale_reports(self):
        self.ready.update({"20019055", "10006055"})
        first = self.room.add("20019055")
        self.room.add("10006055")
        self.assertFalse(self.room.finish("old-entry"))
        self.room.report({"entryId": first["id"], "state": "ended"})
        self.assertEqual("10006055", self.room.current["number"])
        self.room.action("next")
        self.assertIsNone(self.room.current)

    def test_queue_edits_and_controls(self):
        self.ready.add("20019055")
        playing = self.room.add("20019055")
        a, b = self.room.add("10006055"), self.room.add("20012901")
        with self.assertRaises(RoomError): self.room.remove(playing["id"])
        self.room.move_to_next(b["id"])
        self.assertEqual([b["id"], a["id"]], [e["id"] for e in self.room.queue])
        self.room.remove(a["id"])
        self.room.action("pause")
        self.assertFalse(self.room.snapshot()["playing"])
        self.room.action("replay")
        snap = self.room.snapshot()
        self.assertTrue(snap["playing"])
        self.assertEqual(1, snap["replay"])
        self.room.audio("original", 150)
        self.assertEqual(("original", 100), (self.room.vocal, self.room.volume))
        with self.assertRaises(RoomError): self.room.audio("loud")

    def test_snapshot_carries_the_last_reported_position_of_the_current_song(self):
        self.ready.update({"20019055", "10006055"})
        first = self.room.add("20019055")
        self.room.add("10006055")
        self.room.report({"entryId": first["id"], "state": "playing", "position": 95})
        self.assertEqual(95, self.room.snapshot()["current"]["position"])
        self.room.action("next")
        self.assertEqual(0, self.room.snapshot()["current"]["position"])     # 下一首从头唱

    def test_feed_tells_who_ordered_and_what_happened(self):
        first = self.room.add("10006055", "小王")
        self.ready.add("10006055")
        self.room.song_ready("10006055")
        self.ready.add("20012901")
        self.room.add("20012901", "")
        self.room.report({"entryId": first["id"], "state": "ended"})
        self.room.action("next")
        texts = [e["text"] for e in self.room.snapshot()["feed"]]
        self.assertEqual(["小王 点了《江南》", "《江南》下载好了", "轮到 小王 的《江南》", "有人 点了《山丘》",
                          "轮到 有人 的《山丘》", "切掉了《山丘》"], texts)
        self.assertEqual({"小王": {"requested": 1, "sung": 1}}, self.room.users)
        self.assertEqual("小王", self.room.snapshot()["feed"][0]["by"])
        again = Room(self.path, ROWS.get, lambda n: n in self.ready)
        self.assertEqual(texts, [e["text"] for e in again.feed])                  # 重启后动态还在

    def test_poll_returns_as_soon_as_something_changes(self):
        version = self.room.poll()["version"]
        threading.Timer(0.2, lambda: self.room.add("10006055")).start()
        began = time.time()
        snap = self.room.poll(version, wait=5)
        self.assertLess(time.time() - began, 2)
        self.assertEqual(["10006055"], [e["number"] for e in snap["queue"]])
        self.assertTrue(self.room.connected())

    def test_restart_keeps_the_queue_and_replays_the_current_song(self):
        self.ready.add("20019055")
        self.room.add("20019055")
        self.room.add("10006055")
        again = Room(self.path, ROWS.get, lambda n: n in self.ready)
        self.assertIsNone(again.current)
        self.assertEqual(["20019055", "10006055"], [e["number"] for e in again.queue])
        self.assertFalse(again.connected())


if __name__ == "__main__":
    unittest.main()
