"""Conditional river alternatives with legal four-card witnesses and explicit prices."""

from collections import Counter

from .critique import Critique
from .postflop_value import bet_size
from .river_holdings import RANKS, river_holdings


def _example(pairs):
    return next((p.text for p in pairs), None)


def _turn_bet_called(snap, bettor, caller):
    actions = [a for a in snap.history if a.street == "Turn"]
    bets = [a for a in actions if a.action in {"bet", "raise"}]
    return len(bets) == 1 and bets[0].name == bettor and any(
        a.name == caller and a.action == "call" for a in actions
    )


def _value(snap, action, hero, opponent, result, facing):
    better = result.stronger_pair_count
    if (action == "check" and result.hero_category == 2 and result.stronger
            and all(p.category == 3 for p in result.stronger)
            and _turn_bet_called(snap, hero.name, opponent.name)):
        target = _example(p for p in result.weaker if p.category == 2)
        if target and min(hero.stack, opponent.stack) >= snap.bb:
            size = bet_size(snap, hero, [opponent], 33)
            return Critique(
                "river_check_back_top_two", "Checked back two pair after a turn call", "Consider a small value bet",
                f"{opponent.position} called your turn bet and checked the river. Only sets beat "
                f"your two pair. One lower two-pair holding is {target}. Compare your check with "
                f"a value bet around {size.amount / snap.bb:.1f} BB.",
                "Missed draws may make up much of the turn calling range. If lower two pair also "
                "folds or the opponent traps often, checking back can save a bet.",
            )
    # Limited extension for checked-back boats with a credible smaller boat.
    near_boat = action == "check" and result.hero_category == 6 and better <= 4
    if result.hero_category < 4 or (better > 1 and not near_boat):
        return None
    minimum = 6 if better > 1 else max(3, result.hero_category - 2)
    target = _example(p for p in result.weaker if p.category >= minimum)
    if not target:
        return None
    if better == 0:
        strength = "No legal four-card holding beats you."
    elif better == 1:
        a, b = result.stronger[0].cards
        strength = f"Every holding that beats you must contain both {a} and {b}."
    else:
        strength = (
            f"Only {better} available two-card combinations beat your full house. "
            f"One is {' '.join(result.stronger[0].cards)}."
        )
    if action == "fold":
        if not better:
            return None  # The established guide already flags folding the nuts.
        price = snap.to_call / (snap.pot + snap.to_call)
        return Critique(
            "river_fold_near_nuts", "Fold with just one beating hole-card pair", "Consider calling",
            f"{opponent.position} {facing} and you folded. {strength} A possible weaker value hand is "
            f"{target}. A call needs {price:.1%} equity before rake.",
            "A fold can save the call when this opponent bets almost exclusively the one stronger "
            "combination. Check their value range for this size and line.",
        )
    if action == "call":
        if not snap.can_raise or min(hero.stack - snap.to_call, opponent.stack) < snap.to_call:
            return None
        size = 2 * snap.to_call / snap.bb
        lead = f"You called {opponent.position}'s bet of {snap.to_call / snap.bb:g} BB. "
        alternative = f"A raise to {size:g} BB is the smallest full raise and can be matched."
        uid, title, label = "river_flat_value", "Value raise left unexplored", "Consider a value raise"
    else:
        size = bet_size(snap, hero, [opponent], 33)
        if size.contestable < snap.bb:
            return None
        lead = f"{opponent.position} checked and your check closed the betting. "
        alternative = f"A small value bet around {size.amount / snap.bb:.1f} BB tests a weaker caller."
        uid, title, label = "river_check_back_value", "Checked back a value hand", "Consider a value bet"
    caveat = (
        "If worse hands always fold, there is no added value from their calls. Extra rake can matter."
        if not better else
        "Compare worse hands that actually pay with stronger hands that continue or reraise. "
        "If callers are mostly stronger, keeping the pot smaller is defensible."
    )
    return Critique(uid, title, label, f"{lead}{strength} {alternative} A possible target is {target}.", caveat)


