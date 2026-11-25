import XCTest

/// UI step runner for iOS. Reads a JSON list of steps and runs them against an app on a device or a simulator.
///
/// Environment (set by run.py):
///   UIDRIVER_STEPS      base64 of the JSON list of steps
///   UIDRIVER_ALLOWED    comma-separated bundle ids the driver may target; the first is the target until an `app` step
///   UIDRIVER_FORBIDDEN  regular expression, case-insensitive; an element whose label or identifier matches is never
///                       tapped. Empty switches the check off.
///
/// Steps (each a JSON object with "op"):
///   app       {bundle}                 switch the target app (must be an allowed bundle id)
///   activate                           bring the target app to the front
///   wait      {s}                      sleep
///   shot      {name}                   screenshot, attached to the result bundle
///   tree      {types?, maxDepth?, all?} print the element tree (masked), only labelled nodes unless all=true
///   tap       {match… | x,y}           tap the matched node (dx, dy in 0..1 pick a point inside it), or a point
///   press     {match… | x,y, duration} touch and hold
///   type      {text}                   type into the focused field
///   swipe     {x1,y1,x2,y2,duration?}  drag
///   waitfor   {match…, timeout?}       wait until a node matches
///   home                               press the home button
///   terminate                          force-quit the target app
/// Match keys: type, label, id, value (exact), labelContains, idContains, nth (0-based, negative from the end),
/// minY, maxY.
/// Any step may carry "optional": true; a failing step that is not optional stops the run.
///
/// Guards, all fail closed:
///   - only an allowed bundle id can become the target;
///   - tap, press, type and swipe need the target app in front and no system alert (a permission prompt, say) over
///     it; the driver does not answer system alerts;
///   - a tap or press is checked against the matched node and against what lies under the point (the smallest node
///     there, every button-like node around it, and everything inside the innermost button-like one): none may
///     carry a forbidden label.
final class Driver: XCTestCase {
    var allowed: [String] = []
    var forbidden: NSRegularExpression?
    var app = XCUIApplication()
    lazy var springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    var screen = CGRect(x: 0, y: 0, width: 440, height: 960)
    var out: [String] = []

    struct StepError: Error, CustomStringConvertible { let description: String }

    static let typeNames: [UInt: String] = [
        0: "Any", 1: "Other", 2: "Application", 3: "Group", 4: "Window", 5: "Sheet", 7: "Alert", 8: "Dialog", 9: "Button",
        10: "RadioButton", 12: "CheckBox", 14: "PopUpButton", 16: "MenuButton", 17: "ToolbarButton", 18: "Popover",
        19: "Keyboard", 20: "Key", 21: "NavigationBar", 22: "TabBar", 24: "Toolbar", 25: "StatusBar", 26: "Table",
        32: "CollectionView", 33: "Slider", 34: "PageIndicator", 35: "ProgressIndicator", 36: "ActivityIndicator",
        37: "SegmentedControl", 38: "Picker", 40: "Switch", 41: "Toggle", 42: "Link", 43: "Image", 44: "Icon",
        45: "SearchField", 46: "ScrollView", 48: "StaticText", 49: "TextField", 50: "SecureTextField", 52: "TextView",
        53: "Menu", 54: "MenuItem", 58: "WebView", 75: "Cell", 80: "Tab",
    ]

    /// Node types that act when tapped. A forbidden label on one of these around a tap point stops the tap.
    static let buttonLike: Set<String> = [
        "Button", "RadioButton", "CheckBox", "PopUpButton", "MenuButton", "ToolbarButton", "Switch", "Toggle", "Link",
        "Icon", "MenuItem", "Cell", "Tab", "Key",
    ]

    static let matchKeys = ["type", "label", "id", "value", "labelContains", "idContains"]

    override func setUp() { continueAfterFailure = true }

    /// A failure XCTest raises inside a step (an event it could not deliver, say) fails that step in the driver's
    /// log. It is not passed on: a failed XCTest case would only add minutes of diagnostics collection.
    var issues: [String] = []
    override func record(_ issue: XCTIssue) { issues.append(mask(issue.compactDescription)) }

    func log(_ s: String) { out.append(s) }

    /// Long runs of digits (phone numbers, one-time codes) do not go into the log.
    func mask(_ s: String) -> String {
        var r = s.replacingOccurrences(of: "\n", with: " ")
        r = r.replacingOccurrences(of: "[0-9][0-9 ]{5,}[0-9]", with: "<digits>", options: .regularExpression)
        return r.count > 70 ? String(r.prefix(70)) + "…" : r
    }

    func name(_ t: XCUIElement.ElementType) -> String { Driver.typeNames[t.rawValue] ?? "T\(t.rawValue)" }

    func walk(_ n: XCUIElementSnapshot, _ depth: Int, _ visit: (XCUIElementSnapshot, Int) -> Void) {
        visit(n, depth)
        for c in n.children { walk(c, depth + 1, visit) }
    }

