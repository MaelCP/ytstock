// ytstock barre de menu — client léger qui réutilise le serveur ytstock (:8787).
// Copie un lien YouTube, clique, il l'envoie à l'app pour téléchargement.
import Cocoa

let SERVER = "http://127.0.0.1:8787"
let QUALITIES = ["max", "1080", "720", "480", "360", "audio"]

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var quality = "720"

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Y"
        item.button?.font = .boldSystemFont(ofSize: 15)
        let menu = NSMenu()
        menu.delegate = self          // reconstruit à chaque ouverture (presse-papiers frais)
        item.menu = menu
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let clip = (NSPasteboard.general.string(forType: .string) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let hasURL = clip.contains("youtu")
        let dl = NSMenuItem(title: hasURL ? "⬇  Télécharger le lien copié" : "Copie d'abord un lien YouTube",
                            action: hasURL ? #selector(download) : nil, keyEquivalent: "")
        dl.target = self
        menu.addItem(dl)
        if hasURL {
            let preview = clip.count > 48 ? String(clip.prefix(48)) + "…" : clip
            menu.addItem(NSMenuItem(title: "   " + preview, action: nil, keyEquivalent: ""))
        }
        menu.addItem(.separator())
        let qSub = NSMenu()
        for q in QUALITIES {
            let mi = NSMenuItem(title: q + (q == quality ? "  ✓" : ""), action: #selector(setQuality(_:)), keyEquivalent: "")
            mi.representedObject = q; mi.target = self
            qSub.addItem(mi)
        }
        let qItem = NSMenuItem(title: "Qualité : \(quality)", action: nil, keyEquivalent: "")
        qItem.submenu = qSub
        menu.addItem(qItem)
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Ouvrir ytstock…", action: #selector(openApp), keyEquivalent: "o")
        open.target = self; menu.addItem(open)
        let quit = NSMenuItem(title: "Quitter", action: #selector(quit), keyEquivalent: "q")
        quit.target = self; menu.addItem(quit)
    }

    @objc func setQuality(_ s: NSMenuItem) { quality = s.representedObject as! String }
    @objc func openApp() { let p = Process(); p.launchPath = "/usr/bin/open"; p.arguments = ["-a", "ytstock"]; try? p.run() }
    @objc func quit() { NSApp.terminate(nil) }

    @objc func download() {
        let clip = (NSPasteboard.general.string(forType: .string) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard clip.contains("youtu") else { return }
        flash("…")
        var comps = URLComponents(string: SERVER + "/api/download")!
        comps.queryItems = [.init(name: "q", value: quality), .init(name: "url", value: clip)]
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "POST"
        req.setValue(SERVER, forHTTPHeaderField: "Origin")   // anti-CSRF du serveur
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            var sym = "✕"
            if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                if o["ok"] as? Bool == true { sym = "✓" }
                else if o["busy"] as? Bool == true { sym = "⏳" }
            }
            DispatchQueue.main.async { self.flash(sym) }
        }.resume()
    }

    func flash(_ s: String) {
        item.button?.title = s
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.item.button?.title = "Y" }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)    // pas d'icône dans le Dock
let d = App()
app.delegate = d
app.run()
