"""The Warpipe window (left click on the tray icon) — offscreen only, nothing is shown on screen."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

from helpers import FakePw, GraphBuilder
from scenarios import base_system, monitor_mode, per_app, with_apps

from warpipe import i18n
from warpipe.enforcer import Enforcer, Status
from warpipe.state import Config, SnapshotStore


class TestPlaceNear(unittest.TestCase):
    """X11 only: where the window opens (pure function)."""

    AREA = (0, 0, 1920, 1040)        # available geometry, panel at the bottom

    def test_above_a_tray_icon_at_the_bottom_right(self):
        from warpipe.popup import place_near
        self.assertEqual(place_near(420, 400, (1800, 1050, 24, 24), self.AREA), (1500, 640))   # clamped above the panel

    def test_below_a_top_panel_and_clamped(self):
        from warpipe.popup import place_near
        self.assertEqual(place_near(420, 400, (10, 0, 24, 24), (0, 30, 1920, 1050)), (0, 30))
        self.assertEqual(place_near(420, 400, (900, 2, 24, 24), (0, 30, 1920, 1050)), (702, 32))

    def test_second_screen_and_huge_window(self):
        from warpipe.popup import place_near
        self.assertEqual(place_near(420, 400, (4000, 1400, 1, 1), (1920, 0, 2560, 1400)), (3790, 994))
        self.assertEqual(place_near(420, 2000, (100, 100, 1, 1), self.AREA), (0, 0))


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class TestAppIcons(unittest.TestCase):
    def test_candidates_best_first(self):
        from warpipe.icons import GAMES_ICON, app_icon_names
        names = app_icon_names([{"application.icon-name": "my-player", "application.name": "Player",
                                 "application.process.binary": "/usr/bin/player"}], "Player")
        self.assertEqual(names[0], "my-player")
        self.assertIn("player", names)
        self.assertNotIn(GAMES_ICON, names)
        self.assertEqual(names[-2:], ["audio-x-generic", "application-x-executable"])
        flatpak = app_icon_names([{"pipewire.access.portal.app_id": "com.example.Music"}], "Music")
        self.assertLess(flatpak.index("com.example.Music"), flatpak.index("Music"))

    def test_loaders_get_a_games_icon_not_their_own(self):
        from warpipe.icons import GAMES_ICON, app_icon_names
        wine = app_icon_names([{"application.name": "Warframe.x64.exe",
                                "application.process.binary": "wine64-preloader"}], "Warframe.x64.exe")
        self.assertEqual(wine[0], GAMES_ICON)
        java = app_icon_names([{"node.name": "java", "application.name": "java"}], "java")
        self.assertEqual(java[0], GAMES_ICON)
        self.assertNotIn("java", java)
        self.assertEqual(app_icon_names([], "Spotify")[:2], ["Spotify", "spotify"])

    def test_desktop_entries(self):
        from warpipe.icons import DesktopIndex, app_icon_names
        with tempfile.TemporaryDirectory() as d:
            apps = Path(d)
            (apps / "org.example.Player.desktop").write_text(
                "[Desktop Entry]\nName=Player\nIcon=player-from-desktop\nStartupWMClass=PlayerWM\n"
                "Exec=flatpak run --branch=stable org.example.Player %U\n[Desktop Action new]\nIcon=nope\n")
            (apps / "cool.desktop").write_text("[Desktop Entry]\nIcon=/opt/cool/icon.png\n"
                                               "Exec=env GDK_BACKEND=x11 /opt/cool/bin/coolplayer --x\n")
            (apps / "jar.desktop").write_text("[Desktop Entry]\nIcon=jar-icon\nExec=java -jar app.jar\n")
            (apps / "hidden.desktop").write_text("[Desktop Entry]\nIcon=hidden-icon\nHidden=true\nExec=ghost\n")
            (apps / "broken.desktop").write_text("[Desktop Entry]\nIcon=b\nExec=\"unterminated\n")
            idx = DesktopIndex([apps, apps / "missing"])
            for name in ("Player", "org.example.Player", "playerwm", "PLAYER"):
                self.assertEqual(idx.icon_for(name), "player-from-desktop", name)
            self.assertEqual(idx.icon_for("coolplayer"), "/opt/cool/icon.png")
            self.assertIsNone(idx.icon_for("java"))          # a generic loader is no app
            self.assertIsNone(idx.icon_for("ghost"))
            self.assertIsNone(idx.icon_for(""))
            names = app_icon_names([{"application.process.binary": "coolplayer"}], "Cool Player", idx)
            self.assertEqual(names[0], "/opt/cool/icon.png")

    def test_resolve_icon_never_null_for_generic_names(self):
        from warpipe.icons import resolve_icon
        self.assertTrue(resolve_icon(["/nonexistent/x.png", "no-such-icon-really"]).isNull())


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class TestGreenTrayIcon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def _pixels(self, icon, size=22):
        img = icon.pixmap(size, size).toImage()
        return [img.pixelColor(x, y) for y in range(img.height()) for x in range(img.width())]

    def test_states_from_the_app_icon(self):
        from warpipe.icons import app_icon_file
        from warpipe.tray import load_tray_icon
        self.assertIsNotNone(app_icon_file(22))
        self.assertEqual(app_icon_file(22).parent.parent.name, "22x22")    # pixel-fitted file
        self.assertEqual(app_icon_file(48).parent.parent.name, "scalable")
        active = self._pixels(load_tray_icon("dark", "active"))
        normal = self._pixels(load_tray_icon("dark", ""))
        off = self._pixels(load_tray_icon("dark", "off"))
        green = [c for c in active if c.alpha() > 200 and c.green() > c.red() + 60]
        self.assertGreater(len(green), 40)                                  # green like the app icon
        self.assertNotEqual([c.rgba() for c in active], [c.rgba() for c in normal])   # faded waves
        self.assertGreater(sum(c.alpha() for c in active), sum(c.alpha() for c in normal))
        self.assertTrue(any(c.alpha() > 200 for c in off))
        self.assertTrue(all(abs(c.red() - c.green()) <= 2 and abs(c.green() - c.blue()) <= 2
                            for c in off if c.alpha() > 0))                 # grey
        icon = load_tray_icon("light", "off")
        for s in (16, 22, 24, 32):
            self.assertIn(s, [z.width() for z in icon.availableSizes()])

    def test_app_icons_dir_when_installed(self):
        """install.sh: <appdir>/warpipe (package) + <appdir>/icons/hicolor/<size>/apps/*.svg."""
        from warpipe.icons import app_icon_file, app_icons_dir
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "warpipe-app"
            (app / "warpipe").mkdir(parents=True)
            self.assertIsNone(app_icons_dir(app / "warpipe"))
            apps = app / "icons" / "hicolor" / "scalable" / "apps"
            apps.mkdir(parents=True)
            hicolor = app_icons_dir(app / "warpipe")
            self.assertEqual(hicolor, app / "icons" / "hicolor")
            self.assertIsNone(app_icon_file(22, hicolor))
            (apps / "io.github.gawson.Warpipe.svg").write_text("<svg/>")
            self.assertEqual(app_icon_file(22, hicolor), apps / "io.github.gawson.Warpipe.svg")

    def test_fade_waves_touches_only_the_waves(self):
        from warpipe.icons import app_icon_file, fade_waves
        for size in (16, 22, 128):
            svg = app_icon_file(size).read_text()
            faded = fade_waves(svg, 0.35)
            self.assertEqual(faded.count('opacity="0.35"'), 1, size)
            self.assertEqual(svg.count("<rect"), faded.count("<rect"))


@unittest.skipUnless(HAVE_QT, "PySide6 missing")
class PopupCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.controllers = []

    def tearDown(self):
        for c in self.controllers:
            c.popup.hide()
        i18n.set_language("en")
        self.tmp.cleanup()

    def enforcer(self, b, name="c", **kw) -> Enforcer:
        return Enforcer(FakePw(b), Config(self.dir / f"{name}.json"), SnapshotStore(self.dir / f"{name}-s.json"), **kw)

    def controller(self, enf):
        from warpipe.tray import TrayController
        ctl = TrayController(enf)
        self.controllers.append(ctl)
        return ctl

    def wait(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        return cond()


class TestPopupModel(PopupCase):
    def rows(self, model):
        return {r.key: r for r in model.rows}

    def test_whole_system(self):
        from warpipe.popup import PILL_OK, ROW_NO_PID, ROW_ON_STREAM, popup_model
        b, _ns = per_app()
        st = self.enforcer(b).refresh()
        m = popup_model(st)
        self.assertEqual((m.pill, m.pill_kind), ("Discord Go Live: audio active", PILL_OK))
        self.assertFalse(m.enabled)
        self.assertEqual(m.icon_state, "off")
        self.assertEqual(m.notices, ())
        self.assertEqual([r.key for r in m.rows], ["Firefox", "java", "Warframe.x64.exe"])
        rows = self.rows(m)
        # Discord's default: every app with a PID is on stream, the PID-less one is not
        self.assertEqual((rows["Firefox"].state, rows["Firefox"].subtitle), (ROW_ON_STREAM, "on stream"))
        self.assertTrue(rows["Firefox"].on_stream)
        self.assertEqual(rows["Warframe.x64.exe"].state, ROW_ON_STREAM)
        self.assertEqual((rows["java"].state, rows["java"].subtitle),
                         (ROW_NO_PID, "Discord can't capture this — Warpipe adds it"))
        self.assertFalse(any(r.checked for r in m.rows))
        # media.name only in the tooltip
        self.assertIn("SEKRETNY", rows["Firefox"].tooltip)
        self.assertNotIn("SEKRETNY", rows["Firefox"].title + rows["Firefox"].subtitle)
        self.assertNotIn("WEBRTC", " ".join(r.title for r in m.rows))

    def test_only_selected_with_a_remembered_app(self):
        from warpipe.popup import ROW_NO_PID, ROW_ON_STREAM, ROW_PLAYING, ROW_REMEMBERED, popup_model
        b, ns = per_app()
        st = self.enforcer(b, enabled=True, selected=["Spotify", "Warframe.x64.exe"]).refresh()
        m = popup_model(st)
        self.assertTrue(m.enabled)
        self.assertEqual(m.icon_state, "active")
        rows = self.rows(m)
        self.assertEqual([r.key for r in m.rows], ["Firefox", "java", "Warframe.x64.exe", "Spotify"])
        self.assertEqual((rows["Warframe.x64.exe"].state, rows["Warframe.x64.exe"].checked), (ROW_ON_STREAM, True))
        self.assertEqual((rows["Firefox"].state, rows["Firefox"].subtitle), (ROW_PLAYING, "playing, not on stream"))
        self.assertEqual(rows["java"].state, ROW_NO_PID)
        sp = rows["Spotify"]
        self.assertEqual((sp.state, sp.subtitle, sp.present, sp.checked, sp.on_stream),
                         (ROW_REMEMBERED, "not playing · remembered", False, True, False))
        self.assertEqual(b.links_into(ns.cap_ff), set())

    def test_waiting_for_go_live_and_stream_count(self):
        from warpipe.popup import PILL_IDLE, ROW_WAITING, popup_model
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        b.app("Firefox", "firefox", media_name="second tab")
        st = self.enforcer(b, enabled=True, selected=["Firefox"]).refresh()
        m = popup_model(st)
        self.assertEqual((m.pill, m.pill_kind, m.icon_state), ("No Go Live audio", PILL_IDLE, ""))
        ff = self.rows(m)["Firefox"]
        self.assertEqual((ff.state, ff.subtitle), (ROW_WAITING, "selected · waiting for Go Live"))
        self.assertEqual(ff.title, "Firefox (2 streams)")

    def test_empty_selection_warning(self):
        from warpipe.popup import PILL_WARN, popup_model
        b, _ns = per_app()
        m = popup_model(self.enforcer(b, enabled=True, selected=[]).refresh())
        self.assertEqual(m.notices[0].kind, PILL_WARN)
        self.assertEqual(m.notices[0].text, "No app selected — stream has no audio")
        # not when enforcement is off
        self.assertEqual(popup_model(self.enforcer(b, "c2", enabled=False, selected=[]).refresh()).notices, ())

    def test_ambiguous_and_errors(self):
        from warpipe.popup import PILL_ERROR, PILL_WARN, popup_model
        b = GraphBuilder()
        ns = with_apps(b, base_system(b))
        for _i in range(2):
            c = b.capture(node_name="Discord", app_name="Discord", media_name="RecordStream")
            b.connect(ns.alsa, c, src_prefix="monitor")
        m = popup_model(self.enforcer(b).refresh())
        self.assertEqual((m.pill, m.pill_kind), ("Capture ambiguous", PILL_WARN))
        self.assertTrue(m.notices)
        m = popup_model(Status(None, [], True, ["Firefox"], error="pw-dump failed"))
        self.assertEqual((m.pill, m.pill_kind), ("PipeWire error", PILL_ERROR))
        self.assertIn("Error: pw-dump failed", [n.text for n in m.notices])

    def test_polish(self):
        from warpipe.popup import popup_model
        b, _ns = per_app()
        enf = self.enforcer(b, enabled=True, selected=["Warframe.x64.exe"])
        i18n.set_language("pl")
        m = popup_model(enf.refresh())
        rows = self.rows(m)
        self.assertEqual(m.pill, "Go Live z dźwiękiem")
        self.assertEqual(rows["Warframe.x64.exe"].subtitle, "na streamie")
        self.assertEqual(rows["Firefox"].subtitle, "gra, ale nie na streamie")


class TestPopupWindow(PopupCase):
    def test_left_click_toggles_the_window(self):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QSystemTrayIcon
        b, _ns = per_app()
        ctl = self.controller(self.enforcer(b))
        T = QSystemTrayIcon.ActivationReason
        self.assertFalse(ctl.popup.isVisible())
        with mock.patch.object(ctl.tray, "showMessage") as show:
            ctl._on_activated(T.Trigger)
            self.assertTrue(ctl.popup.isVisible())
            self.assertEqual(ctl.popup.windowTitle(), "Warpipe")
            self.assertEqual(ctl.popup.width(), 420)
            self.assertEqual([w.key for w in ctl.popup.rows], ["Firefox", "java", "Warframe.x64.exe"])
            ctl._on_activated(T.Context)                 # right click: the platform's menu
            self.assertTrue(ctl.popup.isVisible())
            ctl._on_activated(T.Trigger)                 # the next left click hides it
            self.assertFalse(ctl.popup.isVisible())
            ctl._on_activated(T.Trigger)
            self.assertTrue(ctl.popup.isVisible())
        show.assert_not_called()
        QTest.keyClick(ctl.popup, Qt.Key.Key_Escape)
        self.assertFalse(ctl.popup.isVisible())
        # quitting hides it too
        ctl.show_popup()
        with mock.patch.object(QApplication, "quit"):
            ctl._on_quit()
        self.assertFalse(ctl.popup.isVisible())

    def test_window_controls_drive_the_enforcer(self):
        b, ns = per_app()
        enf = self.enforcer(b)
        ctl = self.controller(enf)
        ctl.show_popup()
        pop = ctl.popup
        self.assertFalse(pop.master.isChecked())
        self.assertFalse(pop.system_button.isEnabled())
        pop.row("Warframe.x64.exe").switch.click()       # ticking an app turns enforcement on
        self.app.processEvents()
        self.assertTrue(enf.enabled)
        self.assertEqual(enf.selected, ["Warframe.x64.exe"])
        self.assertTrue(self.wait(lambda: pop.master.isChecked()))
        self.assertTrue(pop.system_button.isEnabled())
        self.assertEqual(pop.row("Firefox").sub.text(), "playing, not on stream")
        self.assertTrue(pop.row("Firefox").pipe.isHidden())
        self.assertFalse(pop.row("Warframe.x64.exe").pipe.isHidden())
        self.assertEqual(b.links_into(ns.cap_ff), set())
        pop.system_button.click()                        # Whole system
        self.app.processEvents()
        self.assertFalse(enf.enabled)
        self.assertTrue(self.wait(lambda: not pop.master.isChecked()))
        self.assertEqual(pop.row("Firefox").sub.text(), "on stream")
        pop.master.click()
        self.app.processEvents()
        self.assertTrue(enf.enabled)

    def test_empty_selection_warning_is_visible(self):
        b, _ns = per_app()
        ctl = self.controller(self.enforcer(b, enabled=True, selected=[]))
        ctl.show_popup()
        self.assertEqual(len(ctl.popup.notices), 1)
        notice = ctl.popup.notices[0]
        self.assertTrue(notice.isVisible())
        self.assertEqual(notice.label.text(), "No app selected — stream has no audio")
        ctl.popup.row("Firefox").switch.click()
        self.assertTrue(self.wait(lambda: not ctl.popup.notices))

    def test_live_update_from_another_thread(self):
        """Statuses come from the watcher/control threads; the window changes only in
        the GUI thread, through the queued signal."""
        b, _ns = per_app()
        enf = self.enforcer(b)
        ctl = self.controller(enf)
        ctl.show_popup()
        row = ctl.popup.row("Firefox")
        self.assertFalse(row.switch.isChecked())
        t = threading.Thread(target=lambda: enf.set_app_selected("Firefox", True))
        t.start()
        t.join(5)
        self.assertFalse(row.switch.isChecked())        # not yet: the GUI thread hasn't run
        self.assertTrue(self.wait(lambda: row.switch.isChecked()))
        self.assertTrue(ctl.popup.master.isChecked())
        self.assertEqual(ctl.popup.model.icon_state, "active")
        self.assertEqual(ctl.icon_key[1], "active")
        # a new app appears while the window is open
        b.app("Spotify", "spotify")
        enf.refresh("graph changed")
        self.assertTrue(self.wait(lambda: ctl.popup.row("Spotify") is not None))

    def assert_clean_layout(self, w, label="", rows=True):
        """Fixed width; every shown child inside the window; no two shown siblings overlap
        (window children, and the app rows); nothing removed is still painted."""
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QWidget

        from warpipe.popup import MAX_VISIBLE_ROWS, AppRowWidget, NoticeWidget

        def shown(parent):
            return [c for c in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
                    if not c.isHidden() and not c.isWindow()]

        self.assertEqual(w.width(), 420, label)
        for parent in (w, w.rows_widget) if rows else (w,):
            kids = shown(parent)
            area = QRect(0, 0, parent.width(), parent.height())
            for c in kids:
                self.assertTrue(area.contains(c.geometry()), (label, c, c.geometry(), area))
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    self.assertFalse(a.geometry().intersects(b.geometry()),
                                     (label, a, a.geometry(), b, b.geometry()))
        if rows and w.rows and len(w.rows) <= MAX_VISIBLE_ROWS:
            # every row fully visible: no clipping, no scroll bar
            self.assertGreaterEqual(w.scroll.viewport().height(), w.rows_layout.sizeHint().height(), label)
            self.assertFalse(w.scroll.verticalScrollBar().isVisible(), label)
        stale = [c for c in w.findChildren(NoticeWidget) if c not in w.notices]
        stale += [c for c in w.findChildren(AppRowWidget) if c not in w.rows]
        self.assertEqual([c for c in stale if not c.isHidden()], [], label)
        for n in w.notices:
            self.assertFalse(n.isHidden(), label)
            self.assertFalse(n.geometry().intersects(w.section.geometry()), label)
            self.assertLess(n.geometry().bottom(), w.section.geometry().top(), label)

    def test_layout_stays_clean_through_transitions(self):
        """Regression: the empty-selection notice stayed painted over "What the stream
        hears" after it should have gone (Whole system → nothing ticked → tick → …)."""
        b, ns = per_app()
        enf = self.enforcer(b, enabled=False, selected=[])
        ctl = self.controller(enf)
        ctl.show_popup()
        pop = ctl.popup
        heights = {}

        def step(label, action, notices):
            action()
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(len(pop.notices), notices, label)
            self.assert_clean_layout(pop, label)
            heights[label] = pop.height()

        step("whole system", lambda: None, 0)
        step("only selected, nothing ticked", lambda: pop.master.click(), 1)
        step("+warframe", lambda: pop.row("Warframe.x64.exe").switch.click(), 0)
        step("+firefox", lambda: pop.row("Firefox").switch.click(), 0)
        step("untick all", lambda: (pop.row("Firefox").switch.click(),
                                    self.app.processEvents(),
                                    pop.row("Warframe.x64.exe").switch.click()), 1)
        step("whole system again", lambda: pop.system_button.click(), 0)
        self.assertEqual(heights["whole system"], heights["+warframe"])
        self.assertEqual(heights["whole system"], heights["whole system again"])
        self.assertGreater(heights["only selected, nothing ticked"], heights["whole system"])
        self.assertEqual(heights["only selected, nothing ticked"], heights["untick all"])
        # apps appear and go away while the window is open; a very long name; no capture
        long = "A" * 40 + " very long application name that never fits.exe"
        step("new apps", lambda: (b.app(long, "longapp"), b.app("Spotify", "spotify"),
                                  enf.refresh("graph changed")), 0)
        self.assertEqual(len(pop.rows), 5)
        self.assertLess(pop.row(long).name.width(), 300)       # elided, not wider
        step("apps gone", lambda: (b.remove_node(ns.firefox), b.remove_node(ns.java),
                                   enf.refresh("graph changed")), 0)
        self.assertEqual(len(pop.rows), 3)
        step("Go Live ended", lambda: (b.remove_node(ns.cap_wf), b.remove_node(ns.cap_ff),
                                       enf.refresh("graph changed")), 0)
        self.assertEqual(pop.pill.text(), "No Go Live audio")

    def test_set_model_is_clean_without_an_event_loop(self):
        """Removed widgets go away at once — not only when deleteLater() gets its turn."""
        from warpipe.popup import PopupWindow, popup_model
        b, _ns = per_app()
        enf = self.enforcer(b, enabled=True, selected=[])
        w = PopupWindow()
        try:
            w.set_model(popup_model(enf.refresh()))
            self.assertEqual(len(w.notices), 1)
            h_notice = w.height()
            enf.set_app_selected("Firefox", True)
            w.set_model(popup_model(enf.refresh()))
            self.assertEqual(w.notices, [])
            # rows: positioned by the scroll area's own (deferred) resize — checked shown
            self.assert_clean_layout(w, "no event loop", rows=False)
            self.assertLess(w.height(), h_notice)
            measured = w.height()
            w.show()                               # sizes measured before showing are final
            self.app.processEvents()
            self.assertEqual(w.height(), measured)
            self.assert_clean_layout(w, "shown with rows")
            w.hide()
            w.set_model(popup_model(Status(None, [], True, [], error="x")))
            self.assertEqual(w.rows, [])
            self.assertFalse(w.empty.isHidden())
            self.assert_clean_layout(w, "no rows", rows=False)
            w.show()
            self.app.processEvents()
            self.assert_clean_layout(w, "shown")
            w.hide()
        finally:
            w.deleteLater()

    def test_hidden_window_is_not_rebuilt(self):
        b, _ns = per_app()
        enf = self.enforcer(b)
        ctl = self.controller(enf)
        with mock.patch.object(ctl.popup, "set_model") as set_model:
            enf.set_app_selected("Firefox", True)
            self.app.processEvents()
            self.app.processEvents()
        set_model.assert_not_called()

    def test_many_apps_scroll(self):
        from warpipe.popup import MAX_VISIBLE_ROWS
        b, _ns = per_app()
        for i in range(10):
            b.app(f"Player {i}", f"player{i}")
        ctl = self.controller(self.enforcer(b))
        ctl.show_popup()
        self.assertEqual(len(ctl.popup.rows), 13)
        row_h = ctl.popup.rows[0].sizeHint().height()
        self.assertLess(ctl.popup.scroll.height(), (MAX_VISIBLE_ROWS + 1) * row_h)
        self.assertLess(ctl.popup.height(), 700)

    def test_settings_menu_and_language(self):
        b, _ns = per_app()
        enf = self.enforcer(b)
        ctl = self.controller(enf)
        ctl.build_settings_menu()
        texts = [a.text() for a in ctl.settings_menu.actions() if not a.isSeparator()]
        self.assertEqual(texts, ["Keep Discord's own sounds off the stream", "Language", "Start at login", "Quit"])
        self.assertTrue(ctl.language_actions["auto"].isChecked())
        ctl.show_popup()
        ctl.language_actions["pl"].trigger()
        self.app.processEvents()
        self.assertEqual(i18n.language(), "pl")
        self.assertEqual(Config(self.dir / "c.json").language, "pl")
        self.assertEqual(ctl.popup.section.text(), "Co słychać na streamie")
        self.assertEqual(ctl.popup.system_button.text(), "Cały system")
        self.assertIn("Zakończ", [a.text() for a in ctl.menu.actions()])
        ctl.build_settings_menu()
        self.assertTrue(ctl.language_actions["pl"].isChecked())
        self.assertEqual(ctl.language_actions["pl"].text(), "Polski")
        ctl.set_language("en")
        self.assertEqual(ctl.popup.section.text(), "What the stream hears")

    def test_settings_menu_offers_the_stream_choice(self):
        b, ns = monitor_mode("ee_sink")
        enf = self.enforcer(b, enabled=True, selected=["Firefox"])
        enf.refresh()
        ctl = self.controller(enf)
        ctl.build_settings_menu()
        subs = [a.menu() for a in ctl.settings_menu.actions() if a.menu() is not None]
        self.assertEqual(subs[0].title(), "Which Discord stream?")
        enf.shutdown()

    def test_start_at_login(self):
        from warpipe import state
        cfg, data = self.dir / "xdg-config", self.dir / "xdg-data"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg), "XDG_DATA_HOME": str(data)}):
            b, _ns = per_app()
            ctl = self.controller(self.enforcer(b))
            self.assertFalse(state.autostart_enabled())
            self.assertFalse(ctl.act_autostart.isEnabled())      # not installed
            desktop = data / "applications" / "io.github.gawson.Warpipe.desktop"
            desktop.parent.mkdir(parents=True)
            desktop.write_text("[Desktop Entry]\nType=Application\nName=Warpipe\nExec=warpipe --tray\n")
            ctl.build_settings_menu()
            self.assertTrue(ctl.act_autostart.isEnabled())
            ctl.act_autostart.trigger()
            auto = cfg / "autostart" / "io.github.gawson.Warpipe.desktop"
            self.assertTrue(auto.is_file())
            self.assertIn("Exec=warpipe --tray", auto.read_text())
            self.assertIn("X-KDE-autostart-phase=2", auto.read_text())
            self.assertTrue(state.autostart_enabled())
            self.assertTrue(ctl.act_autostart.isChecked())
            ctl.act_autostart.trigger()
            self.assertFalse(auto.exists())
            self.assertFalse(ctl.act_autostart.isChecked())
            auto.write_text("[Desktop Entry]\nHidden=true\n")      # disabled in the system settings
            self.assertFalse(state.autostart_enabled())


if __name__ == "__main__":
    unittest.main()
