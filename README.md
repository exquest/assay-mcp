# assay-mcp

Assay's two paid answers as MCP tools, paid per call in USDC on Base from **your own wallet**, under a **daily limit
you set**. It is a local stdio MCP server, tested over stdio with the MCP Python SDK's client; if your MCP client has
trouble with it, open an issue.

| tool | what | cost |
|---|---|---|
| `new_base_pools` | liquidity pools created on Base mainnet in Assay's recent window, with measured facts and flags | Assay's pools price, read from its live payment challenge |
| `verify_claim` | several independent model families check a claim against passages you paste | Assay's verify price, read the same way |
| `assay_status` | prices, your wallet, today's spend against your limit | free |

## Setup

1. A wallet holding USDC on Base. Use a **dedicated wallet with a small balance**: the key sits in your MCP
   client's configuration. x402 payments need no ETH for gas.
2. Choose your daily limit in USD. There is no default: the server will not start without one.

Add it to your MCP client's `mcpServers` configuration. It needs [uv](https://docs.astral.sh/uv/):

```json
{
  "mcpServers": {
    "assay": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/exquest/assay-mcp", "assay-mcp"],
      "env": {"ASSAY_PAYER_PRIVATE_KEY": "0x…", "ASSAY_MAX_USD_PER_DAY": "<your limit>"}
    }
  }
}
```

Optional: `ASSAY_URL` (default `https://assay.cascadiantech.com`), `ASSAY_SPEND_FILE` (default
`~/.assay-mcp/spend.jsonl`).

## How the limit works

Before any payment is signed, its price is reserved in the spend file; afterwards it is closed as settled or
failed. Past the limit, a tool answers `limit_reached` and pays nothing. A payment whose outcome is unknown keeps
counting. Days are UTC. Restarting the server does not reset the limit.

## What you get

`verify_claim` returns Assay's answer as is. `new_base_pools` returns a condensed page (each pool's address, exchange,
creation block, token symbols, pairing and flags) and saves the full paid answer, with every fact's read block and
evidence, under `~/.assay-mcp/answers/`. Assay's terms: https://assay.cascadiantech.com/terms

## About

Assay is run by Cascadian Tech LLC (https://assay.cascadiantech.com, assay@cascadiantech.com). This client is MIT
licensed; each paid call is a purchase from Assay under its terms. It is not on PyPI yet.

## Spend control for your own agents

Assay pays for its own model calls through a spend-control ledger: allow, deny or hold before anything is
signed, hard caps no approval can lift, a named approver on every exception, and a hash-chained log that shows
if anyone edits it afterwards. If you'd want that, or the evidence report it produces, for your own agents,
[tell us here](../../issues/new?template=ledger-interest.md).
