import threading
import time
import unittest

from kairos.wake import NOTIFICATION_PREFIX, WakeScheduler, format_notifications


def event(i, **extra):
    return {"id": f"e{i}", "kind": "terminal_finished", "text": f"event {i}", "created_at": 0.0, **extra}


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Host:
    """Records wakes; idle unless told otherwise."""

    def __init__(self):
        self.idle = True
        self.wakes = []
        self.paused = []
        self.lock = threading.Lock()

    def start_wake(self, events):
        with self.lock:
            self.wakes.append(list(events))

    def scheduler(self, **kwargs):
        kwargs.setdefault("debounce", 0.05)
        return WakeScheduler(self.start_wake, lambda: self.idle, on_paused=self.paused.append, **kwargs)


class WakeSchedulerTests(unittest.TestCase):
    def test_debounce_batches_events_into_one_wake(self):
        host = Host()
        scheduler = host.scheduler(debounce=0.2)
        for i in range(3):
            scheduler.push(event(i))
            time.sleep(0.02)
        self.assertEqual(host.wakes, [])  # still inside the batch window
        self.assertTrue(wait_for(lambda: host.wakes))
        time.sleep(0.3)
        self.assertEqual([[e["id"] for e in w] for w in host.wakes], [["e0", "e1", "e2"]])
        self.assertEqual(scheduler.take_pending(), [])

    def test_in_run_events_are_left_to_the_run(self):
        host = Host()
        scheduler = host.scheduler()
        scheduler.push(event(1, in_run=True))
        time.sleep(0.2)
        self.assertEqual(host.wakes, [])
        self.assertEqual(scheduler.take_pending(), [])

    def test_busy_host_keeps_events_for_the_user_message_or_a_later_wake(self):
        host = Host()
        host.idle = False
        scheduler = host.scheduler()
        scheduler.push(event(1))
        time.sleep(0.2)
        self.assertEqual(host.wakes, [])
        host.idle = True  # still pending: the retry wakes once idle
        self.assertTrue(wait_for(lambda: host.wakes))
        self.assertEqual(host.wakes[0][0]["id"], "e1")

        host.idle = False
        scheduler.push(event(2))
        self.assertEqual([e["id"] for e in scheduler.take_pending()], ["e2"])
        host.idle = True
        time.sleep(0.2)
        self.assertEqual(len(host.wakes), 1)  # taken events never wake

    def test_cap_pauses_once_and_a_user_message_resets(self):
        host = Host()
        scheduler = host.scheduler(cap=2)
        for i in range(2):
            scheduler.push(event(i))
            self.assertTrue(wait_for(lambda: len(host.wakes) == i + 1))
        scheduler.push(event(2))
        self.assertTrue(wait_for(lambda: host.paused))
        scheduler.push(event(3))
        time.sleep(0.2)
        self.assertTrue(scheduler.paused)
        self.assertEqual(len(host.wakes), 2)
        self.assertEqual(host.paused, [1])  # called once, with the count then

        scheduler.user_message_sent()
        self.assertFalse(scheduler.paused)
        self.assertEqual([e["id"] for e in scheduler.take_pending()], ["e2", "e3"])
        scheduler.push(event(4))  # counter reset: waking again
        self.assertTrue(wait_for(lambda: len(host.wakes) == 3))

    def test_every_event_delivered_exactly_once_under_concurrency(self):
        host = Host()
        scheduler = host.scheduler(debounce=0.01, cap=10_000)
        taken = []

        def producer(base):
            for i in range(200):
                scheduler.push(event(base + i))
                if i % 50 == 0:
                    time.sleep(0.005)

        def taker():
            for _ in range(100):
                taken.extend(scheduler.take_pending())
                time.sleep(0.002)

        threads = [threading.Thread(target=producer, args=(n * 1000,)) for n in range(4)]
        threads.append(threading.Thread(target=taker))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.2)
        taken.extend(scheduler.take_pending())
        delivered = [e["id"] for e in taken] + [e["id"] for w in host.wakes for e in w]
        self.assertEqual(len(delivered), 800)
        self.assertEqual(len(set(delivered)), 800)

    def test_close_stops_waking_but_keeps_pending(self):
        host = Host()
        scheduler = host.scheduler()
        scheduler.push(event(1))
        scheduler.close()
        time.sleep(0.2)
        self.assertEqual(host.wakes, [])
        self.assertEqual([e["id"] for e in scheduler.take_pending()], ["e1"])

    def test_format_notifications(self):
        text = format_notifications([event(1), event(2)])
        self.assertTrue(text.startswith(NOTIFICATION_PREFIX))
        self.assertEqual(text.count(NOTIFICATION_PREFIX), 2)
        self.assertIn("event 2", text)


if __name__ == "__main__":
    unittest.main()
