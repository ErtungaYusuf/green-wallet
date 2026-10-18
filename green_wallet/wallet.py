"""The Green wallet: a page in the browser for one wallet folder.

    python -m green_wallet.wallet [--data FOLDER] [--node URL] [--port 8610]

Create a wallet (a 24-word recovery phrase, encrypted with a password) or restore one
from its phrase, see the address, balance and history, and send coins. The key never
leaves this program: transactions are signed here and only the signed transaction
goes to the node. The node only needs to be reachable (it can be anyone's); the
wallet file is the same as the command line's (`grn wallet`), so both can use it.
"""

import argparse
import json
import os
import threading
import time
import webbrowser

from green import hd, params, tx as txmod
from green.cli import create_wallet, wallet_path
from green.crypto import Key, WrongPassword, decrypt_secret, is_address
from green.node import http_get, http_post

from .server import LocalUI, UIError

MIN_PASSWORD = 8


def _read(data: str) -> dict | None:
    try:
        with open(wallet_path(data), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def unlock(w: dict, password: str) -> Key:
    """The wallet's key from its file (and password, if it is encrypted)."""
    try:
        if "path" in w:                                  # made from a recovery phrase
            secret = ({"phrase": w["phrase"], "passphrase": w.get("passphrase", "")} if "phrase" in w
                      else json.loads(bytes.fromhex(decrypt_secret(w["encrypted_phrase"], password))))
            key = hd.key_from_phrase(secret["phrase"], secret["passphrase"], w["path"])
        elif "secret" in w:
            key = Key.from_hex(w["secret"])
        else:
            key = Key.from_hex(decrypt_secret(w["encrypted"], password))
    except WrongPassword:
        raise UIError("wrong password") from None
    if key.address != w["address"]:
        raise UIError("the wallet file is corrupt: the address does not match the key")
    return key


class Wallet(LocalUI):
    def __init__(self, data: str, node: str, port: int = 8610):
        super().__init__("wallet.html", port, "Green wallet")
        self.data, self.node = data, node.rstrip("/")
        self.key: Key | None = None
        self.lock = threading.Lock()

    # -- reading --

    def get_state(self, q: dict) -> dict:
        w = _read(self.data)
        out = {"exists": w is not None, "unlocked": self.key is not None, "folder": os.path.abspath(self.data),
               "node": self.node}
        if w is None:
            return out
        addr = w["address"]
        out.update(address=addr, encrypted="phrase" not in w and "secret" not in w)
        try:
            st = http_get(f"{self.node}/status", timeout=5)
            acct = http_get(f"{self.node}/account/{addr}", timeout=5)
            fee = http_get(f"{self.node}/v1/fee?blocks=2", timeout=5)
            out.update(online=True, height=st["height"], balance=acct["balance"], pending=acct["pending_balance"],
                       transfer_fee=fee["transfer_fee"], history=self._history(addr))
        except OSError as e:
            out.update(online=False, error=str(e))
        return out

    def _history(self, addr: str) -> list[dict]:
        h = http_get(f"{self.node}/v1/address/{addr}/history?limit=15", timeout=5)
        rows = [dict(p, height=None) for p in h.get("pending", [])] + h.get("items", [])
        for r in rows[:15]:                              # amounts of transfers, from the transactions
            if r.get("type") == "transfer" and r.get("txid"):
                try:
                    t = http_get(f"{self.node}/v1/tx/{r['txid']}", timeout=5)["tx"]
                    r["amount"], r["other"] = t["amount"], t["to"] if r["role"] == "sender" else txmod.sender(t)
                except (OSError, KeyError, TypeError):
                    pass
        return rows[:15]

    # -- acting --

    def post_create(self, body: dict) -> dict:
        """A new wallet; the phrase is shown once, to be written down."""
        if _read(self.data) is not None:
            raise UIError("this folder already holds a wallet")
        password = body.get("password", "")
        if len(password) < MIN_PASSWORD:
            raise UIError(f"choose a password of at least {MIN_PASSWORD} characters")
        phrase = hd.new_phrase(24)
        with self.lock:
            self.key = create_wallet(self.data, password, phrase)
        return {"address": self.key.address, "phrase": phrase}

    def post_restore(self, body: dict) -> dict:
        if _read(self.data) is not None:
            raise UIError("this folder already holds a wallet")
        password = body.get("password", "")
        if len(password) < MIN_PASSWORD:
            raise UIError(f"choose a password of at least {MIN_PASSWORD} characters")
        try:
            phrase = hd.normalize_phrase(body.get("phrase", ""))
        except hd.BadPhrase as e:
            raise UIError(f"that phrase cannot be used: {e}") from None
        with self.lock:
            self.key = create_wallet(self.data, password, phrase)
        return {"address": self.key.address}

    def post_unlock(self, body: dict) -> dict:
        w = _read(self.data)
        if w is None:
            raise UIError("no wallet in this folder yet")
        key = unlock(w, body.get("password", ""))
        with self.lock:
            self.key = key
        return {"address": key.address}

    def post_lock(self, body: dict) -> dict:
        with self.lock:
            self.key = None
        return {"locked": True}

    def post_send(self, body: dict) -> dict:
        with self.lock:
            key = self.key
        if key is None:
            raise UIError("unlock the wallet first")
        to = str(body.get("to", "")).strip()
        if not is_address(to):
            raise UIError("that is not a Green address (they start with grn1)")
        amount = params.parse_amount(str(body.get("amount", "")))
        if amount <= 0:
            raise UIError("the amount must be positive")
        chain_id = http_get(f"{self.node}/status", timeout=10)["genesis"]
        acct = http_get(f"{self.node}/account/{key.address}", timeout=10)
        fields = {"to": to, "amount": amount}
        fee = txmod.min_fee({"type": "transfer", **fields})
        rate = http_get(f"{self.node}/v1/fee?blocks=2", timeout=10)["fee_rate"]
        size = txmod.tx_size(txmod.make_tx(key, chain_id, "transfer", acct["pending_nonce"], fee, **fields))
        fee = max(fee, int(rate * size) + 1 if rate else 0)
        if amount + fee > acct["pending_balance"]:
            raise UIError(f"not enough coins: {params.fmt_amount(acct['pending_balance'])} available, "
                          f"{params.fmt_amount(amount + fee)} needed with the fee")
        t = txmod.make_tx(key, chain_id, "transfer", acct["pending_nonce"], fee, **fields)
        return {"txid": http_post(f"{self.node}/tx", t, timeout=10)["txid"], "fee": fee}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m green_wallet.wallet", description="The Green wallet, in a browser")
    ap.add_argument("--data", default="wallet", help="the wallet's folder (default: wallet)")
    ap.add_argument("--node", default=f"http://127.0.0.1:{params.DEFAULT_PORT}", help="a Green node's API")
    ap.add_argument("--port", type=int, default=8610)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    w = Wallet(args.data, args.node, args.port)
    w.start()
    print(f"Green wallet: {w.url}   (folder {os.path.abspath(args.data)}, node {args.node}); Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(w.url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        w.stop()


if __name__ == "__main__":
    main()
