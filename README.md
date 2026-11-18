# Dip-Buy BTC Bot — btc-race-24h-5

[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Rifat7600/btc-race-24h-5/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Rifat7600/btc-race-24h-5/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

A dip-buying BTC trading bot for make.fun's "Race a BTC trading bot live for 24 hours" bounty. It speaks the `agent-fun/bars-replay/v1` protocol: newline-delimited JSON on stdin/stdout, one reply per batch.

## Strategy

Long only, no leverage. The bot watches the CBBTC one-minute bars and buys sharp intraday dips in small scaled tranches: a 10-minute drop beyond a multiple of recent 5-minute volatility, confirmed by the last minute ticking up. The first tranche is half the equity; if the dip extends a further quarter percent, a second tranche averages down. It sells on the recovery (a take-profit above the average entry), on a tight stop if the dip keeps falling, on a trailing exit once the bounce stalls, or after a time limit. It never opens a position in the last hour of the window, when there is no time left for a bounce. Everything is deterministic: no clock, no randomness.

## Dev-window result (local replay)

Net PnL **+$8.89** on $1,000 start, 7 filled trades (3 winning round trips, no losses), max drawdown 0.31%. Two runs produce byte-identical action logs.

## Run it

```sh
python3 main.py
```

The bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` per batch on stdout. Python 3.13, standard library only, no network, no other files.

---

<sub>Entered on [make.fun](https://make.fun/?ref=rifat100&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