    /// The target app's tree. Its root frame is the screen, in points.
    func snapshot() throws -> XCUIElementSnapshot {
        let root = try app.snapshot()
        if root.frame.width > 0 && root.frame.height > 0 { screen = root.frame }
        return root
    }

    /// Apps keep scrolled-away rows in the tree with frames far off the screen; those cannot be tapped.
    func onScreen(_ n: XCUIElementSnapshot) -> Bool {
        let f = n.frame
        return f.width > 0 && f.height > 0 && screen.contains(CGPoint(x: f.midX, y: f.midY))
    }

    func matches(_ n: XCUIElementSnapshot, _ m: [String: Any]) -> Bool {
        if let t = m["type"] as? String, name(n.elementType) != t { return false }
        if let l = m["label"] as? String, n.label != l { return false }
        if let i = m["id"] as? String, n.identifier != i { return false }
        if let v = m["value"] as? String, (n.value as? String) != v { return false }
        if let l = m["labelContains"] as? String, !n.label.localizedCaseInsensitiveContains(l) { return false }
        if let i = m["idContains"] as? String, !n.identifier.localizedCaseInsensitiveContains(i) { return false }
        if let y = m["minY"] as? Double, Double(n.frame.midY) < y { return false }
        if let y = m["maxY"] as? Double, Double(n.frame.midY) > y { return false }
        return onScreen(n)
    }

    func pick(_ root: XCUIElementSnapshot, _ m: [String: Any]) throws -> XCUIElementSnapshot? {
        if !Driver.matchKeys.contains(where: { m[$0] != nil }) { throw StepError(description: "step has no match key") }
        var hits: [XCUIElementSnapshot] = []
        walk(root, 0) { n, _ in if matches(n, m) { hits.append(n) } }
        let nth = m["nth"] as? Int ?? 0
        if nth < 0 { return hits.count + nth >= 0 ? hits[hits.count + nth] : nil }
        return nth < hits.count ? hits[nth] : nil
    }

    // MARK: guards

    func namesForbidden(_ n: XCUIElementSnapshot) -> Bool {
        guard let re = forbidden else { return false }
        let id = n.identifier.replacingOccurrences(of: "[_.\\-]", with: " ", options: .regularExpression)
        let s = n.label + " " + id
        return re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    func needFront(_ what: String) throws {
        if app.state != .runningForeground {
            throw StepError(description: "\(what) refused: the target app is not in front")
        }
        if springboard.alerts.firstMatch.exists {
            throw StepError(description: "\(what) refused: a system alert is in front; the driver does not answer it")
        }
    }

    func checkPoint(_ p: CGPoint, _ root: XCUIElementSnapshot, _ what: String) throws {
        var under: [XCUIElementSnapshot] = []
        walk(root, 0) { n, _ in if n.frame.width > 0 && n.frame.height > 0 && n.frame.contains(p) { under.append(n) } }
        let at = "(\(Int(p.x)),\(Int(p.y)))"
        guard let smallest = under.min(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else {
            throw StepError(description: "\(what) refused: no element at \(at)")
        }
        let buttons = under.filter { Driver.buttonLike.contains(name($0.elementType)) }
        for n in [smallest] + buttons where namesForbidden(n) {
            throw StepError(description: "\(what) refused: the element at \(at) names a forbidden action")
        }
        // The innermost button-like node is the control that takes the touch: the labels inside it count as well.
        if let inner = buttons.min(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) {
            var hit = false
            walk(inner, 0) { n, _ in if namesForbidden(n) { hit = true } }
            if hit { throw StepError(description: "\(what) refused: the control at \(at) holds a forbidden label") }
        }
    }

    func point(_ s: [String: Any], _ what: String) throws -> CGPoint {
        let root = try snapshot()
        let p: CGPoint
        if let x = s["x"] as? Double, let y = s["y"] as? Double {
            p = CGPoint(x: x, y: y)
        } else {
            guard let n = try pick(root, s) else { throw StepError(description: "no node matches") }
            if namesForbidden(n) { throw StepError(description: "\(what) refused: the matched element names a forbidden action") }
            let dx = min(max(s["dx"] as? Double ?? 0.5, 0), 0.999), dy = min(max(s["dy"] as? Double ?? 0.5, 0), 0.999)
            p = CGPoint(x: Double(n.frame.minX) + Double(n.frame.width) * dx, y: Double(n.frame.minY) + Double(n.frame.height) * dy)
        }
        try checkPoint(p, root, what)
        return p
    }

    func coord(_ p: CGPoint) -> XCUICoordinate {
        app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: p.x, dy: p.y))
    }

    // MARK: steps

