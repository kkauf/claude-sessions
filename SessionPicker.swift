// SessionPicker — a super-lightweight native macOS front-end for claude-sessions.
//
// Click the Dock icon (or `open -a SessionPicker`) → floating Spotlight-style
// panel → type to search the FTS5 index → Enter opens iTerm at the session's
// directory and resumes it. A preview pane shows the conversation, query-
// matched while searching. All search/ranking/lineage logic lives in
// session_indexer.py; resume-dir resolution and preview rendering live in the
// claude-sessions bash scripts. This file is only a window.
//
// Build: ./build-app.sh   (single swiftc invocation, no Xcode project)
//
// Config (all optional), via: defaults write earth.kaufmann.SessionPicker <key> <value>
//   HotKeyCode   Carbon key code — set to ENABLE the global hotkey (49 = Space).
//                No default: without it, no hotkey is registered at all.
//   HotKeyMods   Carbon modifier mask (default when HotKeyCode set: 2048 = Option)
//   IndexerPath  path to session_indexer.py   (default: ~/github/claude-sessions/session_indexer.py)
//   OpenerPath   path to claude-sessions      (default: first of /opt/homebrew/bin, /usr/local/bin, repo)
//   PreviewPath  path to session-preview.sh   (default: next to IndexerPath)
//   PythonPath   path to python3              (default: /usr/bin/python3)

import AppKit
import Carbon.HIToolbox

// MARK: - Row model (matches session_indexer.py --json)

struct SessionRow: Decodable {
    let sid: String
    let title: String
    let preview: String
    let project: String
    let reldate: String
    let size: String
    let is_fork: Int
    let active: Int
}

// MARK: - Paths

enum Paths {
    static func expand(_ p: String) -> String { (p as NSString).expandingTildeInPath }

    /// The clone this app was built from — baked into Info.plist by build-app.sh.
    static var repo: String {
        (Bundle.main.object(forInfoDictionaryKey: "RepoPath") as? String)
            ?? expand("~/github/claude-sessions")
    }
    static var indexer: String {
        expand(UserDefaults.standard.string(forKey: "IndexerPath")
               ?? repo + "/session_indexer.py")
    }
    static var previewScript: String {
        if let p = UserDefaults.standard.string(forKey: "PreviewPath") { return expand(p) }
        return (indexer as NSString).deletingLastPathComponent + "/session-preview.sh"
    }
    static var python: String {
        expand(UserDefaults.standard.string(forKey: "PythonPath") ?? "/usr/bin/python3")
    }
    static var opener: String {
        if let p = UserDefaults.standard.string(forKey: "OpenerPath") { return expand(p) }
        for c in [repo + "/claude-sessions",
                  "/opt/homebrew/bin/claude-sessions", "/usr/local/bin/claude-sessions"] {
            if FileManager.default.isExecutableFile(atPath: c) { return c }
        }
        return "claude-sessions"
    }
    /// Home dir encoded the way the preview script expects ("Users-kkaufmann").
    static var homeKey: String {
        String(NSHomeDirectory().replacingOccurrences(of: "/", with: "-").dropFirst())
    }
}

// MARK: - Process bridge (indexer, opener, preview)

final class Bridge {
    private var queryProcess: Process?
    private var previewProcess: Process?

    private func run(_ exe: String, _ args: [String], track: ReferenceWritableKeyPath<Bridge, Process?>,
                     done: @escaping (Data) -> Void) {
        self[keyPath: track]?.terminate()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { proc in
            guard proc.terminationStatus == 0 || proc.terminationReason == .exit else { return }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            DispatchQueue.main.async { done(data) }
        }
        do { try p.run() } catch { done(Data()) }
        self[keyPath: track] = p
    }

    func cancel() {
        queryProcess?.terminate(); queryProcess = nil
        previewProcess?.terminate(); previewProcess = nil
    }

    func query(_ q: String, done: @escaping ([SessionRow]) -> Void) {
        var args = [Paths.indexer, "--json"]
        if !q.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--search", q] }
        run(Paths.python, args, track: \.queryProcess) { data in
            done((try? JSONDecoder().decode([SessionRow].self, from: data)) ?? [])
        }
    }

