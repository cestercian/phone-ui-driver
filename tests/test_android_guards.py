"""Unit tests for the Android driver's guards. No phone is needed: adb is replaced by canned answers.

Run: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "android"))
import run as driver  # noqa: E402

APP = "com.example.app"


def el(pkg, bounds, *children, text="", desc="", rid="", cls="android.widget.TextView", clickable=False):
    x1, y1, x2, y2 = bounds
    return (f'<node package="{pkg}" class="{cls}" text="{text}" content-desc="{desc}" resource-id="{rid}" '
            f'clickable="{str(clickable).lower()}" bounds="[{x1},{y1}][{x2},{y2}]">' + "".join(children) + "</node>")


def screen(*tops):
    return "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">" + "".join(tops) + "</hierarchy>"


# A row with a text in it, a button whose label "Pay" sits on a child, a plain button, a control named only by its
# resource id, and an element that belongs to another app (as a permission dialog would).
DEFAULT_SCREEN = screen(el(
    APP, (0, 0, 1080, 2400),
    el(APP, (0, 200, 1080, 400), el(APP, (40, 240, 600, 300), text="Hello there"), rid=f"{APP}:id/row", clickable=True),
    el(APP, (0, 600, 540, 800), el(APP, (100, 660, 300, 740), text="Pay"), rid=f"{APP}:id/action", clickable=True),
    el(APP, (540, 600, 1080, 800), rid=f"{APP}:id/next", text="Next", clickable=True),
    el(APP, (0, 1000, 1080, 1200), rid=f"{APP}:id/menuitem_delete", clickable=True),
    el("com.android.permissioncontroller", (100, 1400, 980, 1600), text="Allow", clickable=True),
    cls="android.widget.FrameLayout"))


class FakePhone(driver.Phone):
    def __init__(self, cfg, out, front=APP, xml=DEFAULT_SCREEN, awake=True, locked=False):
        super().__init__(cfg, out)
        self.front_pkg, self.xml, self.awake, self.locked = front, xml, awake, locked
        self.sent = []

    def adb(self, *args, binary=False, timeout=60):
        if args[:3] == ("shell", "dumpsys", "power"):
            return "mWakefulness=Awake" if self.awake else "mWakefulness=Asleep"
        if args[:3] == ("shell", "dumpsys", "window"):
            return f"isKeyguardShowing={'true' if self.locked else 'false'}"
        if args[:3] == ("shell", "dumpsys", "activity"):
            return f"  topResumedActivity=ActivityRecord{{abc u0 {self.front_pkg}/.Main t12}}"
        if args[:2] == ("exec-out", "uiautomator"):
            return "junk before " + self.xml + " UI hierchary dumped to: /dev/tty"
        if args[:2] == ("exec-out", "screencap"):
            return b"\x89PNG fake"
        if args[:2] == ("shell", "input") or args[:2] == ("shell", "monkey"):
            self.sent.append(args[1:])
            return ""
        raise AssertionError(f"unexpected adb call: {args}")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.runs = 0

    def config(self, **over):
        cfg = {"serial": "TESTSERIAL01", "allowed_packages": [APP], "output_dir": str(self.dir / "out")}
        cfg.update(over)
        p = self.dir / "config.json"
        p.write_text(json.dumps(cfg))
        return driver.Config(p)

    def phone(self, cfg=None, **kw):
        self.runs += 1
        out = self.dir / f"run{self.runs}"
        out.mkdir()
        p = FakePhone(cfg or self.config(), out, **kw)
        self.addCleanup(p.logf.close)
        return p

    def log(self, phone):
        phone.logf.flush()
        return (phone.out / "log.txt").read_text()

    def taps(self, phone):
        return [a for a in phone.sent if a[:2] == ("input", "tap")]

    def test_tap_on_a_plain_button_goes_through(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "text": "Next"}]), 0)
        self.assertEqual(self.taps(p), [("input", "tap", "810", "700")])

    def test_another_app_in_front_stops_the_run(self):
        p = self.phone(front="com.android.permissioncontroller")
        self.assertEqual(p.run([{"op": "tap", "text": "Next"}, {"op": "tap", "text": "Next"}]), 1)
        self.assertEqual(p.sent, [])
        self.assertIn("is in front, not an allowed app", self.log(p))

    def test_type_key_and_swipe_need_an_allowed_app_in_front_too(self):
        for step in ({"op": "type", "text": "hello"}, {"op": "key", "code": "BACK"},
                     {"op": "swipe", "x1": 1, "y1": 1, "x2": 2, "y2": 2}):
            p = self.phone(front="com.other.app")
            self.assertEqual(p.run([step]), 1, step)
            self.assertEqual(p.sent, [], step)

    def test_matched_element_with_a_forbidden_label_is_not_tapped(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "text": "Pay"}]), 1)
        self.assertEqual(p.sent, [])
        self.assertIn("names a forbidden action", self.log(p))

    def test_forbidden_word_in_a_resource_id_counts(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "id": "menuitem_delete"}]), 1)
        self.assertEqual(p.sent, [])

    def test_raw_point_is_checked_against_the_element_under_it(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "x": 200, "y": 700}]), 1)     # on the text "Pay"
        self.assertEqual(p.run([{"op": "tap", "x": 500, "y": 620}]), 1)     # inside the Pay button, beside its text
        self.assertIn("holds a forbidden label", self.log(p))
        self.assertEqual(p.run([{"op": "press", "x": 300, "y": 1100}]), 1)  # the control with a forbidden id
        self.assertEqual(p.sent, [])
        self.assertEqual(p.run([{"op": "tap", "x": 800, "y": 700}, {"op": "tap", "x": 700, "y": 300}]), 0)
        self.assertEqual(self.taps(p), [("input", "tap", "800", "700"), ("input", "tap", "700", "300")])

    def test_container_whose_label_sits_on_a_child_is_refused(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "id": "action"}]), 1)
        self.assertEqual(p.sent, [])

    def test_row_with_a_forbidden_word_in_its_text_is_refused_wherever_it_is_tapped(self):
        xml = screen(el(APP, (0, 0, 1080, 2400),
                        el(APP, (0, 200, 1080, 400), el(APP, (40, 240, 600, 300), text="please call me back"), clickable=True)))
        p = self.phone(xml=xml)
        self.assertEqual(p.run([{"op": "tap", "x": 900, "y": 350}]), 1)
        self.assertEqual(p.sent, [])

    def test_raw_point_on_another_apps_element_is_refused(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "x": 500, "y": 1500}]), 1)
        self.assertEqual(p.sent, [])
        self.assertIn("belongs to com.android.permissioncontroller", self.log(p))

    def test_matched_element_of_another_app_is_refused(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "text": "Allow"}]), 1)
        self.assertEqual(p.sent, [])

    def test_plural_and_whole_word_rules(self):
        cfg = self.config(forbidden_labels=["call", "log out"])
        rx = cfg.forbidden
        for hit in ("Call", "Calls", "Voice call", "LOG OUT"):
            self.assertTrue(rx.search(hit), hit)
        for miss in ("Recall", "Callback", "Catalog outline", "Pay"):
            self.assertFalse(rx.search(miss), miss)

    def test_empty_forbidden_list_switches_the_label_check_off_only(self):
        p = self.phone(self.config(forbidden_labels=[]))
        self.assertEqual(p.run([{"op": "tap", "text": "Pay"}]), 0)
        self.assertEqual(p.run([{"op": "tap", "text": "Allow"}]), 1)

    def test_screen_off_or_locked_stops_before_the_first_step(self):
        for kw in ({"awake": False}, {"locked": True}):
            p = self.phone(**kw)
            with self.assertRaises(driver.Stop):
                p.run([{"op": "tap", "text": "Next"}])
            self.assertEqual(p.sent, [])

    def test_typing_is_limited_to_shell_safe_characters(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "type", "text": "hello; rm -rf $HOME"}]), 1)
        self.assertEqual(p.sent, [])
        self.assertEqual(p.run([{"op": "type", "text": "hello world 1"}]), 0)
        self.assertEqual(p.sent, [("input", "text", "'hello%sworld%s1'")])

    def test_start_only_launches_an_allowed_app(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "start", "package": "com.other.app"}]), 1)
        self.assertEqual(p.sent, [])
        self.assertEqual(p.run([{"op": "start"}]), 0)
        self.assertEqual(p.sent[0][:3], ("monkey", "-p", APP))

    def test_a_step_without_a_match_key_is_refused(self):
        p = self.phone()
        self.assertEqual(p.run([{"op": "tap", "nth": 0}]), 1)
        self.assertEqual(p.sent, [])

    def test_tree_masks_long_digit_runs(self):
        xml = screen(el(APP, (0, 0, 1080, 2400), text="Code 123 456 789 sent to +1 555 010 0199"))
        p = self.phone(xml=xml)
        self.assertEqual(p.run([{"op": "tree"}]), 0)
        log = self.log(p)
        self.assertIn("<digits>", log)
        self.assertNotIn("555", log)

    def test_config_must_name_a_device_and_apps(self):
        for bad in ({"serial": "<the device's serial, from `adb devices`>"}, {"allowed_packages": []},
                    {"allowed_packages": "com.example.app"}, {"forbidden_labels": "pay"}):
            with self.assertRaises(driver.Stop, msg=str(bad)):
                self.config(**bad)
        with self.assertRaises(driver.Stop):
            driver.Config(self.dir / "missing.json")


if __name__ == "__main__":
    unittest.main()
