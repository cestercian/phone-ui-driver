import SwiftUI

// Stub host for the UI driver. It does nothing; the UI test bundle drives other apps.
@main
struct UIDriverHostApp: App {
    var body: some Scene {
        WindowGroup { Text("UI driver host").padding() }
    }
}