    /// Conversation preview — same script the fzf picker uses, ANSI stripped.
    func preview(sid: String, query: String, done: @escaping (String) -> Void) {
        run("/bin/bash", [Paths.previewScript, sid, Paths.homeKey, query],
            track: \.previewProcess) { data in
            var text = String(data: data, encoding: .utf8) ?? ""
            text = text.replacingOccurrences(of: "\u{1B}\\[[0-9;]*m", with: "",
                                             options: .regularExpression)
            done(text)
        }
    }

    /// Incremental index sync (new/changed sessions), then refresh.
    func sync(done: @escaping () -> Void) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Paths.python)
        p.arguments = [Paths.indexer]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in DispatchQueue.main.async { done() } }
        do { try p.run() } catch { done() }
    }

    /// Resume via `claude-sessions open <sid> --gui` (.command + LaunchServices —
    /// no AppleEvents, so no Automation permission can silently break it).
    /// Reports failure so the UI can show it instead of doing nothing.
    func open(sid: String, done: @escaping (Int32, String) -> Void) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Paths.opener)
        p.arguments = ["open", sid, "--gui"]
        let err = Pipe()
        p.standardOutput = FileHandle.nullDevice
        p.standardError = err
        p.terminationHandler = { proc in
            let msg = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async { done(proc.terminationStatus, msg) }
        }
        do { try p.run() } catch {
            done(-1, "could not launch \(Paths.opener): \(error.localizedDescription)")
        }
    }

    /// Synchronous helper for the self-test: run the opener and capture stdout.
    func openPrint(sid: String) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Paths.opener)
        p.arguments = ["open", sid, "--print"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, "") }
        p.waitUntilExit()
        let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return (p.terminationStatus, s)
    }
}

// MARK: - Panel

final class PickerPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class PickerController: NSObject, NSTextFieldDelegate, NSTableViewDataSource, NSTableViewDelegate {
    let panel: PickerPanel
    let field = NSTextField()
    let table = NSTableView()
    let previewView = NSTextView()
    let countLabel = NSTextField(labelWithString: "")
    let bridge = Bridge()
    var rows: [SessionRow] = []
    var debounce: DispatchWorkItem?
    var previewDebounce: DispatchWorkItem?

    override init() {
        let width: CGFloat = 1020, height: CGFloat = 500
        let listWidth: CGFloat = 640
        panel = PickerPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered, defer: false)
        super.init()

        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = true
        panel.isMovableByWindowBackground = true

        let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        effect.material = .popover
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 12
        // SP_OPAQUE=1: screenshot mode — solid background instead of vibrancy
        // (nothing bleeds through the blur) and the panel stays visible even
        // if the app loses focus mid-capture.
        if ProcessInfo.processInfo.environment["SP_OPAQUE"] != nil {
            effect.state = .inactive
            effect.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            panel.hidesOnDeactivate = false
        }
        panel.contentView = effect

        field.frame = NSRect(x: 16, y: height - 46, width: width - 120, height: 30)
        field.font = .systemFont(ofSize: 18)
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.placeholderString = "Search Claude sessions…"
        field.delegate = self
        effect.addSubview(field)

