"""Build dist/Sharp Check.app: a native Mac window around the dashboard that stops it on Quit.

    python scripts/make_app.py      then drag "dist/Sharp Check.app" to the Dock

A small Swift program (WKWebView, the engine Safari uses) compiled here with swiftc from the Xcode
Command Line Tools; no Python GUI dependency. It runs the dashboard from this repo's .venv on its own
port, so a dev server on Dash's default 8050 (or Finance.app on 8765) is never adopted or killed.
Closing the window or Quit stops the dashboard. Mirrors personal-finance-dash/scripts/make_app.py.
"""
import json
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PORT = 8766
NAME = "Sharp Check"


def launch_command(repo: Path, python: Path) -> str:
    q = lambda p: shlex.quote(str(p))  # noqa: E731
    return (
        # `cd` runs first so `&` backgrounds only the server, with every stdio stream redirected,
        # so the shell returns at once instead of waiting on the server's output.
        # Dash's app.run() reads HOST and PORT from the environment.
        f"cd {q(repo)} || exit 1; mkdir -p data; HOST=127.0.0.1 PORT={PORT} "
        f"nohup {q(python)} -m sharp_check.app < /dev/null >> {q(repo / 'data' / 'app.log')} 2>&1 &"
    )


# ponytail: kills the port listener only; a Sync mid-run finishes on its own (the archive is append-only)
KILL = f"lsof -ti tcp:{PORT} -sTCP:LISTEN | xargs kill 2>/dev/null || true"


def _swift(s: str) -> str:
    """A Swift string literal. JSON escaping is valid Swift for these strings (no control characters)."""
    return json.dumps(s, ensure_ascii=False)


def swift_source(repo: Path, python: Path) -> str:
    return f'''import Cocoa
import WebKit

let launch = {_swift(launch_command(repo, python))}
let kill = {_swift(KILL)}
let home = URL(string: "http://127.0.0.1:{PORT}/")!
let logPath = {_swift(str(repo / "data" / "app.log"))}

func sh(_ cmd: String) {{
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/sh")
    p.arguments = ["-c", cmd]
    try? p.run()
    p.waitUntilExit()
}}

func healthy() -> Bool {{
    var req = URLRequest(url: home)
    req.timeoutInterval = 2
    let done = DispatchSemaphore(value: 0)
    var ok = false
    URLSession.shared.dataTask(with: req) {{ _, resp, _ in
        ok = (resp as? HTTPURLResponse)?.statusCode == 200
        done.signal()
    }}.resume()
    done.wait()
    return ok
}}

func page(_ text: String) -> String {{
    "<html><body style='font:15px -apple-system;color:#77736b;background:#fbfaf8;display:grid;" +
    "place-items:center;height:90vh;margin:0'>" + text + "</body></html>"
}}

final class App: NSObject, NSApplicationDelegate {{
    var window: NSWindow!
    let web = WKWebView()
    var loadedAt = Date()

    func applicationDidFinishLaunching(_ n: Notification) {{
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = {_swift(NAME)}
        window.contentView = web
        window.center()
        window.setFrameAutosaveName({_swift(NAME)})
        window.makeKeyAndOrderFront(nil)
        web.loadHTMLString(page("Starting the dashboard…"), baseURL: nil)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.global().async {{
            if !healthy() {{  // reuse a server that is already up (e.g. left behind by a force quit)
                sh(launch)
                for _ in 0..<60 {{
                    if healthy() {{ break }}
                    Thread.sleep(forTimeInterval: 0.5)
                }}
            }}
            let up = healthy()
            DispatchQueue.main.async {{
                if up {{ self.loadedAt = Date(); self.web.load(URLRequest(url: home)) }}
                else {{ self.web.loadHTMLString(page("The dashboard didn't start. See " + logPath), baseURL: nil) }}
            }}
        }}
    }}

    // A window left open for days shows a stale page. Coming back after an hour reloads it (the
    // layout re-reads the database; it does not sync, that stays the Sync button's job).
    func applicationDidBecomeActive(_ n: Notification) {{
        guard web.url?.host == "127.0.0.1", Date().timeIntervalSince(loadedAt) > 3600 else {{ return }}
        loadedAt = Date()
        web.reload()
    }}

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool {{ true }}
    func applicationWillTerminate(_ n: Notification) {{ sh(kill) }}  // only what listens on port {PORT}
}}

func menu(_ title: String, _ items: [(String, Selector, String, NSEvent.ModifierFlags)]) -> NSMenuItem {{
    let m = NSMenu(title: title)
    for (name, action, key, mods) in items {{
        let i = NSMenuItem(title: name, action: action, keyEquivalent: key)
        i.keyEquivalentModifierMask = mods
        m.addItem(i)
    }}
    let top = NSMenuItem()
    top.submenu = m
    return top
}}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let bar = NSMenu()
bar.addItem(menu({_swift(NAME)}, [({_swift("Quit " + NAME)}, #selector(NSApplication.terminate(_:)), "q", .command)]))
bar.addItem(menu("Edit", [  // without these, copy and paste shortcuts don't reach the web page
    ("Undo", Selector(("undo:")), "z", .command), ("Redo", Selector(("redo:")), "z", [.command, .shift]),
    ("Cut", #selector(NSText.cut(_:)), "x", .command), ("Copy", #selector(NSText.copy(_:)), "c", .command),
    ("Paste", #selector(NSText.paste(_:)), "v", .command),
    ("Select All", #selector(NSText.selectAll(_:)), "a", .command)]))
bar.addItem(menu("View", [("Reload", #selector(WKWebView.reload(_:)), "r", .command)]))
bar.addItem(menu("Window", [("Close", #selector(NSWindow.performClose(_:)), "w", .command),
                            ("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m", .command)]))
app.mainMenu = bar
let delegate = App()
app.delegate = delegate
app.run()
'''


