import json
import os
import threading
import time
import unittest

from helpers import load_fixture

from warpipe.enforcer import MonitorParser, Watcher
from warpipe.pw import PwError


def arr(objs):
    """Tak jak pw-dump -m -N: tablica z wcięciem 2, zakończona linią "]"."""
    return (json.dumps(objs, indent=2, ensure_ascii=False) + "\n").encode()


class TestMonitorParser(unittest.TestCase):
    def setUp(self):
        self.p = MonitorParser()
        self.full = load_fixture()
        self.assertTrue(self.p.feed(arr(self.full)))

    def test_initial_dump_in_small_chunks(self):
        p = MonitorParser()
        data = arr(self.full)
        results = [p.feed(data[i:i + 997]) for i in range(0, len(data), 997)]
        self.assertTrue(any(results))
        self.assertEqual(p.arrays, 1)
        self.assertEqual(p.buf.strip(), "")

    def test_multibyte_split(self):
        p = MonitorParser()
        data = arr([{"id": 5, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "Źdźbło"}}}])
        cut = data.index("Ź".encode()) + 1  # w środku znaku UTF-8
        self.assertFalse(p.feed(data[:cut]))
        self.assertTrue(p.feed(data[cut:]))

    def test_client_churn_is_ignored(self):
        # każde wywołanie pw-dump/pw-link to nowy klient — nie wolno na to reagować
        self.assertFalse(self.p.feed(arr([{"id": 900, "type": "PipeWire:Interface:Client",
                                            "info": {"props": {"application.name": "pw-dump"}}}])))
        self.assertFalse(self.p.feed(arr([{"id": 900, "info": None}])))

    def test_link_added_and_removed(self):
        link = {"id": 901, "type": "PipeWire:Interface:Link",
                "info": {"output-node-id": 1, "output-port-id": 2, "input-node-id": 3, "input-port-id": 4,
                         "change-mask": ["state", "props"], "state": "init", "props": {}}}
        self.assertTrue(self.p.feed(arr([link])))
        link2 = dict(link, info=dict(link["info"], state="active", **{"change-mask": ["state"]}))
        self.assertFalse(self.p.feed(arr([link2])))  # sama zmiana stanu
        self.assertTrue(self.p.feed(arr([{"id": 901, "info": None}])))
        self.assertFalse(self.p.feed(arr([{"id": 901, "info": None}])))  # powtórzone usunięcie

    def test_node_params_only_ignored_props_relevant(self):
        self.assertFalse(self.p.feed(arr([{"id": 93, "type": "PipeWire:Interface:Node",
                                            "info": {"change-mask": ["params"], "props": {}}}])))
        self.assertFalse(self.p.feed(arr([{"id": 93, "type": "PipeWire:Interface:Node",
                                            "info": {"change-mask": ["state"], "props": {}}}])))
        self.assertTrue(self.p.feed(arr([{"id": 93, "type": "PipeWire:Interface:Node",
                                           "info": {"change-mask": ["props", "params"], "props": {}}}])))

    def test_port_first_emission_relevant_even_if_id_known(self):
        # gdyby usunięcie i ponowne użycie id skleiło się w jedną emisję
        self.assertTrue(self.p.feed(arr([{"id": 188, "type": "PipeWire:Interface:Port",
                                           "info": {"change-mask": ["props", "params"], "props": {}}}])))
        self.assertFalse(self.p.feed(arr([{"id": 188, "type": "PipeWire:Interface:Port",
                                            "info": {"change-mask": ["params"], "props": {}}}])))

    def test_removed_node_relevant(self):
        self.assertTrue(self.p.feed(arr([{"id": 177, "info": None}])))

    def test_several_arrays_in_one_chunk(self):
        data = arr([{"id": 900, "type": "PipeWire:Interface:Client", "info": {"props": {}}}]) + \
            arr([{"id": 177, "info": None}])
        self.assertTrue(self.p.feed(data))


class FakeProc:
    def __init__(self):
        self.r, self.w = os.pipe()
        self.stdout = os.fdopen(self.r, "rb", buffering=0)
        self.terminated = False

    def write(self, data: bytes):
        os.write(self.w, data)

    def close_writer(self):
        try:
            os.close(self.w)
        except OSError:
            pass

    def terminate(self):
        self.terminated = True
        self.close_writer()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0


class FakeMonPw:
    def __init__(self):
        self.procs = []
        self.fail = False

    def start_monitor(self):
        if self.fail:
            raise PwError("brak")
        p = FakeProc()
        self.procs.append(p)
        return p


class CountingEnforcer:
    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def refresh(self, reason=""):
        with self.lock:
            self.n += 1


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestWatcher(unittest.TestCase):
    def test_debounced_refresh_and_stop(self):
        pw = FakeMonPw()
        enf = CountingEnforcer()
        w = Watcher(enf, pw, debounce=0.05, max_delay=0.3, poll_interval=60.0)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: enf.n >= 1))  # odświeżenie na starcie
            self.assertTrue(wait_for(lambda: pw.procs))
            base = enf.n
            proc = pw.procs[0]
            proc.write(arr(load_fixture()))
            self.assertTrue(wait_for(lambda: enf.n == base + 1))
            # seria zmian w krótkim czasie → jedno odświeżenie
            base = enf.n
            for i in range(5):
                proc.write(arr([{"id": 5000 + i, "type": "PipeWire:Interface:Link",
                                 "info": {"output-node-id": 1, "output-port-id": 2, "input-node-id": 3,
                                          "input-port-id": 4, "props": {}}}]))
                time.sleep(0.005)
            self.assertTrue(wait_for(lambda: enf.n >= base + 1))
            time.sleep(0.2)
            self.assertEqual(enf.n, base + 1)
            # zmiany klientów nie budzą
            base = enf.n
            proc.write(arr([{"id": 7000, "type": "PipeWire:Interface:Client", "info": {"props": {}}}]))
            time.sleep(0.2)
            self.assertEqual(enf.n, base)
            # ręczne żądanie
            w.request_refresh()
            self.assertTrue(wait_for(lambda: enf.n == base + 1))
        finally:
            w.stop()
            t.join(3)
        self.assertFalse(t.is_alive())
        self.assertTrue(pw.procs[0].terminated)

    def test_monitor_death_falls_back_and_restarts(self):
        pw = FakeMonPw()
        enf = CountingEnforcer()
        w = Watcher(enf, pw, debounce=0.02, max_delay=0.1, poll_interval=60.0, fallback_poll=0.1)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: pw.procs))
            base = enf.n
            pw.procs[0].close_writer()  # monitor padł
            self.assertTrue(wait_for(lambda: enf.n > base))
            self.assertTrue(wait_for(lambda: len(pw.procs) >= 2, timeout=5))
        finally:
            w.stop()
            t.join(3)
        self.assertFalse(t.is_alive())

    def test_no_monitor_available_polls(self):
        pw = FakeMonPw()
        pw.fail = True
        enf = CountingEnforcer()
        w = Watcher(enf, pw, poll_interval=60.0, fallback_poll=0.05)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: enf.n >= 3))
        finally:
            w.stop()
            t.join(3)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
