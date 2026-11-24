// Face boxes for a list of images, using Apple's Vision framework (on-device).
// Output: JSON [{file, w, h, faces: [[x, y, w, h, confidence], ...]}], coords normalized, origin top-left.
import Foundation
import ImageIO
import Vision

func load(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

var out: [[String: Any]] = []
for path in CommandLine.arguments.dropFirst() {
    guard let img = load(path) else {
        out.append(["file": path, "w": 0, "h": 0, "faces": []])
        continue
    }
    let req = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    try? handler.perform([req])
    var faces: [[Double]] = []
    for f in req.results ?? [] {
        let b = f.boundingBox
        faces.append([Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height), Double(f.confidence)])
    }
    out.append(["file": path, "w": img.width, "h": img.height, "faces": faces])
}
let data = try! JSONSerialization.data(withJSONObject: out)
FileHandle.standardOutput.write(data)