# Cartoon chef's knife (thick ink outline, steel blade, wood handle) on a green tile. Drawn with AppKit
# at build time (the venv has no Pillow), so no image file lives in the repo.
ICON_SWIFT = '''import Cocoa
// a fixed 1024px bitmap: lockFocus on a Retina screen would draw 2048px, which icns rejects
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
func rgb(_ h: Int) -> NSColor {
    NSColor(red: CGFloat(h >> 16 & 255) / 255, green: CGFloat(h >> 8 & 255) / 255, blue: CGFloat(h & 255) / 255, alpha: 1)
}
let ink = rgb(0x1c1b19)
func shape(_ p: NSBezierPath, _ fill: Int, line: CGFloat = 26) {
    rgb(fill).setFill(); p.fill()
    p.lineWidth = line; p.lineJoinStyle = .round; p.lineCapStyle = .round
    ink.setStroke(); p.stroke()
}
func pt(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: x, y: y) }

rgb(0x5cc58a).setFill()
NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185).fill()

// draw the knife lying flat around the origin, then tilt it so the tip points up and to the right
let t = NSAffineTransform()
t.translateX(by: 512, yBy: 500)
t.rotate(byDegrees: 42)
t.concat()

let blade = NSBezierPath()
blade.move(to: pt(-95, 70))
blade.line(to: pt(290, 70))
blade.curve(to: pt(395, 30), controlPoint1: pt(345, 70), controlPoint2: pt(380, 55))
blade.curve(to: pt(-95, -105), controlPoint1: pt(300, -95), controlPoint2: pt(120, -105))
blade.close()
shape(blade, 0xdfe5ea)

let shine = NSBezierPath()  // the cartoon glint along the spine
shine.move(to: pt(-50, 40))
shine.line(to: pt(270, 40))
shine.lineWidth = 22; shine.lineCapStyle = .round
NSColor.white.setStroke(); shine.stroke()

let edge = NSBezierPath()  // a darker bevel line just above the cutting edge
edge.move(to: pt(-60, -60))
edge.curve(to: pt(330, 0), controlPoint1: pt(120, -62), controlPoint2: pt(260, -45))
edge.lineWidth = 12; edge.lineCapStyle = .round
rgb(0xa9b4bd).setStroke(); edge.stroke()

shape(NSBezierPath(roundedRect: NSRect(x: -135, y: -120, width: 50, height: 205), xRadius: 18, yRadius: 18), 0x9aa5ae)
shape(NSBezierPath(roundedRect: NSRect(x: -400, y: -62, width: 270, height: 110), xRadius: 50, yRadius: 50), 0x9b5a2e)
for x in [-330, -255, -180] as [CGFloat] {
    shape(NSBezierPath(ovalIn: NSRect(x: x - 17, y: -24, width: 34, height: 34)), 0xf2d27a, line: 12)
}

func sparkle(_ c: NSPoint, _ r: CGFloat) {  // four-point twinkle off the tip
    let s = NSBezierPath()
    s.move(to: pt(c.x, c.y + r))
    s.curve(to: pt(c.x + r, c.y), controlPoint1: pt(c.x + r * 0.15, c.y + r * 0.15), controlPoint2: pt(c.x + r * 0.15, c.y + r * 0.15))
    s.curve(to: pt(c.x, c.y - r), controlPoint1: pt(c.x + r * 0.15, c.y - r * 0.15), controlPoint2: pt(c.x + r * 0.15, c.y - r * 0.15))
    s.curve(to: pt(c.x - r, c.y), controlPoint1: pt(c.x - r * 0.15, c.y - r * 0.15), controlPoint2: pt(c.x - r * 0.15, c.y - r * 0.15))
    s.curve(to: pt(c.x, c.y + r), controlPoint1: pt(c.x - r * 0.15, c.y + r * 0.15), controlPoint2: pt(c.x - r * 0.15, c.y + r * 0.15))
    NSColor.white.setFill(); s.fill()
}
sparkle(pt(395, 135), 52)
sparkle(pt(300, 175), 26)
NSGraphicsContext.current?.flushGraphics()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
'''


