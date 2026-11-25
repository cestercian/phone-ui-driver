#!/usr/bin/env python3
"""Run a JSON list of UI steps on an iPhone or an iOS simulator through the XCUITest driver; save a log and screenshots.

Usage:
  python3 run.py [--config FILE] build                       generate the Xcode project and build the driver
  python3 run.py [--config FILE] <run-name> '<JSON steps>'   run steps (see UITests/Driver.swift for the list)
  python3 run.py [--config FILE] <run-name> @steps.json

The config (default: config.json next to this script; see config.example.json) names the one device or simulator,
the apps the driver may target and the labels it must never tap. There is no device discovery, so a second phone
that happens to be paired with this Mac is never picked up.
Output: <output_dir>/<run-name>/log.txt, *.jpg and xcodebuild.log.
Exit status: 0 all steps done, 1 stopped or failed, 2 bad arguments.

Needs Xcode 16 or later and xcodegen.
"""
from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FORBIDDEN = ["call", "pay", "payment", "purchase", "buy", "delete", "block", "report", "log out", "sign out"]
DEVICE_UDID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40}"
SIMULATOR_UDID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
MINE = "uidriver-run.xctestrun"


def forbidden_pattern(words: list[str]) -> str:
    """One regular expression for the forbidden labels: whole words, an optional plural s. '' when there are none."""
    if not words:
        return ""
    return r"\b(?:" + "|".join(re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", w.strip()) for w in words) + r")s?\b"