        countLabel.frame = NSRect(x: width - 96, y: height - 42, width: 80, height: 20)
        countLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        countLabel.textColor = .tertiaryLabelColor
        countLabel.alignment = .right
        effect.addSubview(countLabel)

        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("c"))
        col.width = listWidth - 24
        table.addTableColumn(col)
        table.headerView = nil
        table.rowHeight = 26
        table.style = .inset
        table.backgroundColor = .clear
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(openSelected)

        let scroll = NSScrollView(frame: NSRect(x: 8, y: 10, width: listWidth - 16, height: height - 64))
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        effect.addSubview(scroll)

        // Preview pane (right) — the conversation, query-matched when searching.
        previewView.isEditable = false
        previewView.drawsBackground = false
        previewView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        previewView.textContainerInset = NSSize(width: 8, height: 8)
        let pScroll = NSScrollView(frame: NSRect(x: listWidth, y: 10,
                                                 width: width - listWidth - 12, height: height - 64))
        pScroll.documentView = previewView
        pScroll.hasVerticalScroller = true
        pScroll.drawsBackground = false
        previewView.frame = NSRect(origin: .zero, size: pScroll.contentSize)
        previewView.autoresizingMask = [.width]
        previewView.textContainer?.widthTracksTextView = true
        effect.addSubview(pScroll)

        let divider = NSBox(frame: NSRect(x: listWidth - 4, y: 12, width: 1, height: height - 68))
        divider.boxType = .separator
        effect.addSubview(divider)
    }

    func toggle() {
        if panel.isVisible { hide() } else { show() }
    }

    func show() {
        // Center horizontally, upper third of the screen with the mouse.
        let screen = NSScreen.screens.first {
            NSMouseInRect(NSEvent.mouseLocation, $0.frame, false)
        } ?? NSScreen.main
        if let s = screen {
            let f = panel.frame
            panel.setFrameOrigin(NSPoint(
                x: s.visibleFrame.midX - f.width / 2,
                y: s.visibleFrame.minY + s.visibleFrame.height * 0.58))
        }
        field.stringValue = ""
        // Activate BEFORE ordering front: showing while inactive lets
        // hidesOnDeactivate dismiss the panel instantly (launch "flash").
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(field)
        refresh()
        // Pick up new/changed sessions in the background, then refresh once.
        bridge.sync { [weak self] in self?.refresh() }
    }

    func hide() {
        bridge.cancel()
        panel.orderOut(nil)
    }

    func refresh() {
        bridge.query(field.stringValue) { [weak self] rows in
            guard let self else { return }
            self.rows = rows
            self.countLabel.stringValue = "\(rows.count)"
            self.table.reloadData()
            if !rows.isEmpty {
                self.table.selectRowIndexes([0], byExtendingSelection: false)
                self.table.scrollRowToVisible(0)
            } else {
                self.previewView.string = ""
            }
        }
    }

    func refreshPreview() {
        previewDebounce?.cancel()
        let i = table.selectedRow
        guard i >= 0, i < rows.count else { previewView.string = ""; return }
        let sid = rows[i].sid
        let q = field.stringValue
        let work = DispatchWorkItem { [weak self] in
            self?.bridge.preview(sid: sid, query: q) { [weak self] text in
                guard let self, self.table.selectedRow < self.rows.count,
                      self.table.selectedRow >= 0,
                      self.rows[self.table.selectedRow].sid == sid else { return }
                self.previewView.string = text
                self.previewView.scroll(.zero)
            }
        }
        previewDebounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }

    // MARK: search field events

    func controlTextDidChange(_ obj: Notification) {
        debounce?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.refresh() }
        debounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: work)
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.moveDown(_:)):
            moveSelection(1); return true
        case #selector(NSResponder.moveUp(_:)):
            moveSelection(-1); return true
        case #selector(NSResponder.insertNewline(_:)):
            openSelected(); return true
        case #selector(NSResponder.cancelOperation(_:)):
            hide(); return true
        default:
            return false
        }
    }

    func moveSelection(_ delta: Int) {
        guard !rows.isEmpty else { return }
        let next = min(max(table.selectedRow + delta, 0), rows.count - 1)
        table.selectRowIndexes([next], byExtendingSelection: false)
        table.scrollRowToVisible(next)
    }

    @objc func openSelected() {
        let i = table.selectedRow
        guard i >= 0, i < rows.count else { return }
        let sid = rows[i].sid
        bridge.open(sid: sid) { status, err in
            guard status != 0 else { return }
            // Never fail silently — that reads as "the app is broken".
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Could not resume session"
            alert.informativeText = err.isEmpty
                ? "claude-sessions open exited with status \(status)."
                : err.trimmingCharacters(in: .whitespacesAndNewlines)
            alert.runModal()
        }
        hide()
    }

    // MARK: table

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableViewSelectionDidChange(_ notification: Notification) {
        refreshPreview()
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let id = NSUserInterfaceItemIdentifier("cell")
        let cell = (tableView.makeView(withIdentifier: id, owner: nil) as? NSTextField) ?? {
            let f = NSTextField(labelWithString: "")
            f.identifier = id
            f.lineBreakMode = .byTruncatingTail
            return f
        }()
        cell.attributedStringValue = attributed(rows[row])
        return cell
    }

    private func attributed(_ r: SessionRow) -> NSAttributedString {
        let dim: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.secondaryLabelColor,
            .font: NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)]
        let s = NSMutableAttributedString()
        s.append(NSAttributedString(string: pad(r.reldate, 6) + pad(r.size, 5) + " ", attributes: dim))
        if r.active == 1 {
            s.append(NSAttributedString(string: "● ", attributes: [.foregroundColor: NSColor.systemRed]))
        }
        if r.is_fork == 1 {
            s.append(NSAttributedString(string: "↪ ", attributes: [.foregroundColor: NSColor.systemGreen]))
        }
        let name = r.title.isEmpty ? r.preview : r.title
        s.append(NSAttributedString(string: name, attributes: [
            .foregroundColor: NSColor.labelColor,
            .font: r.title.isEmpty ? NSFont.systemFont(ofSize: 13)
                                   : NSFont.systemFont(ofSize: 13, weight: .semibold)]))
        s.append(NSAttributedString(string: "  · \(r.project)", attributes: [
            .foregroundColor: NSColor.tertiaryLabelColor,
            .font: NSFont.systemFont(ofSize: 12)]))
        return s
    }

    private func pad(_ s: String, _ n: Int) -> String {
        s.count >= n ? s + " " : s + String(repeating: " ", count: n - s.count)
    }
}

