import os, shutil, time, unittest

from server.jobs import Jobs
from tests.helpers import Cancelled, FakeDownloader, SONGS, temp_dirs

ROWS = {s[0]: (s[0], s[1], s[2], s[4]) for s in SONGS}


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.media = temp_dirs()
        self.state = os.path.join(self.data, "state.json")
        self.download = FakeDownloader()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def jobs(self, paused=True):
        jobs = Jobs(ROWS.get, self.media, self.state, self.download, Cancelled, parallel=1, retry_delay=0)
        if paused: jobs.set_paused(True)
        return jobs

    def wait(self, jobs, number, state, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if (jobs.progress_of(number) or {}).get("state") == state: return
            time.sleep(0.02)
        self.fail(f"{number} 没有变成 {state}：{jobs.progress_of(number)}")

    def test_guest_orders_jump_ahead_of_bulk_but_keep_their_own_order(self):
        jobs = self.jobs()
        jobs.submit_many(["20034621", "10006055", "20012901"])
        jobs.submit("20019055", priority=True)
        jobs.submit("30000461", priority=True)
        self.assertEqual(["20019055", "30000461", "20034621", "10006055", "20012901"], jobs.queue)

    def test_ordering_a_bulk_queued_song_promotes_it(self):
        jobs = self.jobs()
        jobs.submit_many(["20034621", "10006055", "20012901"])
        code, job = jobs.submit("20012901", priority=True)
        self.assertEqual(200, code)
        self.assertTrue(job["priority"])
        self.assertEqual("20012901", jobs.queue[0])

    def test_existing_file_is_done_and_bulk_skips_it(self):
        with open(os.path.join(self.media, "220019055.mkv"), "wb") as f: f.write(b"x")
        jobs = self.jobs()
        self.assertEqual(200, jobs.submit("20019055")[0])
        res = jobs.submit_many(["20019055", "10006055", "99999999", "abc"])
        self.assertEqual({"total": 4, "queued": 1, "existing": 1, "active": 0, "missing": 2}, res)

    def test_redownload_replaces_a_bad_file_only_when_the_new_one_is_complete(self):
        path = os.path.join(self.media, "220019055.mkv")
        with open(path, "wb") as f: f.write(b"bad")
        jobs = self.jobs(paused=False)
        self.download.fail.add("20019055")
        self.download.gate.clear()
        self.assertEqual(202, jobs.redownload("20019055")[0])
        self.wait(jobs, "20019055", "downloading")
        self.assertTrue(jobs.replacing("20019055"))
        self.assertEqual("downloading", jobs.song_state("20019055", "220019055.mkv"))   # 旧文件在，但不算下好
        self.assertEqual(200, jobs.submit("20019055", priority=True)[0])                 # 点歌不会把任务顶掉
        self.assertEqual(409, jobs.redownload("20019055")[0])
        self.download.gate.set()
        self.wait(jobs, "20019055", "failed")
        with open(path, "rb") as f: self.assertEqual(b"bad", f.read())                  # 下失败：旧的还在
        self.download.fail.clear()
        self.assertEqual(202, jobs.redownload("20019055")[0])
        self.wait(jobs, "20019055", "done")
        with open(path, "rb") as f: self.assertEqual(b"new mkv", f.read())
        self.assertIn(("20019055", "replace"), self.download.calls)
        self.assertFalse(jobs.replacing("20019055"))
        self.assertEqual(404, jobs.redownload("99999999")[0])

    def test_unknown_number_needs_a_filename(self):
        jobs = self.jobs()
        self.assertEqual(404, jobs.submit("88888888")[0])
        code, job = jobs.submit("88888888", filename="288888888.mkv")
        self.assertEqual(202, code)
        self.assertEqual("288888888.mkv", job["filename"])

    def test_queue_and_pause_survive_restart_with_priority_first(self):
        jobs = self.jobs()
        jobs.submit_many(["20034621", "10006055"])
        jobs.submit("20019055", priority=True)
        again = Jobs(ROWS.get, self.media, self.state, self.download, Cancelled, parallel=1, retry_delay=0)
        self.assertTrue(again.paused)
        self.assertEqual(["20019055", "20034621", "10006055"], again.queue)

    def test_clear_keeps_guest_orders(self):
        jobs = self.jobs()
        jobs.submit_many(["20034621", "10006055"])
        jobs.submit("20019055", priority=True)
        self.assertEqual(2, jobs.clear_queue()["cleared"])
        self.assertEqual(["20019055"], jobs.queue)

    def test_worker_downloads_and_failures_can_be_retried(self):
        self.download.fail.add("10006055")
        jobs = self.jobs(paused=False)
        jobs.submit("20019055", priority=True)
        jobs.submit("10006055")
        self.wait(jobs, "20019055", "done")
        self.wait(jobs, "10006055", "failed")
        self.assertTrue(os.path.exists(os.path.join(self.media, "220019055.mkv")))
        self.assertEqual(3, self.download.calls.count("10006055"))      # 失败会自动重试 3 次
        self.download.fail.clear()
        self.assertEqual(1, jobs.retry_failed()["retried"])
        self.wait(jobs, "10006055", "done")

    def test_active_lists_downloading_and_priority_waiting(self):
        self.download.gate.clear()
        jobs = self.jobs(paused=False)
        jobs.submit("20019055", priority=True)
        self.wait(jobs, "20019055", "downloading")
        jobs.submit_many(["10006055"])
        jobs.submit("20012901", priority=True)
        self.assertEqual(["20019055", "20012901"], [j["number"] for j in jobs.active()])
        self.download.gate.set()


if __name__ == "__main__":
    unittest.main()