def _bluff_catch(snap, opponent, result, price, raised):
    made = result.hero_category
    bottom_straight = made == 4 and any(p.category == 4 for p in result.stronger) and all(
        p.category != 4 for p in result.weaker
    )
    suits = Counter(c[1] for c in snap.board)
    suit, count = suits.most_common(1)[0]
    if raised and (made <= 3 or bottom_straight):
        rule, title = "river_call_large_raise", "Costly call after a river raise"
        line = f"You bet, {opponent.position} raised and you called"
        if bottom_straight:
            line = "No lower straight is possible. " + line
    elif not raised and made in (2, 3) and count == 4 and all(c[1] != suit for c in snap.hero_cards):
        rule, title = "river_call_four_flush_board", "Large call without a flush-suit card"
        line = (
            f"The board contains four cards of the same suit. You hold none of that suit "
            f"to block a two-card flush. Facing {opponent.position}'s river bet, you called"
        )
    elif not raised and made == 5:
        if count != 3 or len({c[0] for c in snap.board}) != 5:
            return None
        highest = max(RANKS.index(c[0]) for c in snap.hero_cards if c[1] == suit)
        higher = [r+suit for r in RANKS[highest+1:] if r+suit not in snap.board]
        if len(higher) < 3 or not _turn_bet_called(snap, opponent.name, snap.hero):
            return None
        rule, title = "river_low_flush_second_bet", "Low flush facing another large bet"
        line = (
            f"You called {opponent.position}'s turn and river bets. Your best flush suit card "
            f"is {RANKS[highest]}{suit}, with {len(higher)} higher cards of that suit still available. "
            f"You called"
        )
    elif not raised and made in (1, 2) and _turn_bet_called(snap, opponent.name, snap.hero):
        ranks = Counter(RANKS.index(c[0]) + 2 for c in snap.board)
        shared_pair = any(ranks[r] == 2 for r in result.hero_kickers[:2])
        if not ((made == 1 and len(ranks) == 5) or (made == 2 and shared_pair and len(ranks) == 4)):
            return None
        if not (result.missed_flush or result.missed_straight):
            return None
        rule, title = "river_pair_second_barrel", "Pair-based bluff-catcher against two bets"
        hand = "one pair" if made == 1 else "two pair including the shared board pair"
        line = f"You called the turn bet. On the river you held {hand} and called {opponent.position}'s bet for"
    else:
        return None
    min_category = max(2, made) if rule == "river_pair_second_barrel" else max(4, made)
    value = _example(sorted(
        (p for p in result.stronger if p.category >= min_category), key=lambda p: p.category == 8
    ))
    # Prefer a credible weaker flush; otherwise show a holding whose actual value loses.
    below = tuple(p for p in result.weaker if made == 5 and p.category == 5)
    counter = _example(below + result.missed_flush + result.missed_straight + result.weaker)
    if not value or not counter:
        return None
    return Critique(
        rule, title, "Consider folding",
        f"{line} {snap.to_call / snap.bb:g} BB. A call needs {price:.1%} equity before rake. "
        f"One legal value holding is {value}. If only bluffs lose and there are no chops, "
        f"at least {price:.1%} of this betting range must be bluffs to break even.",
        f"You beat {counter}. A call can pay if enough bluffs or worse value hands take this "
        "line. These examples establish possible holdings, not their frequencies.",
    )


def _small_nut_bet(snap, event, hero, opponent, result):
    paid = min(opponent.stack, event.amount)
    if result.stronger_pair_count or result.hero_category < 4 or paid <= 0 or 5 * paid > snap.pot:
        return None
    if not (_turn_bet_called(snap, hero.name, opponent.name) or _turn_bet_called(snap, opponent.name, hero.name)):
        return None
    larger = bet_size(snap, hero, [opponent], 50).contestable
    if larger < max(2 * paid, snap.bb):
        return None
    target = _example(p for p in result.weaker if p.category >= max(3, result.hero_category - 2))
    if not target:
        return None
    return Critique(
        "river_small_nut_bet", "Small bet with the nuts after a contested turn", "Consider a larger value bet",
        f"{opponent.position} checked. Your bet risked {paid / snap.pot:.0%} pot in matched chips, "
        f"and no four-card hand beats you. One weaker target is {target}. A bet around {larger / snap.bb:.1f} BB "
        f"earns more if it gets over {paid / larger:.0%} as many calls from worse hands, assuming no raises, "
        "no chops and no extra rake.",
        "A small size may induce raises or collect many more calls from weak hands. "
        "Check which holdings still pay the larger size.",
    )