// MARK: - App / triggers

let controller = PickerController()

final class AppDelegate: NSObject, NSApplicationDelegate {
    var hotKeyRef: EventHotKeyRef?
    var hasShownOnce = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = Self.dockIcon()
        registerHotKeyIfConfigured()
        if ProcessInfo.processInfo.environment["SP_SELFTEST"] != nil {
            hasShownOnce = true
            SelfTest.run()
        }
        // No show() here: at launch the app may not be active yet, and a
        // panel shown while inactive is dismissed by hidesOnDeactivate the
        // moment anything else has focus — the "flashes then nothing" bug.
        // The first show happens in applicationDidBecomeActive.
    }

    /// First activation (Dock/Finder launch, open -a) shows the panel.
    /// Launching as a Login Item does NOT activate — so no panel pops at
    /// login; the first Dock click brings it up.
    func applicationDidBecomeActive(_ notification: Notification) {
        if !hasShownOnce {
            hasShownOnce = true
            if !controller.panel.isVisible { controller.show() }
        }
    }

    /// Clicking the Dock icon (or `open -a SessionPicker`) toggles the panel.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        controller.toggle()
        return false
    }

    /// Hotkey is strictly opt-in:
    ///   defaults write earth.kaufmann.SessionPicker HotKeyCode -int 49   # Space
    ///   defaults write earth.kaufmann.SessionPicker HotKeyMods -int 2048 # Option
    private func registerHotKeyIfConfigured() {
        guard let code = UserDefaults.standard.object(forKey: "HotKeyCode") as? Int else {
            NSLog("SessionPicker: no hotkey configured (Dock icon toggles the panel; set HotKeyCode to enable one)")
            return
        }
        let mods = UserDefaults.standard.object(forKey: "HotKeyMods") as? Int ?? optionKey

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
            DispatchQueue.main.async { controller.toggle() }
            return noErr
        }, 1, &eventType, nil, nil)

        let hotKeyID = EventHotKeyID(signature: OSType(0x53_50_4B_52) /* SPKR */, id: 1)
        let status = RegisterEventHotKey(UInt32(code), UInt32(mods), hotKeyID,
                                         GetApplicationEventTarget(), 0, &hotKeyRef)
        if status != noErr {
            NSLog("SessionPicker: hotkey registration failed (status %d) — combo taken by another app?", status)
        } else {
            NSLog("SessionPicker: hotkey registered (keyCode %d, mods %d)", code, mods)
        }
    }

    /// Icon artwork — single source of truth for the runtime Dock icon AND the
    /// bundle's AppIcon.icns (via --dump-iconset in build-app.sh). No asset
    /// files to ship; the .icns exists so a pinned Dock tile still shows the
    /// logo when the app isn't running.
    static func drawIcon(in canvas: NSRect) {
        let k = canvas.width / 512
        let rect = canvas.insetBy(dx: 40 * k, dy: 40 * k)
        let bg = NSBezierPath(roundedRect: rect, xRadius: 96 * k, yRadius: 96 * k)
        NSColor(calibratedRed: 0.85, green: 0.47, blue: 0.34, alpha: 1.0).setFill()  // Claude terracotta
        bg.fill()
        if let sym = NSImage(systemSymbolName: "bubble.left.and.bubble.right.fill",
                             accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 240 * k, weight: .medium)
                .applying(.init(paletteColors: [.white]))) {
            let s = sym.size
            let scale = min(280 * k / s.width, 280 * k / s.height)
            let w = s.width * scale, h = s.height * scale
            sym.draw(in: NSRect(x: canvas.minX + (canvas.width - w) / 2,
                                y: canvas.minY + (canvas.height - h) / 2, width: w, height: h))
        }
    }

    static func dockIcon() -> NSImage {
        let img = NSImage(size: NSSize(width: 512, height: 512))
        img.lockFocus()
        drawIcon(in: NSRect(x: 0, y: 0, width: 512, height: 512))
        img.unlockFocus()
        return img
    }

    /// Render the icon at exact pixel sizes into an .iconset directory.
    static func writeIconset(to dir: String) throws {
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let sizes: [(Int, String)] = [(128, "icon_128x128"), (256, "icon_256x256"),
                                      (512, "icon_512x512"), (1024, "icon_512x512@2x")]
        for (px, name) in sizes {
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
                                             bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                             isPlanar: false, colorSpaceName: .calibratedRGB,
                                             bytesPerRow: 0, bitsPerPixel: 0),
                  let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
                throw NSError(domain: "SessionPicker", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "could not create bitmap for \(name)"])
            }
            rep.size = NSSize(width: px, height: px)
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = ctx
            drawIcon(in: NSRect(x: 0, y: 0, width: px, height: px))
            NSGraphicsContext.restoreGraphicsState()
            guard let png = rep.representation(using: .png, properties: [:]) else {
                throw NSError(domain: "SessionPicker", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "could not encode \(name)"])
            }
            try png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }
}