class Config:
    def __init__(self, path: Path):
        try:
            cfg = json.loads(path.read_text())
        except FileNotFoundError:
            sys.exit(f"no config at {path}: copy config.example.json to config.json and fill it in")
        except ValueError as e:
            sys.exit(f"{path} is not valid JSON: {e}")
        if not isinstance(cfg, dict):
            sys.exit(f"{path} must hold a JSON object")
        self.udid = cfg.get("udid")
        if isinstance(self.udid, str) and re.fullmatch(DEVICE_UDID, self.udid):
            self.kind = "device"
        elif isinstance(self.udid, str) and re.fullmatch(SIMULATOR_UDID, self.udid):
            self.kind = "simulator"
        else:
            sys.exit("config: \"udid\" must be the UDID of the device or simulator; the driver does not pick one itself")
        self.destination = ("platform=iOS," if self.kind == "device" else "platform=iOS Simulator,") + f"id={self.udid}"
        self.allowed = cfg.get("allowed_bundles")
        if not (isinstance(self.allowed, list) and self.allowed
                and all(isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", b) for b in self.allowed)):
            sys.exit("config: \"allowed_bundles\" must be a non-empty list of bundle ids")
        words = cfg.get("forbidden_labels", DEFAULT_FORBIDDEN)
        if not (isinstance(words, list) and all(isinstance(w, str) and w.strip() for w in words)):
            sys.exit("config: \"forbidden_labels\" must be a list of words or phrases (an empty list switches the check off)")
        self.forbidden = forbidden_pattern(words)
        self.team = cfg.get("development_team", "")
        self.prefix = cfg.get("bundle_prefix", "")
        if not isinstance(self.team, str) or not re.fullmatch(r"[A-Za-z0-9]*", self.team):
            sys.exit("config: \"development_team\" must be an Apple developer team id (or empty for a simulator)")
        if not isinstance(self.prefix, str) or not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", self.prefix):
            sys.exit("config: \"bundle_prefix\" must be a reverse-domain prefix for the driver's own bundle ids, such as com.example")
        self.out = Path(cfg.get("output_dir", "~/phone-ui-driver-runs/ios")).expanduser()
        self.products = HERE / "build" / self.kind / "Build" / "Products"


def build(cfg: Config) -> int:
    if cfg.kind == "device" and not cfg.team:
        sys.exit("config: a build for a device needs \"development_team\"")
    if not shutil.which("xcodegen"):
        sys.exit("xcodegen not found (brew install xcodegen)")
    env = dict(os.environ, UIDRIVER_TEAM=cfg.team, UIDRIVER_BUNDLE_PREFIX=cfg.prefix)
    r = subprocess.run(["xcodegen", "generate"], cwd=HERE, env=env)
    if r.returncode != 0:
        return 1
    derived = HERE / "build" / cfg.kind
    derived.mkdir(parents=True, exist_ok=True)
    cmd = ["xcodebuild", "build-for-testing", "-project", "UIDriver.xcodeproj", "-scheme", "UIDriver",
           "-destination", cfg.destination, "-derivedDataPath", str(derived)]
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    log = HERE / "build" / f"{cfg.kind}.log"
    log.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    if p.returncode != 0:
        tail = [l for l in (p.stdout + p.stderr).splitlines() if "error" in l.lower()][-15:]
        print(f"build failed (xcodebuild exit {p.returncode}); full log in {log}")
        print("\n".join(l[:220] for l in tail))
        return 1
    print(f"built for the {cfg.kind}; products in {cfg.products}")
    return 0


def read_steps(arg: str) -> list:
    steps = json.loads(Path(arg[1:]).read_text() if arg.startswith("@") else arg)
    if not isinstance(steps, list) or not all(isinstance(x, dict) for x in steps):
        raise ValueError("steps must be a JSON list of objects")
    return steps


def run(cfg: Config, name: str, steps: list) -> int:
    runs = [r for r in sorted(cfg.products.glob("*.xctestrun")) if r.name != MINE]
    if len(runs) != 1:
        sys.exit(f"expected one .xctestrun in {cfg.products}, found {len(runs)}; run `run.py build` first")
    plan = plistlib.loads(runs[0].read_bytes())
    env = {"UIDRIVER_STEPS": base64.b64encode(json.dumps(steps).encode()).decode(),
           "UIDRIVER_ALLOWED": ",".join(cfg.allowed), "UIDRIVER_FORBIDDEN": cfg.forbidden}
    n = 0

    def inject(node):
        nonlocal n
        if isinstance(node, dict):
            if "TestBundlePath" in node:
                node.setdefault("EnvironmentVariables", {}).update(env)
                n += 1
            for v in node.values():
                inject(v)
        elif isinstance(node, list):
            for v in node:
                inject(v)

    inject(plan)
    if n != 1:
        sys.exit(f"expected one test target in the test plan, found {n}")
    mine = runs[0].with_name(MINE)
    mine.write_bytes(plistlib.dumps(plan))
    out = cfg.out / name
    try:
        out.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        print(f"a run named {name} exists already; pick a new name", file=sys.stderr)
        return 2
    bundle = out / "result.xcresult"
    cmd = ["xcodebuild", "test-without-building", "-xctestrun", str(mine), "-destination", cfg.destination,
           "-resultBundlePath", str(bundle), "-only-testing:UIDriverTests/Driver/testDrive",
           "-collect-test-diagnostics", "never"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        print("xcodebuild did not finish within 15 minutes; stopped it. No driver log.", file=sys.stderr)
        return 1
    (out / "xcodebuild.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    att = out / "attachments"
    subprocess.run(["xcrun", "xcresulttool", "export", "attachments", "--path", str(bundle), "--output-path", str(att)],
                   capture_output=True, text=True)
    log, shots = None, []
    manifest = att / "manifest.json"
    if manifest.exists():
        for test in json.loads(manifest.read_text()):
            for a in test.get("attachments", []):
                src = att / a["exportedFileName"]
                human = a.get("suggestedHumanReadableName", "")
                if human.startswith("uidriverlog"):
                    log = src.read_text()
                elif src.suffix.lower() in (".png", ".jpg", ".jpeg", ".heic"):
                    stem = re.sub(r"[^A-Za-z0-9._-]", "-", human.split("_")[0]) or "shot"
                    dst = out / f"{stem}.jpg"
                    k = 1
                    while dst.exists():
                        k += 1
                        dst = out / f"{stem}-{k}.jpg"
                    subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1100", str(src), "--out", str(dst)], capture_output=True)
                    shots.append(dst)
    shutil.rmtree(bundle, ignore_errors=True)
    shutil.rmtree(att, ignore_errors=True)
    if log is None:
        tail = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()][-25:]
        print(f"xcodebuild exit {p.returncode}; no driver log. Last lines:")
        print("\n".join(l[:220] for l in tail))
        return 1
    (out / "log.txt").write_text(log + "\n")
    print(log)
    for s in shots:
        print("screenshot:", s)
    return 1 if ("stopped at step" in log or "FAILED before" in log) else 0


def main(argv: list) -> int:
    args = argv[1:]
    config = HERE / "config.json"
    if len(args) >= 2 and args[0] == "--config":
        config, args = Path(args[1]), args[2:]
    if args == ["build"]:
        return build(Config(config))
    if len(args) != 2 or not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", args[0]) or args[0] == "build":
        print(__doc__)
        return 2
    cfg = Config(config)
    try:
        steps = read_steps(args[1])
    except (ValueError, OSError) as e:
        print(f"bad steps: {e}", file=sys.stderr)
        return 2
    return run(cfg, args[0], steps)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
