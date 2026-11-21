"""pw-dump --monitor parsing and the Watcher loop (debounce, restarts, fallback polling)."""

import json
import os
import random
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import ROOT, load_fixture

from warpipe import pw as pwmod
from warpipe.enforcer import MonitorParser, Watcher, _terminate
from warpipe.pw import PwCli, PwError


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


class TestMonitorView(unittest.TestCase):
    """The parser keeps the graph as the stream describes it and reports NEW links
    into recording streams (for the fast path)."""

    def setUp(self):
        from scenarios import per_app
        self.b, self.ns = per_app()
        self.p = MonitorParser()
        self.p.feed(arr(self.b.dump()))

    def test_initial_state_is_not_news(self):
        self.assertEqual(self.p.take_new_capture_links(), [])
        from warpipe.graph import Graph
        self.assertEqual(set(Graph.from_dump(self.p.view()).links), {o["id"] for o in self.b.objs
                                                                    if o["type"] == "PipeWire:Interface:Link"})

    def test_new_capture_links_reported(self):
        from test_enforcer import add_ding
        ding, cap, links, new = add_ding(self.b)
        sink = self.b.connect(ding, self.ns.ee_sink, dst_prefix="playback")   # to a sink: not interesting
        ours = self.b.link(self.ns.firefox.p("output_FL"), self.ns.cap_ff.p("input_FL"), managed=True)
        new = new + [o for o in self.b.objs if o["id"] in set(sink) | {ours}]
        self.assertTrue(self.p.feed(arr(new)))
        self.assertEqual(self.p.take_new_capture_links(), links)
        self.assertEqual(self.p.take_new_capture_links(), [])            # taken
        # removal of a link and a state-only update of a node
        self.p.feed(arr([{"id": links[0], "info": None},
                         {"id": cap.id, "type": "PipeWire:Interface:Node",
                          "info": {"change-mask": ["state"], "state": "idle"}}]))
        self.assertNotIn(links[0], self.p.objects)
        node = self.p.objects[cap.id]
        self.assertEqual(node["info"]["state"], "idle")
        self.assertEqual(node["info"]["props"]["node.name"], "discord_capture")   # merged, not lost

    def test_unparsable_batch_disables_the_view(self):
        from test_enforcer import add_ding
        self.p.feed(b"[\n  { broken\n]\n")
        ding, cap, links, new = add_ding(self.b)
        self.p.feed(arr(new))
        self.assertFalse(self.p.view_ok)
        self.assertEqual(self.p.take_new_capture_links(), [])


class TestWatcherFastPath(unittest.TestCase):
    def test_unwanted_link_cut_before_the_debounced_refresh(self):
        """Monitor stream -> Watcher -> Enforcer.fast_cut -> pw-link, no pw-dump, no debounce."""
        import tempfile
        from pathlib import Path

        from helpers import FakePw
        from scenarios import per_app
        from test_enforcer import add_ding

        from warpipe.enforcer import Enforcer
        from warpipe.graph import Graph
        from warpipe.state import Config, SnapshotStore
        b, ns = per_app()
        fake = FakePw(b)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        enf = Enforcer(fake, Config(d / "c.json"), SnapshotStore(d / "s.json"),
                       enabled=True, selected=["Warframe.x64.exe"], persist=False)
        pw = FakeMonPw()
        w = Watcher(enf, pw, debounce=1.5, max_delay=3.0, poll_interval=60.0)
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: pw.procs and fake.calls))
            pw.procs[0].write(arr(b.dump()))               # the monitor's initial state
            self.assertTrue(wait_for(lambda: w.refresh_count >= 1))
            dumps = sum(1 for c in fake.calls if c[0] == "dump")
            ding, cap, links, new = add_ding(b)
            t0 = time.monotonic()
            pw.procs[0].write(arr(new))
            self.assertTrue(wait_for(lambda: not any(o["id"] in links for o in b.objs), 1.0))
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)                   # the debounce alone is 1.5 s here
            self.assertEqual(sum(1 for c in fake.calls if c[0] == "dump"), dumps)
            self.assertEqual({l.out_node for l in Graph.from_dump(b.dump()).links.values()
                              if l.in_node == ns.cap_wf.id}, {ns.warframe.id})   # the selected app stays
        finally:
            w.stop()
            t.join(3)
        self.assertFalse(t.is_alive())


# A stand-in for the tool: starts the monitor (with `sleep <marker>` instead of
# pw-dump), prints its PID and waits to be killed.
_TOOL = r"""
import sys, threading, time
sys.path.insert(0, sys.argv[1])
from warpipe import pw
pw.MONITOR_ARGV[:] = ["sleep", sys.argv[2]]

def start():
    p = pw.PwCli().start_monitor()
    print(p.pid, flush=True)
    time.sleep(3600)          # like the Watcher thread: lives as long as the tool

if sys.argv[3] == "thread":
    threading.Thread(target=start, daemon=True).start()
    time.sleep(3600)
else:
    start()
"""


def _alive(pid: int, marker: str) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read()
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return marker.encode() in cmd and state != "Z"


@unittest.skipUnless(os.path.isdir("/proc/self") and pwmod._prctl is not None, "Linux prctl needed")
class TestMonitorDiesWithTool(unittest.TestCase):
    """Review finding: after SIGKILL of the tool its `pw-dump -m -N` kept running
    (re-parented to systemd --user). PR_SET_PDEATHSIG makes it go with the tool."""

    def _run(self, mode: str) -> None:
        marker = f"{3600 + random.randint(1, 999)}.{os.getpid()}"
        tool = subprocess.Popen([sys.executable, "-c", _TOOL, ROOT, marker, mode],
                                stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        mon = None
        try:
            line = tool.stdout.readline()
            mon = int(line)
            self.assertTrue(_alive(mon, marker))
            tool.kill()                                  # SIGKILL: no cleanup code runs
            tool.wait(5)
            end = time.monotonic() + 5
            while _alive(mon, marker) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(_alive(mon, marker), "the monitor outlived the killed tool")
        finally:
            if tool.poll() is None:
                tool.kill()
                tool.wait(5)
            tool.stdout.close()
            if mon is not None and _alive(mon, marker):
                os.kill(mon, 9)

    def test_sigkill_of_tool_main_thread(self):     # --watch: the Watcher runs in the main thread
        self._run("main")

    def test_sigkill_of_tool_watcher_thread(self):  # tray: the Watcher has its own thread
        self._run("thread")

    def test_normal_stop_still_works(self):
        marker = f"{3600 + random.randint(1, 999)}.{os.getpid()}"
        with mock.patch.object(pwmod, "MONITOR_ARGV", ["sleep", marker]):
            p = PwCli().start_monitor()
        try:
            self.assertTrue(_alive(p.pid, marker))
            _terminate(p)
            self.assertIsNotNone(p.returncode)
            self.assertFalse(_alive(p.pid, marker))
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(5)


if __name__ == "__main__":
    unittest.main()