def _bluff(snap, event, opponent, result):
    # A large pure-bluff candidate after a turn call on a completed unpaired flush board.
    if result.hero_category > 1 or len({c[0] for c in snap.board}) != 5:
        return None
    suit = snap.board[-1][1]
    if sum(c[1] == suit for c in snap.board) != 3 or any(c[1] == suit for c in snap.hero_cards):
        return None
    if not _turn_bet_called(snap, snap.hero, opponent.name):
        return None
    effective = min(event.amount, opponent.stack)
    if effective < snap.pot * 2 / 3:
        return None
    target = _example(p for p in result.stronger if p.category == 5)
    if not target:
        return None
    folds = effective / (snap.pot + effective)
    return Critique(
        "river_large_bluff_unblocked_flush", "Large bluff on a flush-completing river", "Consider checking back",
        f"{opponent.position} called the turn and checked the river. You bet {event.amount / snap.bb:g} BB "
        f"with no card of the completed flush suit. {target} can call. If every call beats you, "
        f"you need {folds:.1%} folds before rake just to break even on the bet.",
        "A turn caller may have enough missed draws or one-pair hands that fold. Check how often "
        "better hands fold to this size and what your hand wins by checking back.",
    )


def review_river(snap, event, advice):
    """Challenge a particular recorded decision using only information available when it was taken."""
    if (snap.street != "River" or not snap.hero_cards or len(snap.hero_cards) != 4
            or len(snap.board) != 5 or snap.bb <= 0 or snap.pot <= 0):
        return None
    live = [s for s in snap.seats if s.live]
    if len(live) != 2 or snap.to_call != snap.price_to_match:
        return None
    hero = next((s for s in live if s.is_hero), None)
    if not hero:
        return None
    opponent = next(s for s in live if not s.is_hero)
    actions = [a for a in snap.history if a.street == "River"]
    if any(a.name not in {hero.name, opponent.name} for a in actions):
        return None
    action = event.action
    if advice and advice.title == "River · opponent history":
        if len(advice.choices) != 1 or {action, advice.choices[0]} != {"call", "fold"}:
            return None
        alternative = advice.choices[0]
        return Critique(
            "river_" + action + "_against_prior_bets", "River decision against earlier bets",
            "Consider calling" if alternative == "call" else "Consider folding", advice.why,
            "The bounds come from previous comparable bets and broad showdown categories. "
            "Different blockers or a change in this opponent's betting strategy can change the decision.",
        )
    if advice and advice.title == "River · bluff supported by fold history":
        if action != "check" or advice.choices != ("bet",):
            return None
        return Critique(
            "river_check_against_prior_folds", "Checked against a frequent folder", "Consider bluffing",
            advice.why, "A check can win against missed draws. A different river or sizing can also "
            "reduce this opponent's fold frequency.",
        )
    line = [(a.name, a.action) for a in actions]
    checked_to = line == [(opponent.name, "check")]
    opening_bet = line in (
        [(opponent.name, "bet")], [(hero.name, "check"), (opponent.name, "bet")],
    )
    raised = line in (
        [(hero.name, "bet"), (opponent.name, "raise")],
        [(opponent.name, "check"), (hero.name, "bet"), (opponent.name, "raise")],
    )
    if checked_to and action in ("check", "bet") and not snap.to_call and opponent.stack > 0:
        result = river_holdings(snap.hero_cards, snap.board)
        if action == "bet":
            if not result.weaker and result.ties and result.stronger:
                risk = min(event.amount, hero.stack, opponent.stack)
                if risk > 0:
                    return Critique(
                        "river_split_only_bluff", "Bet after a check with only split-pot showdown value",
                        "Compare the bluff with checking back",
                        f"You risked {risk / snap.bb:g} BB into {snap.pot / snap.bb:g} BB in matched chips. "
                        f"If every opponent hand beats you, a pure bluff needs {risk / (snap.pot + risk):.2%} folds "
                        "before rake. A fold from a better hand gains the full pot over checking. "
                        "Folding out a tying hand gains half the pot. A call from a tying hand adds no value before rake.",
                        "Checking takes your share against ties. A bluff can gain when enough better or tying hands fold.",
                    )
            return _small_nut_bet(snap, event, hero, opponent, result) or _bluff(snap, event, opponent, result)
        return _value(snap, action, hero, opponent, result, "")
    if action not in ("fold", "call") or not (opening_bet or raised) or snap.to_call <= 0:
        return None
    result = river_holdings(snap.hero_cards, snap.board)
    if action == "fold" or opening_bet:
        value = _value(snap, action, hero, opponent, result, "raised" if raised else "bet")
        if value:
            return value
    price = snap.to_call / (snap.pot + snap.to_call)
    if action == "call" and price >= (0.25 if raised else 0.30):
        return _bluff_catch(snap, opponent, result, price, raised)
    return None
