"""
Gamma / CLOB API structure probe — RESEARCH ONLY.

Reveals how a slug maps to events -> markets -> outcomes -> token_ids,
including the negRisk and multi-outcome cases (soccer, esports, musk-tweet, etc.).

Usage (run with project venv):
  python research/gamma_probe.py discover [--limit 30] [--tag sports]
  python research/gamma_probe.py event <slug>
  python research/gamma_probe.py markets <slug>
  python research/gamma_probe.py book <token_id>
  python research/gamma_probe.py clobmarket <condition_id>

No auth needed for Gamma read endpoints or the CLOB /book endpoint.
"""
from __future__ import annotations

import json
import sys

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TIMEOUT = 15


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers={"User-Agent": "gamma-probe/0.1"})
    return r.status_code, r.text, dict(r.headers)


def _parse_json_arrays(mkt):
    """Gamma returns outcomes/outcomePrices/clobTokenIds as JSON *strings*."""
    out = {}
    for k in ("outcomes", "outcomePrices", "clobTokenIds", "gameStartTime",
              "negRiskMarketId", "negRiskRequestId"):
        if k in mkt:
            out[k] = mkt[k]
    # try parse the three parallel arrays
    for k in ("outcomes", "outcomePrices", "clobTokenIds"):
        v = mkt.get(k)
        if isinstance(v, str):
            try:
                out[k + "__parsed"] = json.loads(v)
            except Exception as e:
                out[k + "__parsed"] = f"<unparseable: {e}>"
    return out


def _short(s, n=24):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def discover(limit=30, tag=None):
    params = {"active": "true", "closed": "false", "limit": limit,
              "order": "volume", "ascending": "false"}
    if tag:
        params["tag"] = tag
    code, text, hdrs = _get(f"{GAMMA}/events", params)
    print(f"GET /events -> {code}")
    if code != 200:
        print(text[:500]); return
    data = json.loads(text)
    print(f"events returned: {len(data)}")
    print(f"{'slug':<45} {'negR':>5} {'nMk':>4}  title")
    for ev in data:
        markets = ev.get("markets") or []
        title = (ev.get("title") or ev.get("slug") or "")[:60]
        print(f"{_short(ev.get('slug',''),44):<45} {str(ev.get('negRisk'))[:5]:>5} "
              f"{len(markets):>4}  {title}")
        # show a few sub-market questions
        for m in markets[:3]:
            print(f"      └─ {_short(m.get('question',''),70)}  "
                  f"negR={m.get('negRisk')} outcomes={_short(m.get('outcomes'),30)}")
        if len(markets) > 3:
            print(f"      └─ … ({len(markets)-3} more)")


def event(slug):
    code, text, _ = _get(f"{GAMMA}/events", {"slug": slug})
    print(f"GET /events?slug={slug} -> {code}")
    if code != 200:
        print(text[:500]); return
    data = json.loads(text)
    print(f"events matched: {len(data)}")
    for ev in data:
        print("=" * 80)
        print(f"event.id         = {ev.get('id')}")
        print(f"event.slug       = {ev.get('slug')}")
        print(f"event.title      = {ev.get('title')}")
        print(f"event.negRisk    = {ev.get('negRisk')}   (event-level)")
        print(f"event.active/closed = {ev.get('active')}/{ev.get('closed')}")
        print(f"event.tags       = {ev.get('tags')}")
        markets = ev.get("markets") or []
        print(f"event.markets    = {len(markets)} sub-market(s)")
        for i, m in enumerate(markets):
            print(f"\n  --- sub-market [{i}] ---")
            _dump_market(m)


def markets(slug):
    code, text, _ = _get(f"{GAMMA}/markets", {"slug": slug})
    print(f"GET /markets?slug={slug} -> {code}")
    if code != 200:
        print(text[:500]); return
    data = json.loads(text)
    print(f"markets matched: {len(data)}")
    for i, m in enumerate(data):
        print(f"\n=== market [{i}] ===")
        _dump_market(m)


def _dump_market(m):
    print(f"  id                 = {m.get('id')}")
    print(f"  question           = {m.get('question')}")
    print(f"  slug               = {m.get('slug')}")
    print(f"  conditionId        = {m.get('conditionId')}")
    print(f"  active/closed      = {m.get('active')}/{m.get('closed')}")
    print(f"  negRisk            = {m.get('negRisk')}")
    print(f"  negRiskMarketId    = {m.get('negRiskMarketId')}")
    print(f"  negRiskRequestId   = {m.get('negRiskRequestId')}")
    print(f"  enableOrderBook    = {m.get('enableOrderBook')}")
    print(f"  orderMinSize       = {m.get('orderMinSize')}")
    print(f"  orderPriceMinTickSize = {m.get('orderPriceMinTickSize')}")
    pa = _parse_json_arrays(m)
    print(f"  outcomes (raw)     = {pa.get('outcomes')}")
    print(f"  outcomes (parsed)  = {pa.get('outcomes__parsed')}")
    print(f"  outcomePrices      = {pa.get('outcomePrices__parsed')}")
    print(f"  clobTokenIds       = {pa.get('clobTokenIds__parsed')}")
    toks = pa.get('clobTokenIds__parsed')
    outs = pa.get('outcomes__parsed')
    if isinstance(toks, list) and isinstance(outs, list) and len(toks) == len(outs):
        print(f"  >>> outcome->token mapping ({len(toks)} parallel):")
        for j, (o, t) in enumerate(zip(outs, toks)):
            print(f"        [{j}] {o:>20}  ->  {t}")


def book(token_id):
    code, text, _ = _get(f"{CLOB}/book", {"token_id": token_id})
    print(f"GET {CLOB}/book?token_id=... -> {code}")
    if code != 200:
        print(text[:500]); return
    b = json.loads(text)
    print(f"  market          = {b.get('market')}   (== Gamma conditionId)")
    print(f"  asset_id        = {_short(b.get('asset_id'),40)}")
    print(f"  neg_risk        = {b.get('neg_risk')}    <<< CLOB-level neg_risk flag")
    print(f"  tick_size       = {b.get('tick_size')}")
    print(f"  min_order_size  = {b.get('min_order_size')}")
    asks = b.get("asks") or []
    bids = b.get("bids") or []
    print(f"  asks ({len(asks)})  best_ask = {asks[0] if asks else None}")
    print(f"  bids ({len(bids)})  best_bid = {bids[0] if bids else None}")


def clobmarket(condition_id):
    code, text, _ = _get(f"{CLOB}/markets/{condition_id}")
    print(f"GET {CLOB}/markets/<conditionId> -> {code}")
    if code != 200:
        print(text[:500]); return
    m = json.loads(text)
    print(json.dumps(m, indent=2)[:2000])


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    cmd = args[0]
    if cmd == "discover":
        limit = int(args[1]) if len(args) > 1 else 30
        tag = args[2] if len(args) > 2 else None
        discover(limit, tag)
    elif cmd == "event":
        event(args[1])
    elif cmd == "markets":
        markets(args[1])
    elif cmd == "book":
        book(args[1])
    elif cmd == "clobmarket":
        clobmarket(args[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