// MARK: - Self-test (SP_SELFTEST=1, driven by test-app.sh)
//
// End-to-end smoke test through the real UI objects: panel shows, the indexer
// returns rows, the preview renders for the selection, and the opener resolves
// a resume command. Exits 0 on PASS, 1 on FAIL, 2 on watchdog timeout.

enum SelfTest {
    static func run() {
        controller.show()
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) {
            print("SELFTEST FAIL: watchdog timeout")
            exit(2)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { verify() }
    }

    static func verify() {
        guard !controller.rows.isEmpty else {
            print("SELFTEST FAIL: no rows loaded (indexer bridge broken?)")
            exit(1)
        }
        guard controller.panel.isVisible else {
            print("SELFTEST FAIL: panel not visible after show()")
            exit(1)
        }
        let preview = controller.previewView.string
        guard !preview.isEmpty else {
            print("SELFTEST FAIL: empty preview for selected row")
            exit(1)
        }
        let sid = controller.rows[0].sid
        DispatchQueue.global().async {
            let (status, out) = controller.bridge.openPrint(sid: sid)
            DispatchQueue.main.async {
                guard status == 0, out.contains("--resume") else {
                    print("SELFTEST FAIL: open --print status=\(status) output=\(out)")
                    exit(1)
                }
                print("SELFTEST PASS: \(controller.rows.count) rows, preview \(preview.count) chars, resume: \(out.trimmingCharacters(in: .whitespacesAndNewlines))")
                exit(0)
            }
        }
    }
}

let app = NSApplication.shared

// Build-time hook: render the icon artwork into an .iconset directory
// (build-app.sh turns it into AppIcon.icns). Not a user-facing flag.
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--dump-iconset" {
    do {
        try AppDelegate.writeIconset(to: CommandLine.arguments[2])
        exit(0)
    } catch {
        FileHandle.standardError.write("iconset dump failed: \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
}

app.setActivationPolicy(.regular)  // Dock icon = the default trigger
let delegate = AppDelegate()
app.delegate = delegate
app.run()
