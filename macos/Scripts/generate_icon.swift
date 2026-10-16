// One-off generator for Resources/AppIcon.icns. Run it by hand when the design changes; the result
// is committed, so it does not run on every build:
//
//   swift Scripts/generate_icon.swift Resources && iconutil -c icns Resources/AppIcon.iconset -o Resources/AppIcon.icns && rm -r Resources/AppIcon.iconset
import AppKit

let sizes: [(name: String, size: Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]

// First one this macOS has: a path from one point to another suits a "trail".
let symbolCandidates = [
    "point.bottomleft.forward.to.point.topright.scurvepath",
    "location.north.line",
    "arrow.triangle.turn.up.right.diamond",
]

let outputRoot = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."
let iconsetDir = "\(outputRoot)/AppIcon.iconset"
try? FileManager.default.createDirectory(atPath: iconsetDir, withIntermediateDirectories: true)

guard let symbol = symbolCandidates.lazy.compactMap({ NSImage(systemSymbolName: $0, accessibilityDescription: nil) }).first else {
    fatalError("none of the symbols exist on this macOS")
}

func drawIcon(size: Int) -> NSImage {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()

    let rect = NSRect(x: 0, y: 0, width: size, height: size)
    let path = NSBezierPath(roundedRect: rect, xRadius: CGFloat(size) * 0.22, yRadius: CGFloat(size) * 0.22)
    path.addClip()

    NSGradient(colors: [
        NSColor(calibratedRed: 0.23, green: 0.20, blue: 0.62, alpha: 1.0),
        NSColor(calibratedRed: 0.17, green: 0.42, blue: 0.85, alpha: 1.0),
    ])?.draw(in: path, angle: -45)

    let config = NSImage.SymbolConfiguration(pointSize: CGFloat(size) * 0.50, weight: .semibold)
        .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
    let configured = symbol.withSymbolConfiguration(config) ?? symbol
    let s = configured.size
    configured.draw(in: NSRect(x: (CGFloat(size) - s.width) / 2, y: (CGFloat(size) - s.height) / 2, width: s.width, height: s.height))

    image.unlockFocus()
    return image
}

for entry in sizes {
    let image = drawIcon(size: entry.size)
    guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
        let png = bitmap.representation(using: .png, properties: [:])
    else {
        print("Failed to render \(entry.name)")
        continue
    }
    try? png.write(to: URL(fileURLWithPath: "\(iconsetDir)/\(entry.name).png"))
}
print("Wrote \(sizes.count) sizes to \(iconsetDir)")
