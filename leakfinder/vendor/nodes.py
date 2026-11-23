"""Adapted from population.extract_required_pool_records, retaining source events."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from ..engine import Event
AGGRO_ACTIONS = {"bet", "raise"}


def first_action_by_any(
    events: List[Event], players: List[str], after_idx: int = -1
) -> Optional[Event]:
    allowed = set(players)
    for ev in events:
        if ev.idx <= after_idx:
            continue
        if ev.player in allowed:
            return ev
    return None


def next_action_by_player(
    events: List[Event], player: str, after_idx: int
) -> Optional[Event]:
    for ev in events:
        if ev.idx > after_idx and ev.player == player:
            return ev
    return None


def postflop(h):
    recs = []
    pfa = h.last_preflop_raiser
    has_pfa = bool(pfa) and h.pot_type in {"srp", "3bet", "4bet+"}

    flop_events = h.events.get("FLOP", [])
    turn_events = h.events.get("TURN", [])
    river_events = h.events.get("RIVER", [])
    if not flop_events:
        return recs

    flop_order = h.active_players("FLOP")
    turn_order = h.active_players("TURN")
    river_order = h.active_players("RIVER")

    def other_player(order: List[str], player: str) -> str:
        for x in order:
            if x != player:
                return x
        return ""

    def pfa_flop_cbet_and_defender_call() -> Tuple[
        Optional[str], Optional[Event], Optional[Event]
    ]:
        """Return defender, PFA flop c-bet, defender call for clean HU flop c-bet/call nodes."""
        if not has_pfa or len(flop_order) != 2 or pfa not in flop_order:
            return None, None, None
        defender = other_player(flop_order, pfa)
        if not defender:
            return None, None, None

        if flop_order[0] == pfa:  # PFA OOP acts first.
            pfa_bet = first_action_by_any(flop_events, [pfa])
            if not pfa_bet or pfa_bet.action not in AGGRO_ACTIONS:
                return defender, pfa_bet, None
            def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
            if def_resp and def_resp.action == "call":
                return defender, pfa_bet, def_resp
            return defender, pfa_bet, None

        # PFA IP: OOP defender must check, then PFA c-bets.
        def_first = first_action_by_any(flop_events, [defender])
        if not def_first or def_first.action != "check":
            return defender, None, None
        pfa_bet = next_action_by_player(flop_events, pfa, def_first.idx)
        if not pfa_bet or pfa_bet.action not in AGGRO_ACTIONS:
            return defender, pfa_bet, None
        def_resp = next_action_by_player(flop_events, defender, pfa_bet.idx)
        if def_resp and def_resp.action == "call":
            return defender, pfa_bet, def_resp
        return defender, pfa_bet, None

    def pfa_turn_action_after_flop_call(defender: str) -> Tuple[Optional[Event], bool]:
        """Return PFA turn action and whether PFA had a clean c-bet/barrel opportunity."""
        if (
            not defender
            or len(turn_order) != 2
            or pfa not in turn_order
            or defender not in turn_order
        ):
            return None, False
        if turn_order[0] == pfa:  # PFA OOP acts first.
            ev = first_action_by_any(turn_events, [pfa])
            return ev, ev is not None
        # PFA IP: defender must check first.
        def_first = first_action_by_any(turn_events, [defender])
        if not def_first or def_first.action != "check":
            return None, False
        ev = next_action_by_player(turn_events, pfa, def_first.idx)
        return ev, ev is not None

    def pfa_river_action_after_turn_call(defender: str) -> Tuple[Optional[Event], bool]:
        """Return PFA river action and whether PFA had a clean river barrel opportunity."""
        if (
            not defender
            or len(river_order) != 2
            or pfa not in river_order
            or defender not in river_order
        ):
            return None, False
        if river_order[0] == pfa:  # PFA OOP acts first.
            ev = first_action_by_any(river_events, [pfa])
            return ev, ev is not None
        # PFA IP: defender must check first.
        def_first = first_action_by_any(river_events, [defender])
        if not def_first or def_first.action != "check":
            return None, False
        ev = next_action_by_player(river_events, pfa, def_first.idx)
        return ev, ev is not None

    # B) Existing: Bet IP vs missed flop c-bet + NEW OOP response vs that stab.
    # PFA is OOP on flop, checks first, IP player decides bet/check. If IP bets, OOP responds fold/call/xr.
    if has_pfa and len(flop_order) == 2 and pfa in flop_order and flop_order[-1] != pfa:
        pfa_flop_first = first_action_by_any(flop_events, [pfa])
        ip_player = flop_order[-1]
        if pfa_flop_first and pfa_flop_first.action == "check":
            ip_action = next_action_by_player(
                flop_events, ip_player, pfa_flop_first.idx
            )
            if ip_action:
                recs.append(("BET_IP_VS_MISSED_FLOP_CB", ip_action, pfa))
            if ip_action and ip_action.action in AGGRO_ACTIONS:
                oop_resp = next_action_by_player(flop_events, pfa, ip_action.idx)
                if oop_resp:
                    recs.append(
                        (
                            "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB",
                            oop_resp,
                            ip_player,
                        )
                    )

    # C) Existing: Bet IP vs missed turn c-bet + NEW OOP response vs turn stab.
    # PFA OOP c-bets flop, gets called, then checks turn to IP.
    if (
        has_pfa
        and len(flop_order) == 2
        and len(turn_order) == 2
        and pfa in flop_order
        and pfa in turn_order
        and flop_order[-1] != pfa
        and turn_order[-1] != pfa
    ):
        ip_player = flop_order[-1]
        pfa_flop_first = first_action_by_any(flop_events, [pfa])
        if pfa_flop_first and pfa_flop_first.action in AGGRO_ACTIONS:
            ip_flop_resp = next_action_by_player(
                flop_events, ip_player, pfa_flop_first.idx
            )
            if ip_flop_resp and ip_flop_resp.action == "call":
                pfa_turn_first = first_action_by_any(turn_events, [pfa])
                if pfa_turn_first and pfa_turn_first.action == "check":
                    ip_turn_action = next_action_by_player(
                        turn_events, ip_player, pfa_turn_first.idx
                    )
                    if ip_turn_action:
                        recs.append(("BET_IP_VS_MISSED_TURN_CB", ip_turn_action, pfa))
                    if ip_turn_action and ip_turn_action.action in AGGRO_ACTIONS:
                        oop_turn_resp = next_action_by_player(
                            turn_events, pfa, ip_turn_action.idx
                        )
                        if oop_turn_resp:
                            recs.append(
                                (
                                    "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB",
                                    oop_turn_resp,
                                    ip_player,
                                )
                            )

    # D) Turn probe after flop checkback + fold/call/raise vs turn probe.
    # PFA IP checks back flop after OOP check. OOP gets turn probe opportunity. If OOP bets, PFA response is fold/call/raise.
    if (
        has_pfa
        and len(flop_order) == 2
        and len(turn_order) == 2
        and pfa in flop_order
        and pfa in turn_order
        and flop_order[-1] == pfa
        and turn_order[-1] == pfa
    ):
        oop_player = flop_order[0]
        oop_flop_first = first_action_by_any(flop_events, [oop_player])
        if oop_flop_first and oop_flop_first.action == "check":
            pfa_flop_action = next_action_by_player(
                flop_events, pfa, oop_flop_first.idx
            )
            if pfa_flop_action and pfa_flop_action.action == "check":
                oop_turn_first = first_action_by_any(turn_events, [oop_player])
                if oop_turn_first:
                    recs.append(
                        ("TURN_PROBE_AFTER_FLOP_CHECKBACK", oop_turn_first, pfa)
                    )
                if oop_turn_first and oop_turn_first.action in AGGRO_ACTIONS:
                    pfa_response = next_action_by_player(
                        turn_events, pfa, oop_turn_first.idx
                    )
                    if pfa_response:
                        recs.append(("FOLD_TO_TURN_PROBE", pfa_response, oop_player))

    # E) PFA turn barrel/give-up after flop c-bet called; and defender fold/call/raise vs turn barrel.
    defender, flop_cbet, flop_call = pfa_flop_cbet_and_defender_call()
    if has_pfa and defender and flop_cbet and flop_call:
        pfa_turn_ev, pfa_turn_opportunity = pfa_turn_action_after_flop_call(defender)
        if pfa_turn_opportunity and pfa_turn_ev:
            recs.append(
                ("PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED", pfa_turn_ev, defender)
            )
        if pfa_turn_ev and pfa_turn_ev.action in AGGRO_ACTIONS:
            def_turn_resp = next_action_by_player(
                turn_events, defender, pfa_turn_ev.idx
            )
            if def_turn_resp:
                recs.append(("FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL", def_turn_resp, pfa))
            if def_turn_resp and def_turn_resp.action == "call":
                pfa_river_ev, pfa_river_opportunity = pfa_river_action_after_turn_call(
                    defender
                )
                if (
                    pfa_river_opportunity
                    and pfa_river_ev
                    and pfa_river_ev.action in AGGRO_ACTIONS
                ):
                    def_river_resp = next_action_by_player(
                        river_events, defender, pfa_river_ev.idx
                    )
                    if def_river_resp:
                        recs.append(
                            (
                                "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL",
                                def_river_resp,
                                pfa,
                            )
                        )
        elif pfa_turn_ev and pfa_turn_ev.action == "check":
            # PFA bet flop, got called, checks turn; if defender bets, count PFA response.
            def_turn_bet = (
                next_action_by_player(turn_events, defender, pfa_turn_ev.idx)
                if turn_order and turn_order[0] == pfa
                else None
            )
            # If PFA is IP, his check comes after defender checked, so no defender stab can happen after PFA check on same street.
            if def_turn_bet and def_turn_bet.action in AGGRO_ACTIONS:
                pfa_resp = next_action_by_player(turn_events, pfa, def_turn_bet.idx)
                if pfa_resp:
                    recs.append(("PFA_RESPONSE_AFTER_TURN_GIVEUP", pfa_resp, defender))

    return recs
