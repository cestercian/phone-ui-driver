#!/usr/bin/env python3
"""Operate an Android phone over adb: run a JSON list of steps, save a log and screenshots.

Usage:
  python3 run.py [--config FILE] <run-name> '<JSON steps>'
  python3 run.py [--config FILE] <run-name> @steps.json

The config (default: config.json next to this script; see config.example.json) names the one device, the apps the
driver may touch and the labels it must never tap. There is no device discovery and no default app.
Output: <output_dir>/<run-name>/log.txt and NN.jpg screenshots (NN.png where `sips` is not available).

Steps (each a JSON object with "op"):
  focus                         print the app and screen in front
  start  [package]              bring an allowed app to the front (default: the first allowed package)
  shot                          screenshot
  tree                          print the on-screen elements that carry text, a description or an id
  tap    <match> or x, y        tap the matched element (dx, dy in 0..1 pick a point inside it; default its centre)
  press  <match> or x, y [ms]   long press (default 700 ms)
  type   text                   type into the focused field (letters, digits, space and . , : / - _ ? ! only)
  key    code                   BACK, ENTER, DEL
  swipe  x1, y1, x2, y2 [ms]
  wait   s
  waitfor <match> [timeout]     wait until an element matches (default 10 s)
Match keys: id (resource id, with or without the package prefix), text, textContains, desc, descContains, cls, nth,
minY, maxY.

Guards, all fail closed:
  - the phone must be awake and unlocked; the driver never wakes or unlocks it;
  - every tap, press, type, key and swipe needs an allowed app in front; a permission dialog or any other app in
    front stops the run;
  - a tap or press is checked against the matched element and against what lies under the point (the smallest
    element there, every clickable element around it, and everything inside the innermost clickable one): each
    must belong to an allowed app and none may carry a forbidden label;
  - the first step that cannot be done stops the run, with a screenshot of where it stopped.
Exit status: 0 all steps done, 1 stopped, 2 bad arguments.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FORBIDDEN = ["call", "pay", "payment", "purchase", "buy", "delete", "block", "report", "log out", "sign out"]
TYPEABLE = re.compile(r"^[A-Za-z0-9 .,:/\-_?!]*$")
KEYS = {"BACK": "KEYCODE_BACK", "ENTER": "KEYCODE_ENTER", "DEL": "KEYCODE_DEL"}
MATCH_KEYS = ("id", "text", "textContains", "desc", "descContains", "cls")


class Stop(Exception):
    pass


def forbidden_pattern(words: list[str]) -> str:
    """One regular expression for the forbidden labels: whole words, an optional plural s. '' when there are none.
    Written so that Python's re and ICU (the iOS driver) read it the same way."""
    if not words:
        return ""
    return r"\b(?:" + "|".join(re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", w.strip()) for w in words) + r")s?\b"


def mask(s: str) -> str:
    """Long runs of digits (phone numbers, one-time codes) do not go into the log."""
    return re.sub(r"\d[\d ]{5,}\d", "<digits>", s)


