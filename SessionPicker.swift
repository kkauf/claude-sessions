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

    func open(sid: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Paths.opener)
        p.arguments = ["open", sid]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
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
        // SP_OPAQUE=1: solid background instead of vibrancy — used for demo
        // screenshots so no desktop content can bleed through the blur.
        if ProcessInfo.processInfo.environment["SP_OPAQUE"] != nil {
            effect.state = .inactive
            effect.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
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
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
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
        bridge.open(sid: rows[i].sid)
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = Self.dockIcon()
        registerHotKeyIfConfigured()
        controller.show()
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

    /// Runtime-drawn Dock icon — no asset files to ship.
    static func dockIcon() -> NSImage {
        let size = NSSize(width: 512, height: 512)
        let img = NSImage(size: size)
        img.lockFocus()
        let rect = NSRect(origin: .zero, size: size).insetBy(dx: 40, dy: 40)
        let bg = NSBezierPath(roundedRect: rect, xRadius: 96, yRadius: 96)
        NSColor(calibratedRed: 0.85, green: 0.47, blue: 0.34, alpha: 1.0).setFill()  // Claude terracotta
        bg.fill()
        if let sym = NSImage(systemSymbolName: "bubble.left.and.bubble.right.fill",
                             accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 240, weight: .medium)
                .applying(.init(paletteColors: [.white]))) {
            let s = sym.size
            let scale = min(280 / s.width, 280 / s.height)
            let w = s.width * scale, h = s.height * scale
            sym.draw(in: NSRect(x: (512 - w) / 2, y: (512 - h) / 2, width: w, height: h))
        }
        img.unlockFocus()
        return img
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)  // Dock icon = the default trigger
let delegate = AppDelegate()
app.delegate = delegate
app.run()
