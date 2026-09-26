"""The MCP server: Assay's two paid routes as tools, plus a free status tool.

Each paid call: ask Assay without payment, read the 402 challenge, check it asks for the SDK-listed USDC on Base,
reserve the price against the user's daily limit (in the local spend book), then sign and pay with the user's
wallet and close the reservation as settled or failed. Past the limit a tool answers "limit reached" and pays
nothing. Nothing here decides a price: Assay's challenge states it, and the user's limit caps it.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import requests
from x402 import x402ClientSync
from x402.http.utils import (decode_payment_required_header, decode_payment_response_header,
                             encode_payment_signature_header)
from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS
from x402.mechanisms.evm.exact import ExactEvmScheme

from assay_mcp import __version__
from assay_mcp.spend import LimitReached, SpendBook, SpendFileDamaged

BASE_NETWORKS = ("eip155:8453", "eip155:84532")  # Base mainnet, and Base Sepolia for testing
POOLS, VERIFY = "/v1/pools/new", "/v1/verify"


def usdc_on(network: str) -> str | None:
    for a in DEFAULT_ASSETS.get(network, []):
        if a["symbol"] == "USDC":
            return a["asset"]
    return None


def usd_text(v: Decimal) -> str:
    """Two decimals for whole cents; a finer price keeps all its digits."""
    return f"{v:.2f}" if v == v.quantize(Decimal("0.01")) else f"{v.normalize():f}"


class Refused(Exception):
    """Assay's challenge was not something this server pays; nothing was paid."""


def price_of(challenge_header: str) -> tuple[Any, Any, Decimal]:
    """(the payment-required object, the one requirement we would pay, its price in USD). Only the SDK-listed USDC on
    Base (or Base Sepolia) is paid."""
    required = decode_payment_required_header(challenge_header)
    for req in required.accepts:
        if req.network in BASE_NETWORKS and str(req.asset).lower() == (usdc_on(req.network) or "").lower():
            return required, req, Decimal(int(req.amount)) / Decimal(10**6)
    raise Refused("Assay asked for payment in something other than USDC on Base: "
                  f"{[(r.network, r.asset) for r in required.accepts]}; nothing was paid")


class AssayCaller:
    def __init__(self, *, url: str, signer: Any, book: SpendBook, session: requests.Session | None = None,
                 save_dir: str | Path | None = None) -> None:
        self.url = url.rstrip("/")
        self.book = book
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = f"assay-mcp/{__version__}"
        self.client = x402ClientSync()
        for network in BASE_NETWORKS:
            self.client.register(network, ExactEvmScheme(signer=signer))
        self.save_dir = Path(save_dir or Path.home() / ".assay-mcp" / "answers").expanduser()
        self.payer = getattr(signer, "address", None)

    def challenge(self, method: str, path: str, **kw) -> requests.Response:
        return self.session.request(method, self.url + path, timeout=60, **kw)

    def paid(self, method: str, path: str, **kw) -> tuple[requests.Response, dict[str, Any]]:
        """(Assay's final answer, a payment record for the tool result)."""
        first = self.challenge(method, path, **kw)
        if first.status_code != 402:  # refused before payment (400/413/422/503), or served free: nothing paid
            return first, {"paid": False, "why": f"Assay answered {first.status_code} before any payment"}
        required, req, usd = price_of(first.headers.get("payment-required", ""))
        rid = self.book.reserve(f"{method} {path}", usd)  # LimitReached: nothing is signed
        # From here an error leaves the outcome unknown: the reservation stays open and keeps counting.
        payload = self.client.create_payment_payload(required.model_copy(update={"accepts": [req]}))
        final = self.session.request(method, self.url + path, timeout=200,
                                     headers={"PAYMENT-SIGNATURE": encode_payment_signature_header(payload)}, **kw)
        receipt = final.headers.get("payment-response")
        try:
            settled = decode_payment_response_header(receipt) if receipt else None
        except Exception:
            settled = None
        if settled is not None and settled.success:
            self.book.close(rid, "settled", settled.transaction)
            return final, {"paid": True, "usd": usd_text(usd), "network": req.network,
                           "transaction": settled.transaction}
        if settled is not None:  # a receipt that says it failed: the money did not move
            self.book.close(rid, "failed")
            return final, {"paid": False, "usd": usd_text(usd),
                           "why": f"the payment did not settle: {settled.error_reason}"}
        # no readable receipt: the outcome is unknown, so the reservation stays open and keeps counting
        return final, {"paid": "unknown", "usd": usd_text(usd),
                       "why": f"HTTP {final.status_code} without a readable payment receipt; ${usd_text(usd)} stays "
                              "counted against today's limit"}

    def save(self, name: str, body: bytes) -> str:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        p = self.save_dir / f"{stamp}-{name}-{uuid.uuid4().hex[:8]}.json"
        with open(p, "xb") as fh:  # never overwrite an earlier paid answer
            fh.write(body)
        return str(p)


def condense_pools(answer: dict[str, Any], saved_to: str) -> dict[str, Any]:
    """A pool page is about 3 KB per pool; an agent gets the essentials of each and the path of the full answer."""
    def symbols(p):
        toks = ((p.get("facts") or {}).get("tokens")) or []
        return [((t.get("symbol") or {}).get("value")) for t in toks]
    return {
        "read_at": answer.get("read_at"), "window_hours": answer.get("window_hours"),
        "safe_head_block": answer.get("safe_head_block"), "next_cursor": answer.get("next_cursor"),
        "pools": [{"pool": p.get("pool"), "dex": p.get("dex"), "created_block": p.get("created_block"),
                   "token_symbols": symbols(p),
                   "paired_with": (p.get("paired_with") or {}).get("value"),
                   "flags": {k: (v or {}).get("value") for k, v in (p.get("flags") or {}).items()}}
                  for p in answer.get("pools", [])],
        "missing": answer.get("missing"), "terms": answer.get("terms"),
        "condensed": f"condensed by assay-mcp; the full paid answer, with every fact's read block and evidence, is "
                     f"saved at {saved_to}",
    }


