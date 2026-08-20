import { Wallet } from "ethers";
import type { ApiKeyCreds } from "@polymarket/clob-client";

import type { PolymarketCredentials } from "../config";

export class PolymarketAuth {
  private readonly wallet: Wallet;
  private readonly credentials: ApiKeyCreds;
  private readonly funder: string;

  constructor(config: PolymarketCredentials) {
    this.wallet = new Wallet(config.privateKey);
    this.credentials = {
      key: config.apiKey,
      secret: config.apiSecret,
      passphrase: config.passphrase,
    };
    this.funder = config.funderAddress ?? this.wallet.address;
  }

  /**
   * The signer, with ethers v5's typed-data method aliased onto it.
   *
   * The CLOB client is written against ethers v5 and calls `_signTypedData`;
   * ethers v6 renamed it to `signTypedData`. Without this shim every order
   * fails at signing with a missing-method error that says nothing about
   * versions.
   */
  signer(): Wallet {
    const wallet = this.wallet as Wallet & {
      _signTypedData?: unknown;
      signTypedData?: (domain: unknown, types: unknown, value: unknown) => Promise<string>;
    };
    if (!wallet._signTypedData && wallet.signTypedData) {
      wallet._signTypedData = async (
        domain: unknown,
        types: Record<string, unknown>,
        value: unknown,
      ): Promise<string> => {
        const { EIP712Domain: _unused, ...rest } = types;
        return wallet.signTypedData!(domain, rest, value);
      };
    }
    return this.wallet;
  }

  creds(): ApiKeyCreds {
    return this.credentials;
  }

  funderAddress(): string {
    return this.funder;
  }
}
