"""Heads-up turn study lines on matched runouts, measured from the current decision."""

from collections import defaultdict

from .early_sizing import CHECK_MARGIN


def river_branch(rows, band_response, pot, stack, bb, lead_rates):
    """Return branch probability and its probability-weighted chip return.

    Hero sees the river card, price and action. All still-possible opponent
    holdings for that river are pooled before choosing to call or fold.
    """
    checked = mass = 0.0
    facing = defaultdict(lambda: [0.0, 0.0])
    bet = min(stack, pot, max(bb, round(pot / 2)))
    for card, band, strong, weight, share in rows:
        response = band_response[band]
        mass += response * weight
        leads = lead_rates[strong] if bet > 0 else 0
        checked += response * (1 - leads) * share * pot
        if leads:
            row = facing[card]
            row[0] += response * leads * weight
            row[1] += response * leads * share
    called = sum(
        max(0.0, share * (pot + 2 * bet) - weight * bet)
        for weight, share in facing.values()
    )
    return mass, checked + called


def turn_sensitivity(snap, hero, opponent, point, sizes, rates, facing):
    rows = point.river_mass
    if not rows:
        return ()
    pot, bb = snap.pot, snap.bb
    effective = min(hero.stack, opponent.stack)
    plans = [
        ("Check-down river", (0, 0), False),
        ("Value-heavy river bets", (0.08, 0.65), False),
        ("Wider river bets", (0.33, 0.65), False),
    ]
    lines = [
        "WHAT WOULD CHANGE THIS DECISION?",
        (
            "Same turn range and all 40 legal rivers per sampled holding. "
            "River bets are half pot, capped by the effective stack. Hero calls or folds using the range on the visible river. "
            "After a river check both players check through. No rake."
        ),
    ]
    picks = []
    if facing:
        price = snap.to_call
        matched_pot = pot + 2 * price - snap.price_to_match
        remaining = min(hero.stack - price, opponent.stack)
        lines.append(
            f"Call {price / bb:g} BB or fold. Matched pot after calling {matched_pot / bb:g} BB. "
            f"Effective stack left {remaining / bb:g} BB. The comparison omits a turn raise."
        )
    else:
        plans.append(("More turn check-raises", (0.08, 0.65), True))
        lines.append(
            "Compare checking back, betting and folding to a raise, or betting and calling it. "
            "Opponent raises to three times the bet, capped by the effective stack. "
            "The extra check-raise scenario turns one in five modeled turn calls into a raise."
        )
    for label, leads, extra_raises in plans:
        if facing:
            _, value = river_branch(rows, (1,) * 5, matched_pot, remaining, bb, leads)
            gain = value - price
            pick = "call" if gain > 0 else "fold"
            detail = f"call vs fold {gain / bb:+.2f} BB"
        else:
            _, check = river_branch(rows, (1,) * 5, pot, effective, bb, leads)
            options = []
            for amount in sorted({min(effective, s) for s in sizes}):
                fold, call, raise_ = zip(
                    *rates(amount / pot, 0.0, amount < effective, True)
                )
                if extra_raises and amount < effective:
                    raise_ = tuple(r + c / 5 for r, c in zip(raise_, call))
                    call = tuple(0.8 * c for c in call)
                fold_mass = sum(w * fold[band] for _, band, _, w, _ in rows)
                call_mass, call_return = river_branch(
                    rows, call, pot + 2 * amount, effective - amount, bb, leads
                )
                raised_to = min(3 * amount, effective)
                raise_mass, raise_return = river_branch(
                    rows, raise_, pot + 2 * raised_to, effective - raised_to, bb, leads
                )
                bet_fold = (
                    fold_mass * pot
                    + call_return
                    - (call_mass + raise_mass) * amount
                    - check
                )
                bet_call = bet_fold + raise_return - raise_mass * (raised_to - amount)
                options.append(
                    (
                        max(bet_fold, bet_call),
                        amount,
                        bet_fold,
                        bet_call,
                        raised_to,
                        raise_mass,
                    )
                )
            gain, size, bet_fold, bet_call, to, raise_mass = max(
                options, key=lambda v: v[0]
            )
            pick = f"bet {size / pot:.0%} pot" if gain > CHECK_MARGIN * pot else "check"
            detail = f"best bet size {size / bb:g} BB vs check"
            if raise_mass:
                if pick.startswith("bet"):
                    pick += (
                        " / call raise" if bet_call > bet_fold else " / fold to raise"
                    )
                detail += (
                    f" · bet/fold {bet_fold / bb:+.2f} BB · bet/call {bet_call / bb:+.2f} BB "
                    f"against a raise to {to / bb:g} BB (call {(to - size) / bb:g} more, raises {raise_mass:.0%})"
                )
            else:
                detail += f" · matched all-in {gain / bb:+.2f} BB"
        picks.append(pick)
        lines.append(
            f"{label} ({leads[1]:.0%} of straights or better / {leads[0]:.0%} of other hands bet) · {pick} · {detail}."
        )
    if len(set(picks)) > 1:
        lines.append(
            "The preferred line changes across these assumptions. Review the opponent's raising and river betting ranges."
        )
    else:
        lines.append("The same turn line leads in the scenarios shown.")
    lines.append(
        "These response rates are examples. The displayed chips compare modeled lines only."
    )
    return tuple(lines)
