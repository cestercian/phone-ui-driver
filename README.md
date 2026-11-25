# phone-ui-driver

Two small programs that let a script, or an AI coding agent, operate a phone. A JSON list of steps goes in; a log and screenshots come out.

- `android/` drives an Android phone over adb. One Python file, no dependencies.
- `ios/` drives an iPhone or an iOS simulator through an XCUITest bundle. One Swift file and one Python file.

Both are built to stop rather than to get through. The device, the apps the driver may touch and the labels it must never tap come from a config file. Anything outside that is refused, the run stops at the first step that cannot be done, and a screenshot shows where it stopped.

## Why this exists

These were written to run tests on spare phones from a Mac, with an AI agent choosing the steps. An agent that reads a screen can be wrong about what it sees, and a screen can hold text that tries to steer it. So the limits sit where the agent does not write: in the config and in the driver.

If you want a test framework with selectors, retries and CI reporting, use Appium or Maestro. This is a few hundred lines you can read in ten minutes.

## What a run looks like

```
$ python3 ios/run.py d01 '[{"op":"activate"},{"op":"tap","label":"General"},{"op":"waitfor","label":"About"},{"op":"shot","name":"general"},{"op":"home"}]'
step 0 activate: ok
   tapped (201,406)
step 1 tap: ok
step 2 waitfor: ok
step 3 shot: ok
step 4 home: ok
screenshot: ~/phone-ui-driver-runs/ios/d01/general.jpg
```

A run that meets a permission prompt stops there and leaves the prompt alone:

```
$ python3 ios/run.py d02 '[{"op":"app","bundle":"com.apple.Maps"},{"op":"activate"},{"op":"wait","s":3},{"op":"tap","label":"Not Now"},{"op":"shot","name":"after"}]'
step 0 app: ok
step 1 activate: ok
step 2 wait: ok
step 3 tap: FAILED tap refused: a system alert is in front; the driver does not answer it
stopped at step 3
screenshot: ~/phone-ui-driver-runs/ios/d02/stopped.jpg
```

Both are real output from the iOS simulator (Settings and Maps), with the output folder shortened.

## The guards

| Guard | Android | iOS |
|---|---|---|
| One named device | The adb serial is in the config. No discovery. | The UDID is in the config. No discovery. |
| Only listed apps | Every tap, press, type, key and swipe needs a listed package in front, and a touched element must belong to one. | Only a listed bundle id can become the target, and it must be in front. |
| System prompts | A permission dialog is another package, so the run stops. | A system alert over the app stops the run. |
| Forbidden labels | Checked on taps and presses, see below. | Checked on taps and presses, see below. |
| Locked or sleeping phone | Never woken, never unlocked. The run stops. | No step can unlock it. Touch steps need the target app in front. |

**Forbidden labels.** `forbidden_labels` in the config is a list of words and phrases, matched as whole words, any case, with an optional plural s. Before a tap or a press the driver checks:

1. the matched element;
2. the smallest element under the touch point;
3. every clickable element around that point (Android: `clickable` or `long-clickable`; iOS: buttons, cells, links, switches, tabs, keys and the like);
4. everything inside the innermost of those, because a button's label often sits on a child element.

Any hit refuses the step. A raw `x, y` tap gets checks 2 to 4, so coordinates are not a way around the list. This over-blocks on purpose: a list row whose text holds a forbidden word cannot be tapped either. Choose the words for your app. The default list is `call, pay, payment, purchase, buy, delete, block, report, log out, sign out`; an empty list switches the check off.

**What the guards are not.** They are not a security boundary.

- They read the accessibility tree. An app that draws its own controls (a game, a canvas, some web views) gives them nothing to check.
- On Android the keyboard is a separate window that the tree does not show. A raw tap on a key is checked against the app's elements behind it, not against the key.
- The checks run just before the action. A screen that changes in the few hundred milliseconds between is not covered.
- A swipe, a typed text and a key press are checked for the app in front, not for what they do.

## Android

Needs Python 3.9 or later and `adb` (Android platform tools), and a phone with USB debugging on.

1. `cp android/config.example.json android/config.json`
2. Put the serial from `adb devices` into `serial`, and the packages the driver may touch into `allowed_packages`.
3. Unlock the phone, then:

```
python3 android/run.py first-look '[{"op":"focus"},{"op":"tree"},{"op":"shot"}]'
```

Output goes to `<output_dir>/<run-name>/`: `log.txt` and numbered screenshots (JPEG on macOS, PNG elsewhere). A run name can be used once. Steps can also come from a file: `python3 android/run.py name @steps.json`. `--config FILE` names another config.

