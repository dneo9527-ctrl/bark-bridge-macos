// Release-maintainer tool. Private key is created once in a restricted local file.
import Foundation
import CryptoKit
import Darwin

umask(0o077)
let args = CommandLine.arguments
do {
    guard args.count >= 4 else { exit(2) }
    let path = URL(fileURLWithPath: args[2])
    if args[1] == "create" {
        guard args.count == 4, !FileManager.default.fileExists(atPath: path.path) else { exit(3) }
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let key = Curve25519.Signing.PrivateKey()
        // withoutOverwriting avoids replacing a key on repeated runs.
        try key.rawRepresentation.write(to: path, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        try (key.publicKey.rawRepresentation.base64EncodedString() + "\n").write(toFile: args[3], atomically: true, encoding: .utf8)
    } else if args[1] == "sign" {
        guard args.count == 5 else { exit(2) }
        let key = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(contentsOf: path))
        let signature = try key.signature(for: Data(contentsOf: URL(fileURLWithPath: args[3])))
        try (signature.base64EncodedString() + "\n").write(toFile: args[4], atomically: true, encoding: .utf8)
    } else { exit(2) }
} catch { fputs("Release signing stopped.\n", stderr); exit(1) }
