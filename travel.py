"""Teleport (fast travel) cost of carrying items between cities. Read-only: it never talks to the game.

The game charges per item stack: 150 silver x weight (kg) x the item's fasttravelfactor x amount x a server-wide
multiplier, rounded up, times the route distance. Weight and fasttravelfactor come from the game's item data
(gamedata.py; a missing factor is 1, as for equipment and souls). The formula and distances are from Albion Free
Market's reverse-engineered article (checked 2026-11-07), not official, so the multiplier is a setting the player
confirms against the in-game travel planner.
"""
from __future__ import annotations
import gamedata

SILVER_PER_KG = 150
# The royal city ring, in order: neighbours are distance 1, any other pair 2.
# Thetford, Fort Sterling, Lymhurst, Bridgewatch, Martlock.
RING = ("7", "4002", "1002", "2004", "3008")
BRECILIEN = "5003"


def distance(source, dest):
    """Route distance between two city ids, or None when the travel planner has no such route (Caerleon)."""
    if source == dest:
        return 0
    if BRECILIEN in (source, dest):
        return 2 if {source, dest} - {BRECILIEN} <= set(RING) else None
    if source not in RING or dest not in RING:
        return None
    gap = abs(RING.index(source) - RING.index(dest))
    return 1 if gap in (1, len(RING) - 1) else 2


def unit_cost(item, source, dest, multiplier, items=gamedata.catalog):
    """Silver per unit to teleport an item from one city to another, before the per-stack rounding up
    (under one silver per stack). None when the item's weight or the route is unknown."""
    d, data = distance(source, dest), items.get(item)
    if d is None or data is None:
        return None
    return SILVER_PER_KG * data["w"] * data["f"] * multiplier * d