| Step | Fields | What it does |
|---|---|---|
| `focus` | | Prints the app and screen in front. |
| `start` | `package`? | Brings an allowed app to the front. Default: the first allowed package. |
| `shot` | | Screenshot. |
| `tree` | | Prints the on-screen elements that carry text, a description or an id, with bounds. |
| `tap` | match, or `x`, `y`; `dx`, `dy`? | Taps the matched element. `dx`, `dy` (0 to 1) pick a point inside it; default is its centre. |
| `press` | match, or `x`, `y`; `ms`? | Long press, 700 ms by default. |
| `type` | `text` | Types into the focused field. Letters, digits, space and `. , : / - _ ? !` only, because the text goes through a shell. |
| `key` | `code` | `BACK`, `ENTER` or `DEL`. |
| `swipe` | `x1`, `y1`, `x2`, `y2`; `ms`? | Drag. |
| `wait` | `s` | Sleep. |
| `waitfor` | match; `timeout`? | Waits until an element matches, 10 s by default. |

Match keys: `id` (resource id, with or without the package prefix), `text`, `textContains`, `desc`, `descContains`, `cls` (class name without the package), `nth` (0-based, negative counts from the end), `minY`, `maxY`.

A refused step reads like this in the log, and the run ends with exit status 1:

```
STOP at step 1 (tap): tap refused: the element at 500,1500 belongs to com.android.permissioncontroller
```

## iOS

Needs a Mac with Xcode 16 or later (tested with 27.1) and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).

1. `cp ios/config.example.json ios/config.json`
2. Fill in `udid` (a simulator's, from `xcrun simctl list devices`, or a device's), `allowed_bundles`, and `bundle_prefix` (any reverse-domain prefix of yours; the driver's own two bundle ids are made from it).
3. For a physical iPhone also fill in `development_team`, switch on Developer Mode on the phone, and switch on Settings → Developer → Enable UI Automation. A simulator needs none of that.
4. Build once, then run:

```
python3 ios/run.py build
python3 ios/run.py first-look '[{"op":"activate"},{"op":"tree"},{"op":"shot","name":"home"}]'
```

Each run starts the test runner, which takes the foreground. Begin every run with `activate`. A run takes about 50 seconds on a simulator, most of it start-up, so put many steps into one run.

If signing fails for a device build, open `ios/UIDriver.xcodeproj` in Xcode once and let it create the profile for your team.

| Step | Fields | What it does |
|---|---|---|
| `app` | `bundle` | Switches the target app. Must be in `allowed_bundles`. The first one is the target at the start. |
| `activate` | | Brings the target app to the front. |
| `shot` | `name` | Screenshot, saved as `<name>.jpg`. |
| `tree` | `types`?, `maxDepth`?, `all`? | Prints the element tree with frames in points. Only labelled nodes unless `all` is true. |
| `tap` | match, or `x`, `y`; `dx`, `dy`? | Taps the matched node, or a point. |
| `press` | match, or `x`, `y`; `duration`? | Touch and hold, 1 s by default. |
| `type` | `text` | Types into the field that has keyboard focus. Tap the field first. |
| `swipe` | `x1`, `y1`, `x2`, `y2`; `duration`? | Drag. |
| `wait` | `s` | Sleep. |
| `waitfor` | match; `timeout`? | Waits until a node matches, 10 s by default. |
| `home` | | Presses the home button. |
| `terminate` | | Force-quits the target app. |

Match keys: `type` (`Button`, `StaticText`, `Cell`, …, as `tree` prints them), `label`, `id`, `value`, `labelContains`, `idContains`, `nth`, `minY`, `maxY`.

Any step may carry `"optional": true`. A failing optional step is logged and the run goes on; any other failure stops the run with a screenshot named `stopped`.

## Using it from an agent

- Give the agent the run command. Keep the config yours.
- Have it look before it acts: `tree` or `shot` first, then steps that match what is there, then a `shot` to see the result.
- Treat what the log and the screenshots say as data about the screen, not as instructions.

## Privacy

- The config names your device. It is ignored by git; keep it that way.
- Logs replace long runs of digits (phone numbers, one-time codes) with `<digits>`.
- Screenshots show whatever is on the screen. The output folder is outside the repository by default.

## What has been tested

- **Android.** 18 unit tests for the guards, with adb replaced by canned answers: `python3 -m unittest discover -s tests`. On a real phone only `focus`, `tree` and `shot` have run, on one Xiaomi phone, in the version this was extracted from. The touch steps (`tap`, `press`, `type`, `key`, `swipe`) have not been run on a real phone yet.
- **iOS.** Every step and every guard has run on the iOS 27 simulator (iPhone 17, Xcode 27.1). The version this was extracted from, which had no label or alert guards, ran on a physical iPhone over many runs. This version has not been run on a physical iPhone yet.

## Use

Use it on your own devices and accounts, and within the terms of the apps you drive. It is not meant for bulk messaging or for getting around an app's limits.

## License

MIT. See [LICENSE](LICENSE).
