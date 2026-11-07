"""What raising an item's quality at a repair station costs on average, failures included. Read-only arithmetic.

From wiki.albiononline.com/wiki/Quality (the Lands Awaken reroll table, read 2026-11-07): a reroll costs the item value
(not the market price) times the current quality's modifier, and lands on a higher quality with the chances below or
leaves the item as it is; it never lowers the quality. Global Discount and Gold Market Stabilization also change the
price, which the scale setting stands for. Item value doubles with each enchantment level, so rerolling before
enchanting is the cheaper order.
"""
from __future__ import annotations

# Quality 1 Normal, 2 Good, 3 Outstanding, 4 Excellent, 5 Masterpiece.
REROLL_MODIFIER = {1: 4.4, 2: 5.5, 3: 6.6, 4: 27.5}
# From a quality, the chance of each higher one; the rest leaves it unchanged. Normal's row adds up to 100.1%
# on the wiki, so it is scaled to 100%: a reroll from Normal always improves it.
REROLL_ODDS = {1: {2: .80, 3: .15, 4: .05, 5: .001}, 2: {3: .60, 4: .099, 5: .001}, 3: {4: .499, 5: .001}, 4: {5: .005}}


def reroll(value, start, target, scale=1.0):
    """Expected silver and number of rerolls to take an item of this value from quality start to target or better.
    Each quality's expectation counts the rerolls that leave it unchanged (dividing by the chance to move on) and
    what is left after the quality it moves to."""
    if start >= target:
        return {"silver": 0.0, "rolls": 0.0}
    silver, rolls = {}, {}
    for q in range(target - 1, start - 1, -1):
        odds = REROLL_ODDS[q]
        total = sum(odds.values())
        if total > 1:
            odds, total = {k: v / total for k, v in odds.items()}, 1.0
        below = {k: v for k, v in odds.items() if k < target}
        silver[q] = (value * REROLL_MODIFIER[q] * scale + sum(p * silver[k] for k, p in below.items())) / total
        rolls[q] = (1 + sum(p * rolls[k] for k, p in below.items())) / total
    return {"silver": silver[start], "rolls": rolls[start]}