class Config:
    def __init__(self, path: Path):
        try:
            cfg = json.loads(path.read_text())
        except FileNotFoundError:
            raise Stop(f"no config at {path}: copy config.example.json to config.json and fill it in")
        except ValueError as e:
            raise Stop(f"{path} is not valid JSON: {e}")
        if not isinstance(cfg, dict):
            raise Stop(f"{path} must hold a JSON object")
        self.serial = cfg.get("serial")
        if not isinstance(self.serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{4,64}", self.serial):
            raise Stop("config: \"serial\" must be the device's adb serial (see `adb devices`); the driver does not pick a device itself")
        self.allowed = cfg.get("allowed_packages")
        if not (isinstance(self.allowed, list) and self.allowed
                and all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+", p) for p in self.allowed)):
            raise Stop("config: \"allowed_packages\" must be a non-empty list of package names")
        words = cfg.get("forbidden_labels", DEFAULT_FORBIDDEN)
        if not (isinstance(words, list) and all(isinstance(w, str) and w.strip() for w in words)):
            raise Stop("config: \"forbidden_labels\" must be a list of words or phrases (an empty list switches the check off)")
        pattern = forbidden_pattern(words)
        self.forbidden = re.compile(pattern, re.I) if pattern else None
        self.adb = cfg.get("adb", "adb")
        self.out = Path(cfg.get("output_dir", "~/phone-ui-driver-runs/android")).expanduser()
        if not isinstance(self.adb, str) or not self.adb:
            raise Stop("config: \"adb\" must be the path of the adb program")


class Phone:
    def __init__(self, cfg: Config, out: Path):
        self.cfg = cfg
        self.out = out
        self.shots = 0
        self.logf = open(out / "log.txt", "w")

    def log(self, msg: str):
        print(msg)
        self.logf.write(msg + "\n")
        self.logf.flush()

    def adb(self, *args: str, binary: bool = False, timeout: int = 60):
        try:
            r = subprocess.run([self.cfg.adb, "-s", self.cfg.serial, *args], capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise Stop(f"adb not found at {self.cfg.adb!r}: install the Android platform tools or set \"adb\" in the config")
        if r.returncode != 0:
            raise Stop(f"adb {' '.join(args[:3])} failed: {r.stderr.decode(errors='replace').strip()[:200]}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    # --- state
    def awake_and_unlocked(self):
        if "mWakefulness=Awake" not in self.adb("shell", "dumpsys", "power"):
            raise Stop("the phone's screen is off; the driver does not wake it")
        if re.search(r"isKeyguardShowing=true", self.adb("shell", "dumpsys", "window")):
            raise Stop("the phone is locked; the driver does not unlock it")

    def front(self) -> tuple[str, str]:
        out = self.adb("shell", "dumpsys", "activity", "activities")
        m = (re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ ([\w.]+)/([\w.$]+)", out)
             or re.search(r"mResumedActivity: ActivityRecord\{\S+ \S+ ([\w.]+)/([\w.$]+)", out))
        if not m:
            raise Stop("cannot tell which app is in front")
        return m.group(1), m.group(2)

    def need_allowed_front(self, what: str):
        pkg, act = self.front()
        if pkg not in self.cfg.allowed:
            raise Stop(f"{what} refused: {pkg}/{act} is in front, not an allowed app")

    # --- screen contents
    def nodes(self) -> list[dict]:
        for _ in range(4):
            raw = self.adb("exec-out", "uiautomator", "dump", "/dev/tty")
            i, j = raw.find("<?xml"), raw.rfind("</hierarchy>")
            if i >= 0 and j > i:
                try:
                    root = ET.fromstring(raw[i:j + len("</hierarchy>")])
                except ET.ParseError:
                    time.sleep(0.5)
                    continue
                out = []
                flags = (("c", "clickable"), ("l", "long-clickable"), ("f", "focused"), ("s", "selected"), ("k", "checked"))

                def visit(n) -> str:
                    """Appends the element (document order) and returns the labels of it and everything inside it."""
                    d = None
                    m = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", n.get("bounds", ""))
                    if m:
                        x1, y1, x2, y2 = map(int, m.groups())
                        if x2 > x1 and y2 > y1:
                            d = {"id": n.get("resource-id", ""), "text": n.get("text", ""), "desc": n.get("content-desc", ""),
                                 "cls": n.get("class", "").rsplit(".", 1)[-1], "pkg": n.get("package", ""), "b": (x1, y1, x2, y2),
                                 "flags": "".join(k for k, a in flags if n.get(a) == "true")}
                            out.append(d)
                    label = " ".join((n.get("resource-id", "").rsplit("/", 1)[-1].replace("_", " "), n.get("text", ""), n.get("content-desc", "")))
                    whole = " ".join([label] + [visit(c) for c in n.findall("node")])
                    if d is not None:
                        d["label"], d["sub"] = label, whole
                    return whole

                for top in root.findall("node"):
                    visit(top)
                return out
            time.sleep(0.6)
        raise Stop("the screen contents could not be read (uiautomator dump failed four times)")

    @staticmethod
    def matches(n: dict, s: dict) -> bool:
        if "id" in s and not (n["id"] == s["id"] or n["id"].endswith(":id/" + s["id"])):
            return False
        if "text" in s and n["text"] != s["text"]:
            return False
        if "textContains" in s and s["textContains"].lower() not in n["text"].lower():
            return False
        if "desc" in s and n["desc"] != s["desc"]:
            return False
        if "descContains" in s and s["descContains"].lower() not in n["desc"].lower():
            return False
        if "cls" in s and n["cls"] != s["cls"]:
            return False
        if "minY" in s and n["b"][1] < s["minY"]:
            return False
        if "maxY" in s and n["b"][3] > s["maxY"]:
            return False
        return True

    def pick(self, nodes: list[dict], s: dict) -> dict | None:
        if not any(k in s for k in MATCH_KEYS):
            raise Stop(f"step has no match key: {json.dumps(s)}")
        hits = [n for n in nodes if self.matches(n, s)]
        nth = int(s.get("nth", 0))
        return hits[nth] if -len(hits) <= nth < len(hits) else None

    # --- guards on what gets touched
    def names_forbidden(self, n: dict, whole: bool = False) -> bool:
        """Own id, text and description; with whole=True also those of everything inside the element."""
        return self.cfg.forbidden is not None and bool(self.cfg.forbidden.search(n["sub"] if whole else n["label"]))

    def check_element(self, n: dict, what: str, which: str):
        if n["pkg"] not in self.cfg.allowed:
            raise Stop(f"{what} refused: {which} belongs to {n['pkg']}")
        if self.names_forbidden(n):
            raise Stop(f"{what} refused: {which} names a forbidden action")

    def check_point(self, x: int, y: int, nodes: list[dict], what: str):
        """What a touch at x, y would hit: the smallest element there and the clickable elements around it. The
        innermost clickable one is the control that takes the touch, so the labels inside it count as well."""
        def area(n):
            return (n["b"][2] - n["b"][0]) * (n["b"][3] - n["b"][1])

        under = [n for n in nodes if n["b"][0] <= x < n["b"][2] and n["b"][1] <= y < n["b"][3]]
        if not under:
            raise Stop(f"{what} refused: no element at {x},{y}")
        clickable = [n for n in under if "c" in n["flags"] or "l" in n["flags"]]
        for n in [min(under, key=area)] + clickable:
            self.check_element(n, what, f"the element at {x},{y}")
        if clickable and self.names_forbidden(min(clickable, key=area), whole=True):
            raise Stop(f"{what} refused: the control at {x},{y} holds a forbidden label")

    def point(self, s: dict, what: str) -> tuple[int, int]:
        nodes = self.nodes()
        if "x" in s and "y" in s:
            x, y = int(s["x"]), int(s["y"])
        else:
            n = self.pick(nodes, s)
            if n is None:
                raise Stop(f"{what}: nothing matches {json.dumps({k: v for k, v in s.items() if k != 'op'})}")
            self.check_element(n, what, "the matched element")
            x1, y1, x2, y2 = n["b"]
            dx, dy = (min(max(float(s.get(k, 0.5)), 0.0), 0.999) for k in ("dx", "dy"))
            x, y = x1 + int((x2 - x1) * dx), y1 + int((y2 - y1) * dy)
        self.check_point(x, y, nodes, what)
        return x, y

    # --- steps
    def step(self, s: dict):
        op = s.get("op")
        if op == "focus":
            self.log("front: %s/%s" % self.front())
        elif op == "start":
            pkg = s.get("package", self.cfg.allowed[0])
            if pkg not in self.cfg.allowed:
                raise Stop(f"start refused: {pkg} is not an allowed app")
            self.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(1.5)
            self.log("started %s; front: %s/%s" % ((pkg,) + self.front()))
        elif op == "shot":
            self.shots += 1
            png = self.out / f"{self.shots:02d}.png"
            png.write_bytes(self.adb("exec-out", "screencap", "-p", binary=True))
            jpg = png.with_suffix(".jpg")
            if shutil.which("sips"):
                subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "60", "-Z", "1200", str(png), "--out", str(jpg)],
                               capture_output=True)
            if jpg.is_file():
                png.unlink()
            self.log(f"shot {jpg.name if jpg.is_file() else png.name}")
        elif op == "tree":
            pkg, act = self.front()
            self.log(f"tree ({pkg}/{act}):")
            for n in self.nodes():
                if n["text"] or n["desc"] or n["id"]:
                    self.log("  %-12s %-34s t=%-42r d=%-34r %s %s" % (n["cls"][:12], n["id"].rsplit("/", 1)[-1][:34],
                                                                   mask(n["text"])[:40], mask(n["desc"])[:32], n["b"], n["flags"]))
        elif op in ("tap", "press"):
            self.need_allowed_front(op)
            x, y = self.point(s, op)
            if op == "tap":
                self.adb("shell", "input", "tap", str(x), str(y))
            else:
                self.adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(int(s.get("ms", 700))))
            self.log(f"{op} {x},{y}")
            time.sleep(float(s.get("after", 0.8)))
        elif op == "type":
            self.need_allowed_front(op)
            t = s.get("text", "")
            if not isinstance(t, str) or not t or not TYPEABLE.fullmatch(t):
                raise Stop("type: only letters, digits, space and . , : / - _ ? ! can be typed")
            self.adb("shell", "input", "text", "'" + t.replace(" ", "%s") + "'")
            self.log(f"typed {len(t)} characters")
            time.sleep(0.5)
        elif op == "key":
            self.need_allowed_front(op)
            code = KEYS.get(str(s.get("code", "")).upper())
            if not code:
                raise Stop(f"key: only {', '.join(KEYS)}")
            self.adb("shell", "input", "keyevent", code)
            self.log(f"key {code}")
            time.sleep(0.6)
        elif op == "swipe":
            self.need_allowed_front(op)
            try:
                xy = [str(int(s[k])) for k in ("x1", "y1", "x2", "y2")]
            except (KeyError, TypeError, ValueError):
                raise Stop("swipe needs x1, y1, x2, y2")
            self.adb("shell", "input", "swipe", *xy, str(int(s.get("ms", 300))))
            self.log("swipe")
            time.sleep(0.8)
        elif op == "wait":
            time.sleep(float(s.get("s", 1)))
        elif op == "waitfor":
            end = time.time() + float(s.get("timeout", 10))
            while True:
                if self.pick(self.nodes(), s) is not None:
                    self.log("waitfor: found")
                    break
                if time.time() > end:
                    raise Stop(f"waitfor: nothing matched within the timeout: {json.dumps({k: v for k, v in s.items() if k != 'op'})}")
                time.sleep(0.7)
        else:
            raise Stop(f"unknown op: {op!r}")

    def run(self, steps: list[dict]) -> int:
        self.awake_and_unlocked()
        for i, s in enumerate(steps, 1):
            try:
                self.step(s)
            except Stop as e:
                self.log(f"STOP at step {i} ({s.get('op')}): {e}")
                try:
                    self.step({"op": "shot"})
                except Stop:
                    pass
                return 1
        self.log(f"done: {len(steps)} steps; output in {self.out}")
        return 0


def read_steps(arg: str) -> list[dict]:
    steps = json.loads(Path(arg[1:]).read_text() if arg.startswith("@") else arg)
    if not isinstance(steps, list) or not all(isinstance(x, dict) for x in steps):
        raise ValueError("steps must be a JSON list of objects")
    return steps


def main(argv: list[str]) -> int:
    args = argv[1:]
    config = HERE / "config.json"
    if len(args) >= 2 and args[0] == "--config":
        config, args = Path(args[1]), args[2:]
    if len(args) != 2 or not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", args[0]):
        print(__doc__)
        return 2
    try:
        cfg = Config(config)
        steps = read_steps(args[1])
        out = cfg.out / args[0]
        out.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        print(f"STOP: a run named {args[0]} exists already; pick a new name", file=sys.stderr)
        return 2
    except (Stop, ValueError, OSError) as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2
    try:
        return Phone(cfg, out).run(steps)
    except (Stop, subprocess.TimeoutExpired) as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
