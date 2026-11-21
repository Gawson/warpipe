"""OpenDeck plugin: key rendering (Qt offscreen, no event loop)."""

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    # QApplication (not QGuiApplication): the same test process also uses QMenu in test_tray.
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

import helpers  # noqa: F401
from fake_opendeck import decode_data_url, png_size, png_text

# Regex from OpenDeck 2.14 (src/lib/rendererHelper.ts, getImage)
OPENDECK_BASE64_RE = re.compile(
    r"^data:image/(apng|avif|gif|jpeg|png|svg\+xml|webp|bmp|x-icon|tiff);base64,([A-Za-z0-9+/]+={0,2})?$")


@unittest.skipUnless(HAVE_QT, "no PySide6")
class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])
        from warpipe_plugin import logic as L
        from warpipe_plugin import render as R
        cls.L, cls.R = L, R

    def all_visuals(self):
        L = self.L
        V = L.Visual
        return [
            V("app", L.S_UNSET, L.UNSET_LABEL, ""),
            V("app", L.S_DOWN, "Warframe", "start"),
            V("app", L.S_STARTING, "Warframe", "starting…"),
            V("app", L.S_ERROR, "Warframe", "error"),
            V("app", L.S_ABSENT, "Warframe", "not playing"),
            V("app", L.S_ABSENT, "Warframe", "not playing", True),
            V("app", L.S_NOSTREAM, "Warframe", "no stream"),
            V("app", L.S_NOSTREAM, "Warframe", "no stream", True),
            V("app", L.S_UNSURE, "Warframe", "unsure"),
            V("app", L.S_OFF, "Warframe", ""),
            V("app", L.S_OFF, "Warframe", "", True),
            V("app", L.S_ON, "Warframe", "on stream"),
            V("system", L.S_SYS_ON, "Whole system", "on"),
            V("system", L.S_SYS_OFF, "Whole system", "only selected"),
            V("system", L.S_DOWN, "Whole system", "start"),
            # Polish translation (non-ASCII text goes into iTXt metadata)
            V("system", L.S_SYS_OFF, "Cały system", "tylko wybrane"),
        ]

    def test_png_data_urls_accepted_by_opendeck(self):
        r = self.R.Renderer()
        urls = set()
        for v in self.all_visuals():
            url = r.url(v)
            self.assertRegex(url, OPENDECK_BASE64_RE)
            png = decode_data_url(url)
            self.assertEqual(png_size(png), (144, 144))
            meta = png_text(png)
            self.assertEqual(meta["ddstate"], v.tag)
            self.assertEqual(meta["ddlabel"], v.label)       # also with non-ASCII text (iTXt)
            self.assertEqual(meta["ddbadge"], v.badge)
            self.assertEqual(meta["ddring"], "1" if v.ring else "0")
            self.assertLess(len(url), 40_000)                # the image is stored in the OpenDeck profile
            urls.add(url)
        self.assertEqual(len(urls), len(self.all_visuals()))  # every state looks different

    def test_cache(self):
        r = self.R.Renderer()
        v = self.all_visuals()[0]
        self.assertIs(r.url(v), r.url(v))
        self.assertEqual(r.renders, 1)

    def test_colours(self):
        L, R = self.L, self.R
        on = R.render(L.Visual("app", L.S_ON, "Warframe", "on stream"))
        absent = R.render(L.Visual("app", L.S_ABSENT, "Warframe", "not playing"))
        down = R.render(L.Visual("app", L.S_DOWN, "Warframe", "start"))
        ring = R.render(L.Visual("app", L.S_OFF, "Warframe", "", True))
        plain = R.render(L.Visual("app", L.S_OFF, "Warframe", ""))

        def rgb(img, x, y):
            c = img.pixelColor(x, y)
            return c.red(), c.green(), c.blue()

        r, g, b = rgb(on, 10, 60)
        self.assertGreater(g, r + 40)          # green = on stream
        r, g, b = rgb(absent, 10, 60)
        self.assertLess(max(r, g, b), 40)      # dimmed
        r, g, b = rgb(down, 10, 60)
        self.assertGreater(r, g + 30)          # red = the tool is not running
        r, g, b = rgb(ring, 3, 72)
        self.assertGreater(g, 150)             # green ring = selected
        r, g, b = rgb(plain, 3, 72)
        self.assertLess(g, 100)

    def test_long_label_fits(self):
        L, R = self.L, self.R
        img = R.render(L.Visual("app", L.S_OFF, "Bardzo-długa-nazwa-aplikacji-która-się-nie-mieści.exe", ""))
        self.assertEqual((img.width(), img.height()), (144, 144))

    def test_icons(self):
        img = self.R.render_icon("app", 40)
        self.assertEqual(img.width(), 40)
        self.assertGreater(img.pixelColor(20, 20).alpha() + img.pixelColor(14, 20).alpha(), 0)


if __name__ == "__main__":
    unittest.main()