    func run(_ op: String, _ s: [String: Any]) throws {
        switch op {
        case "app":
            guard let b = s["bundle"] as? String, allowed.contains(b) else {
                throw StepError(description: "app refused: \(s["bundle"] as? String ?? "(no bundle)") is not an allowed bundle id")
            }
            app = XCUIApplication(bundleIdentifier: b)
        case "activate":
            app.activate()
            if !app.wait(for: .runningForeground, timeout: 15) { throw StepError(description: "the target app did not come to the front") }
        case "wait":
            Thread.sleep(forTimeInterval: s["s"] as? Double ?? 1)
        case "shot":
            let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot(), quality: .medium)
            a.name = (s["name"] as? String ?? "shot")
            a.lifetime = .keepAlways
            add(a)
        case "tree":
            let root = try snapshot()
            let types = s["types"] as? [String]
            let maxDepth = s["maxDepth"] as? Int ?? 99
            let all = s["all"] as? Bool ?? false
            var lines = 0
            walk(root, 0) { n, d in
                if d > maxDepth || lines > 400 { return }
                let t = name(n.elementType)
                if let types = types, !types.contains(t) { return }
                let v = (n.value as? String) ?? ""
                if !all && n.label.isEmpty && n.identifier.isEmpty && v.isEmpty { return }
                if !onScreen(n) { return }
                let f = n.frame
                var line = String(repeating: " ", count: min(d, 12)) + t
                if !n.label.isEmpty { line += " \"\(mask(n.label))\"" }
                if !n.identifier.isEmpty { line += " id=\(mask(n.identifier))" }
                if !v.isEmpty { line += " val=\(mask(v))" }
                if !n.isEnabled { line += " (disabled)" }
                line += " [\(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height))]"
                log(line)
                lines += 1
            }
        case "tap":
            try needFront(op)
            let p = try point(s, op)
            coord(p).tap()
            log("   tapped (\(Int(p.x)),\(Int(p.y)))")
        case "press":
            try needFront(op)
            let p = try point(s, op)
            coord(p).press(forDuration: s["duration"] as? Double ?? 1.0)
            log("   pressed (\(Int(p.x)),\(Int(p.y)))")
        case "type":
            try needFront(op)
            if app.descendants(matching: .any).matching(NSPredicate(format: "hasKeyboardFocus == true")).count == 0 {
                throw StepError(description: "type: no field has keyboard focus; tap one first")
            }
            app.typeText(s["text"] as? String ?? "")
        case "swipe":
            try needFront(op)
            guard let x1 = s["x1"] as? Double, let y1 = s["y1"] as? Double, let x2 = s["x2"] as? Double, let y2 = s["y2"] as? Double else {
                throw StepError(description: "swipe needs x1,y1,x2,y2")
            }
            coord(CGPoint(x: x1, y: y1)).press(forDuration: s["duration"] as? Double ?? 0.05, thenDragTo: coord(CGPoint(x: x2, y: y2)))
        case "waitfor":
            let deadline = Date().addingTimeInterval(s["timeout"] as? Double ?? 10)
            while true {
                if try pick(try snapshot(), s) != nil { return }
                if Date() > deadline { throw StepError(description: "timed out waiting for a match") }
                Thread.sleep(forTimeInterval: 0.5)
            }
        case "home":
            XCUIDevice.shared.press(.home)
        case "terminate":
            // Force-quit the target app, as swiping it away in the app switcher does.
            app.terminate()
            _ = app.wait(for: .notRunning, timeout: 10)
            log("   app state after terminate: \(app.state.rawValue) (1 = not running)")
        default:
            throw StepError(description: "unknown op \(op)")
        }
    }

    func configure() throws -> [[String: Any]] {
        let env = ProcessInfo.processInfo.environment
        allowed = (env["UIDRIVER_ALLOWED"] ?? "").split(separator: ",").map(String.init)
        guard let first = allowed.first else { throw StepError(description: "UIDRIVER_ALLOWED is empty: no app may be driven") }
        if let f = env["UIDRIVER_FORBIDDEN"], !f.isEmpty {
            guard let re = try? NSRegularExpression(pattern: f, options: [.caseInsensitive]) else {
                throw StepError(description: "UIDRIVER_FORBIDDEN is not a valid regular expression")
            }
            forbidden = re
        }
        guard let b64 = env["UIDRIVER_STEPS"], let data = Data(base64Encoded: b64),
              let steps = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            throw StepError(description: "UIDRIVER_STEPS missing or not a JSON list of objects")
        }
        app = XCUIApplication(bundleIdentifier: first)
        return steps
    }

    /// Not named testRun: XCTest has a property of that name, and a method called that is never run.
    func testDrive() {
        do {
            let steps = try configure()
            for (i, s) in steps.enumerated() {
                let op = s["op"] as? String ?? ""
                do {
                    issues.removeAll()
                    try run(op, s)
                    if !issues.isEmpty { throw StepError(description: issues.joined(separator: "; ")) }
                    log("step \(i) \(op): ok")
                } catch {
                    log("step \(i) \(op): FAILED \(error)")
                    if (s["optional"] as? Bool) != true {
                        log("stopped at step \(i)")
                        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot(), quality: .medium)
                        a.name = "stopped"
                        a.lifetime = .keepAlways
                        add(a)
                        break
                    }
                }
            }
        } catch {
            log("FAILED before the first step: \(error)")
        }
        let a = XCTAttachment(string: out.joined(separator: "\n"))
        a.name = "uidriverlog"
        a.lifetime = .keepAlways
        add(a)
    }
}