def draw_icon(path: Path, tmp: Path) -> None:
    src, exe, iconset = tmp / "icon.swift", tmp / "icon", tmp / "AppIcon.iconset"
    src.write_text(ICON_SWIFT)
    iconset.mkdir()
    subprocess.run(["swiftc", "-o", str(exe), str(src)], check=True)
    subprocess.run([str(exe), str(iconset / "icon_512x512@2x.png")], check=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(path)], check=True)  # sips can't write icns here


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    python = repo / ".venv" / "bin" / "python"
    if not python.exists() or shutil.which("swiftc") is None:
        print("Needs .venv (see CLAUDE.md setup) and swiftc on PATH (`xcode-select --install`)", file=sys.stderr)
        return 1
    app = repo / "dist" / f"{NAME}.app"
    shutil.rmtree(app, ignore_errors=True)
    app.parent.mkdir(exist_ok=True)
    # Build and sign outside the repo: a File Provider (iCloud) on ~/Documents re-adds Finder xattrs,
    # which codesign rejects. The finished, signed bundle is then moved into dist/.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        built = tmp / f"{NAME}.app"
        (built / "Contents" / "MacOS").mkdir(parents=True)
        (built / "Contents" / "Resources").mkdir()
        src = tmp / "main.swift"
        src.write_text(swift_source(repo, python))
        subprocess.run(["swiftc", "-O", "-o", str(built / "Contents" / "MacOS" / NAME), str(src)], check=True)
        with open(built / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({
                "CFBundleExecutable": NAME, "CFBundleIdentifier": "local.sharp-check",
                "CFBundleName": NAME, "CFBundlePackageType": "APPL", "CFBundleIconFile": "AppIcon",
                "CFBundleShortVersionString": "1.0", "NSHighResolutionCapable": True,
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # plain http to 127.0.0.1
            }, f)
        draw_icon(built / "Contents" / "Resources" / "AppIcon.icns", tmp)
        subprocess.run(["xattr", "-cr", str(built)], check=True)  # codesign rejects Finder xattrs
        subprocess.run(["codesign", "--force", "-s", "-", str(built)], check=True)
        shutil.move(built, app)
    print(f"Built {app}. Drag it to the Dock; closing the window or Quit stops the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
