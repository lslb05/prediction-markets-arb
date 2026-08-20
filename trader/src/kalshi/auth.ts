import * as crypto from "node:crypto";

/**
 * Signs Kalshi requests. The TypeScript twin of
 * `src/prediction_markets/sources/kalshi/auth.py`.
 */
export class KalshiAuth {
  constructor(
    private readonly keyId: string,
    private readonly privateKey: string,
  ) {
    if (!keyId || !privateKey) {
      throw new Error("Kalshi needs both a key id and an RSA private key");
    }
  }

  headers(method: string, path: string): Record<string, string> {
    const timestamp = Date.now().toString();
    // The query string is excluded from the signed payload: Kalshi signs the
    // bare path, and including it fails every paginated request with an
    // error that reads like clock skew.
    const payload = `${timestamp}${method}${path.split("?")[0]}`;

    const signature = crypto
      .createSign("SHA256")
      .update(payload)
      .sign(
        {
          key: this.privateKey,
          padding: crypto.constants.RSA_PKCS1_PSS_PADDING,
          saltLength: crypto.constants.RSA_PSS_SALTLEN_DIGEST,
        },
        "base64",
      );

    return {
      "KALSHI-ACCESS-KEY": this.keyId,
      "KALSHI-ACCESS-SIGNATURE": signature,
      "KALSHI-ACCESS-TIMESTAMP": timestamp,
      "Content-Type": "application/json",
    };
  }
}
