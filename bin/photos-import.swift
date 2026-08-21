#!/usr/bin/swift
// Imports an image file into Photos.app via PhotoKit — no UI, no window.
// Usage: photos-import /path/to/image.png
// Compile once: swiftc photos-import.swift -o photos-import

import Photos
import Foundation

let args = CommandLine.arguments
guard args.count > 1 else {
    fputs("Usage: photos-import <file>\n", stderr)
    exit(1)
}

let fileURL = URL(fileURLWithPath: args[1])
guard FileManager.default.fileExists(atPath: fileURL.path) else {
    fputs("File not found: \(fileURL.path)\n", stderr)
    exit(1)
}

let sema = DispatchSemaphore(value: 0)

PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
    guard status == .authorized || status == .limited else {
        fputs("Photos authorization denied\n", stderr)
        sema.signal()
        return
    }
    PHPhotoLibrary.shared().performChanges({
        PHAssetChangeRequest.creationRequestForAssetFromImage(atFileURL: fileURL)
    }, completionHandler: { success, error in
        if !success, let error = error {
            fputs("Import failed: \(error.localizedDescription)\n", stderr)
        }
        sema.signal()
    })
}

sema.wait()
