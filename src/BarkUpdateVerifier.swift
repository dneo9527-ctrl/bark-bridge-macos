import Foundation
import CryptoKit

// This helper only verifies public release metadata. Signing keys are never bundled.
do {
    let args = CommandLine.arguments
    guard args.count == 4, args[1] == "verify" else { exit(2) }
    let resource = URL(fileURLWithPath: args[0]).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources/update-public-key.txt")
    let encoded = try String(contentsOf: resource, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard let keyData = Data(base64Encoded: encoded), keyData.count == 32 else { exit(3) }
    let key = try Curve25519.Signing.PublicKey(rawRepresentation: keyData)
    let data = try Data(contentsOf: URL(fileURLWithPath: args[2]))
    let signatureText = try String(contentsOfFile: args[3], encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard data.count <= 262144, let signature = Data(base64Encoded: signatureText), signature.count == 64,
          key.isValidSignature(signature, for: data) else { exit(4) }
    exit(0)
} catch { exit(5) }
