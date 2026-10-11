# App integrity

A server can ask for proof that a request comes from the real app. There are two checks. Each check is off until the operator configures it. Requests with an API key, and the MCP endpoint, do not use these checks.

| Check | Setting that turns it on | Applies to |
|-------|--------------------------|------------|
| App Attest | `APP_ATTEST_APP_IDS` | `POST /v1/accounts` |
| App code | `APP_TOTP_SECRET` | `POST /v1/attest/challenge`, `POST /v1/accounts`, and each request with an account token |

## App Attest

With this check, only an app that Apple attests can make an account. The account token is the credential after that. A second device of the same account does not attest again.

### Flow

1. The app makes a key with `DCAppAttestService.generateKey`.
2. The app gets a challenge from the server.
3. The app calls `attestKey` with the key ID and the client data hash. The client data hash is the SHA-256 hash of the UTF-8 bytes of the challenge text.
4. The app sends the attestation in `POST /v1/accounts`.

### Get a challenge

`POST /v1/attest/challenge` needs no credential and no body.

```json
{ "challenge": "mZ3vV5mVZc0c1d9oXoY3y6W4mJ8H2Qk0bP7r", "expires_in": 300 }
```

A challenge works one time, and for 300 seconds. An attempt that fails also uses the challenge. The route works also on a server that has the check off, so the app can use one flow for all servers.

### Create the account

```json
{
  "attestation": {
    "key_id": "<base64 key ID from generateKey>",
    "challenge": "<the challenge text>",
    "object": "<base64 attestation object from attestKey>"
  }
}
```

| Case | Result |
|------|--------|
| The attestation is valid | `201 {"account": Account, "token": "mca_..."}` |
| The check is on and the body has no `attestation` | `401` with the code `attestation_required` |
| The challenge is not known, is used, or is expired | `401` with the code `attestation_invalid` |
| The attestation fails validation | `401` with the code `attestation_invalid` |
| The attested key has an account already | `401` with the code `attestation_invalid` |
| `key_id` or `object` is not base64 text, or `key_id` is not 32 bytes | `422` |
| The check is off | The server ignores `attestation` and makes the account |

### What the server validates

The server follows Apple's procedure in [Validating apps that connect to your server](https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server).

1. The certificates in `x5c` chain to the Apple App Attestation Root CA, and each is valid at the time of the request.
2. The nonce in the credential certificate is the SHA-256 hash of the authenticator data plus the client data hash.
3. The key ID is the SHA-256 hash of the public key in the credential certificate.
4. The RP ID hash is the SHA-256 hash of one of the configured app IDs (`TEAMID.bundle.identifier`).
5. The counter is 0.
6. The `aaguid` is the one of the configured environment: `production` or `development`.
7. The credential ID is the key ID.
8. If the authenticator data has the `apple_validation_category_01` extension, its value is 1 to 6. The values 0, 7, 8, and 9 are refused.

The server keeps the key ID, the public key, and the counter with the account. One attested key makes one account. When the account is deleted, the key can make a new account.

A build from Xcode uses the `development` environment unless its entitlement says `production`. A server accepts one environment.

## App code

With this check, the app proves that it has a secret that is built into it. The app sends a time-based one-time password (TOTP, RFC 6238) in the header `X-App-Code`.

```http
GET /v1/account HTTP/1.1
Host: relaydeck.onrender.com
Authorization: Bearer mca_...
X-App-Code: 46119246
```

| Parameter | Value |
|-----------|-------|
| Algorithm | HMAC-SHA-256 |
| Digits | 8 |
| Time step | 30 seconds |
| Secret | The bytes of the base32 text in `APP_TOTP_SECRET` |
| Accepted | The code of the current step, and of one step before or after it |

A missing or wrong code gives `401` with the code `app_code_invalid`. The header is necessary on `POST /v1/attest/challenge`, on `POST /v1/accounts`, and on each request with an account token, also `GET /v1/events`.

```swift
import CryptoKit
import Foundation

func appCode(secret: Data, at date: Date = .now) -> String {
    var counter = UInt64(date.timeIntervalSince1970 / 30).bigEndian
    let mac = HMAC<SHA256>.authenticationCode(
        for: Data(bytes: &counter, count: 8),
        using: SymmetricKey(data: secret)
    )
    let bytes = Array(mac)
    let offset = Int(bytes[bytes.count - 1] & 0x0f)
    let number = bytes[offset..<offset + 4].reduce(UInt32(0)) { $0 << 8 | UInt32($1) } & 0x7fff_ffff
    return String(format: "%08d", number % 100_000_000)
}
```
