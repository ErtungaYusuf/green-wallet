# Green Wallet

A wallet for [Green (GRN)](https://github.com/ErtungaYusuf/green), a page in your browser. It runs on your computer, listens only on `127.0.0.1`, and talks to a Green node's API. The miner is a separate program: [green-miner](https://github.com/ErtungaYusuf/green-miner).

## Install

Python 3.11 or newer, with Green installed (the green repository next to this one works too):

```bash
pip install -e ../green
pip install -e ".[dev]"
```

## The wallet

```bash
green-wallet --data wallet --node http://127.0.0.1:8545
```

(or `python -m green_wallet.wallet ...`). Create a wallet — a 24-word recovery phrase, shown once, and a password that encrypts the file — or restore one from its phrase; then see your address, balance and history, and send coins. The key never leaves the program: transactions are signed here and only the signed transaction goes to the node, so the node can be anyone's. The wallet file is the one Green's command line uses (`grn --data wallet ...`).

## Safety

The page only answers requests that name this computer (`Host: 127.0.0.1` or `localhost`), so a DNS-rebinding web page cannot reach it, and every action needs the page's own token, which a web site open in another tab can neither read nor send. Anyone who can run programs on your computer can still use it, like the wallet file itself.

## Tests

```bash
python -m pytest -q
```

They start a real Green node and check creating, restoring, unlocking and sending from a wallet, and that web pages from other sites cannot use it.
