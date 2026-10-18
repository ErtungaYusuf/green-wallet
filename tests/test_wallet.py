"""The wallet page against a real Green node."""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from green import params
from green.chain import make_genesis
from green.cli import create_wallet
from green.crypto import Key
from green.node import Node
from green_wallet.wallet import Wallet


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(cond, what, timeout=60):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, f"timed out waiting for {what}"
        time.sleep(0.1)


def call(ui, path, body=None, token=True, headers=None):
    h = {"Content-Type": "application/json", **({"X-UI-Token": ui.token} if token else {}), **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ui.url + path, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()) if path != "/" else r.read()


def refused(ui, path, **kw) -> tuple[int, str]:
    with pytest.raises(urllib.error.HTTPError) as e:
        call(ui, path, **kw)
    return e.value.code, json.loads(e.value.read()).get("error", "")


@pytest.fixture
def node(tmp_path):
    miner = Key.generate()
    n = Node(str(tmp_path / "node"), make_genesis(miner.address, "ui"), port=free_port(), miner_key=miner,
             log=lambda m: None)
    n.start(mine=True)
    yield n
    n.stop()


def test_a_wallet_is_created_unlocked_and_sends(node, tmp_path):
    w = Wallet(str(tmp_path / "w"), node.url, port=0)
    w.start()
    try:
        assert w.token.encode() in call(w, "/", token=False)
        assert call(w, "/api/state", token=False)["exists"] is False
        assert refused(w, "/api/create", body={"password": "short"})[0] == 400
        made = call(w, "/api/create", body={"password": "a good password"})
        assert len(made["phrase"].split()) == 24 and made["address"].startswith("grn1")
        st = call(w, "/api/state", token=False)
        assert st["exists"] and st["unlocked"] and st["encrypted"] and st["balance"] == 0
        # Coins arrive from the miner; the wallet sees them and sends some on.
        miner = node.miner_key
        nonce = node.pending_nonce(miner.address)
        from green.tx import make_tx
        node.submit_tx(make_tx(miner, node.chain.chain_id, "transfer", nonce, to=made["address"],
                               amount=params.YAPRAK_PER_COIN))
        wait_until(lambda: call(w, "/api/state", token=False)["balance"] == params.YAPRAK_PER_COIN, "the coins")
        friend = Key.generate().address
        assert refused(w, "/api/send", body={"to": "nonsense", "amount": "1"})[0] == 400
        code, why = refused(w, "/api/send", body={"to": friend, "amount": "5"})
        assert code == 400 and "not enough" in why
        sent = call(w, "/api/send", body={"to": friend, "amount": "0.25"})
        wait_until(lambda: node.chain.state.balance(friend) == params.YAPRAK_PER_COIN // 4, "the payment")
        assert len(sent["txid"]) == 64
        hist = call(w, "/api/state", token=False)["history"]
        assert {h["role"] for h in hist} >= {"sender", "recipient"}
        assert any(h.get("amount") == params.YAPRAK_PER_COIN // 4 for h in hist)
        # Locked, it cannot send; the wrong password does not unlock it; the right one does.
        call(w, "/api/lock", body={})
        assert refused(w, "/api/send", body={"to": friend, "amount": "0.1"})[1] == "unlock the wallet first"
        assert refused(w, "/api/unlock", body={"password": "wrong password"})[1] == "wrong password"
        assert call(w, "/api/unlock", body={"password": "a good password"})["address"] == made["address"]
    finally:
        w.stop()


def test_a_wallet_restores_from_its_phrase(node, tmp_path):
    phrase_owner = tmp_path / "orig"
    key = create_wallet(str(phrase_owner), None)
    with open(phrase_owner / "wallet.json", encoding="utf-8") as f:
        phrase = json.load(f)["phrase"]
    w = Wallet(str(tmp_path / "restored"), node.url, port=0)
    w.start()
    try:
        assert refused(w, "/api/restore", body={"phrase": "not a phrase", "password": "a good password"})[0] == 400
        assert call(w, "/api/restore", body={"phrase": phrase, "password": "a good password"})["address"] == key.address
        assert refused(w, "/api/create", body={"password": "a good password"})[1] == "this folder already holds a wallet"
    finally:
        w.stop()



def test_web_pages_cannot_use_the_wallet(node, tmp_path):
    w = Wallet(str(tmp_path / "w"), node.url, port=0)
    w.start()
    try:
        call(w, "/api/create", body={"password": "a good password"})
        assert refused(w, "/api/send", body={"to": Key.generate().address, "amount": "0.1"}, token=False)[0] == 403
        assert refused(w, "/", token=False, headers={"Host": "evil.example"})[0] == 403
        assert refused(w, "/api/state", token=False, headers={"Host": "evil.example:8610"})[0] == 403
    finally:
        w.stop()