def _price_text(caller: AssayCaller, method: str, path: str, **kw) -> str:
    try:
        r = caller.challenge(method, path, **kw)
        _, req, usd = price_of(r.headers.get("payment-required", ""))
        return f"Costs ${usd_text(usd)} in USDC on {'Base' if req.network == 'eip155:8453' else req.network}, paid from your wallet"
    except Exception:
        return "The price is read from Assay's payment challenge at call time (see assay_status)"


def build_server(caller: AssayCaller) -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("assay", instructions=(
        "Assay sells two answers, paid per call in USDC on Base from the user's wallet: newly created Base liquidity "
        "pools with measured facts, and a multi-model check of a claim against pasted sources. Each paid call counts "
        "against the user's daily limit; assay_status is free and shows prices and today's spend."))
    pools_price = _price_text(caller, "GET", POOLS)
    verify_price = _price_text(caller, "POST", VERIFY, json={"claim": "price check", "sources": ["price check"]})

    @server.tool(description=(
        f"Liquidity pools created on Base mainnet in Assay's recent window, newest first, one page per call. "
        f"{pools_price}. Filters: dex (an exchange name), flag / not_flag (a flag name), cursor (the previous "
        "page's next_cursor). Each fact carries its read block; flags are true, false or unknown. Returns a condensed "
        "page and the path of the full paid answer."))
    def new_base_pools(dex: str | None = None, flag: str | None = None, not_flag: str | None = None,
                       cursor: str | None = None) -> dict:
        params = {k: v for k, v in {"dex": dex, "flag": flag, "not_flag": not_flag, "cursor": cursor}.items() if v}
        return _run(caller, "pools", "GET", POOLS, params=params)

    @server.tool(description=(
        f"Check a claim against passages you paste, with several independent model families; returns every "
        f"family's vote with the quotes it relied on, each checked verbatim against your sources. No single verdict. "
        f"{verify_price}. Pasted text only; URLs are not fetched. The payment settles before the models run."))
    def verify_claim(claim: str, sources: list[str], questions: list[str] | None = None) -> dict:
        return _run(caller, "verify", "POST", VERIFY, json={"claim": claim, "sources": sources,
                                                            "questions": questions or []})

    @server.tool(description="Free. Assay's current prices, your wallet, and today's spend against your daily limit.")
    def assay_status() -> dict:
        out: dict[str, Any] = {"assay": caller.url, "terms": caller.url + "/terms", "wallet": caller.payer,
                               "daily_limit_usd": usd_text(caller.book.limit)}
        try:
            out["spent_today_usd"] = usd_text(caller.book.spent_today_locked())
        except SpendFileDamaged as exc:  # reported, never a crash: payment is refused until it is fixed
            out["spent_today_usd"] = f"unknown: {exc}"
        for name, (m, p, kw) in {"pools": ("GET", POOLS, {}),
                                 "verify": ("POST", VERIFY, {"json": {"claim": "price check",
                                                                      "sources": ["price check"]}})}.items():
            try:
                _, req, usd = price_of(caller.challenge(m, p, **kw).headers.get("payment-required", ""))
                out[f"{name}_price_usd"], out["network"] = usd_text(usd), req.network
            except Exception as exc:
                out[f"{name}_price_usd"] = f"unavailable: {type(exc).__name__}"
        return out

    return server


def _run(caller: AssayCaller, name: str, method: str, path: str, **kw) -> dict:
    try:
        resp, payment = caller.paid(method, path, **kw)
    except LimitReached as exc:
        return {"limit_reached": str(exc)}
    except SpendFileDamaged as exc:
        return {"refused": str(exc)}
    except Refused as exc:
        return {"refused": str(exc)}
    try:
        answer = resp.json()
    except ValueError:
        answer = {"status": resp.status_code, "body": resp.text[:500]}
    if name == "pools" and resp.status_code == 200 and isinstance(answer, dict) and "pools" in answer:
        answer = condense_pools(answer, caller.save(name, resp.content))
    return {"status": resp.status_code, "payment": payment, "answer": answer}


def main() -> None:
    """Entry point: configuration from the environment. The daily limit is required and has no default."""
    from decimal import InvalidOperation

    from eth_account import Account
    from x402.mechanisms.evm import EthAccountSigner

    missing = [k for k in ("ASSAY_PAYER_PRIVATE_KEY", "ASSAY_MAX_USD_PER_DAY") if not os.environ.get(k)]
    if missing:
        sys.exit(f"assay-mcp: set {', '.join(missing)} (the daily limit is yours to choose; there is no default)")
    try:
        limit = Decimal(os.environ["ASSAY_MAX_USD_PER_DAY"])
        book = SpendBook(os.environ.get("ASSAY_SPEND_FILE", "~/.assay-mcp/spend.jsonl"), limit)
    except (InvalidOperation, ValueError):
        sys.exit("assay-mcp: ASSAY_MAX_USD_PER_DAY must be a positive amount in USD")
    signer = EthAccountSigner(Account.from_key(os.environ["ASSAY_PAYER_PRIVATE_KEY"]))
    caller = AssayCaller(url=os.environ.get("ASSAY_URL", "https://assay.cascadiantech.com"), signer=signer, book=book)
    build_server(caller).run("stdio")


if __name__ == "__main__":
    main()
