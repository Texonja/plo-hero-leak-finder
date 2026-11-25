"""Compare the local guide, walk-forward evidence and authored range scenarios."""

from dataclasses import dataclass

from .analysis import wilson
from .early_range import early_ranges
from .early_sizing import CHECK_MARGIN, historical_estimate, supported_sizing
from .opponent_history import (
    MIN_CASES,
    MIN_DATES,
    apply_history,
    betting_limits,
    current_categories,
    heads_up_price,
    postflop_opponent,
    responses,
    river_opponent,
)
from .opponent_range import condition_range
from .postflop_value import bet_size
from .river_holdings import river_holdings
from .statistics import sizing
from .strategy_guide import advice_at, nuts
from .turn_lines import turn_sensitivity
from .vendor.primitives import best_plo_hand

MODEL_INTENSITY = (0.75, 1.0, 1.25)
RESPONSE_SCENARIOS = (
    ("More calls", -0.15),
    ("Middle scenario", 0.0),
    ("More folds", 0.15),
)


@dataclass(frozen=True)
class RangeReview:
    text: str
    sources: tuple
    guide: str
    history: str
    models: tuple[str, ...]
    matched_sources: frozenset = frozenset()
    comparison: tuple[str, str] = ()
    historical_comparison: tuple[str, str] = ()


def _response_rates(size, calling, can_raise, early):
    # Named scenarios vary willingness to call with weak and marginal hands.
    fold_at_half = (
        (0.87, 0.56, 0.22, 0.07, 0.01) if early else (0.96, 0.74, 0.38, 0.11, 0.01)
    )
    raise_rate = (
        (0.03, 0.03, 0.09, 0.18, 0.48) if early else (0.01, 0.01, 0.04, 0.15, 0.58)
    )
    slopes = (0.08, 0.24, 0.30, 0.16, 0.0)
    for band in range(len(fold_at_half)):
        folds = fold_at_half[band] + slopes[band] * (size - 0.5)
        if band <= 2:
            folds += calling
        raises = raise_rate[band] if can_raise else 0.0
        folds = max(0.0, min(1.0 - raises, folds))
        yield folds, 1.0 - raises - folds, raises


def _bet_ev(joint, pot, amount, calling, can_raise, *, early=False):
    """Heads-up bet/fold with opponent responses based on their own strength."""
    gains = 0.0
    rates = _response_rates(amount / pot, calling, can_raise, early)
    for relation, (folds, calls, raises) in zip(joint, rates, strict=True):
        for weight, eq in zip(relation, (1.0, 0.5, 0.0), strict=True):
            gains += weight * (
                folds * pot
                + calls * (eq * (pot + 2 * amount) - amount)
                - raises * amount
            )
    return gains


def _call_equity(point, size, calling):
    rates = _response_rates(size, calling, True, True)
    wins = total = 0.0
    for (win, tie, loss), (_, calls, _) in zip(point.by_strength, rates, strict=True):
        wins += (win + tie / 2) * calls
        total += (win + tie + loss) * calls
    return wins / total if total else point.equity


def _sizing_history(snap, point, baseline, responses_by_size):
    lines = []
    ready = []
    reported_groups = set()
    for size, sample in responses_by_size.items():
        group = sizing(size / snap.pot)
        folds, calls, raises = responses(sample)
        n = folds + calls + raises
        if group not in reported_groups:
            counts = f"{n}/{sample.broader} past opportunities · {sample.days} dates · fold {folds} · call {calls} · raise {raises}"
            interval = ""
            if n:
                lo, hi = wilson(folds / n, n)
                interval = f" · fold interval {lo:.0%}–{hi:.0%}"
            lines.append(f"Size group {group} · {counts}{interval}.")
            reported_groups.add(group)
        if not n:
            continue
        equity = tuple(
            _call_equity(point, size / snap.pot, c) for _, c in RESPONSE_SCENARIOS
        )
        estimate = historical_estimate(
            snap.pot, size, point.equity * snap.pot, equity, sample
        )
        result = "meets the sample threshold" if sample.ready else "exploratory sample"
        lines.append(
            f"  {size / snap.pot:.0%} pot · {result} · vs check {estimate.gain / snap.bb:+.2f} BB"
            f" · model range {estimate.low / snap.bb:+.2f} to {estimate.high / snap.bb:+.2f} BB."
        )
        if sample.ready:
            ready.append(estimate)
    if not ready:
        return (
            baseline,
            "Too few comparable responses to change the recommendation.",
            lines,
        )
    chosen, reason = supported_sizing(snap.pot, baseline, ready)
    return chosen, reason, lines


def _play(advice):
    return advice.play if advice and advice.play else "manual review"


def _early_review(snap, hero, opponent, history):
    base = _play(advice_at(snap))
    timeline = early_ranges(snap, opponent)
    if not timeline:
        return RangeReview(
            "This view requires four known Hero cards and a valid board.",
            (),
            base,
            base,
            (),
        )
    current = timeline[-1]
    actions = [a for a in snap.history if a.street == snap.street]
    facing = bool(snap.to_call)
    last = next((a for a in reversed(actions) if a.action in {"bet", "raise"}), None)
    heads_up = postflop_opponent(snap) is not None and not any(
        a.action == "fold" for a in actions
    )
    checked_to_hero = bool(
        len(actions) == 1
        and actions[0].name == opponent.name
        and actions[0].action == "check"
    )
    amounts = (
        tuple(
            dict.fromkeys(
                bet_size(snap, hero, [opponent], p).amount for p in (33, 50, 67, 85)
            )
        )
        if snap.can_raise and not facing and opponent.stack > 0
        else ()
    )
    fit_call = heads_up and facing and last is not None and last.name == opponent.name
    fit_bet = heads_up and checked_to_hero and not facing and bool(amounts)
    exclude_bet_fold = False
    if fit_bet and any(s < min(hero.stack, opponent.stack) for s in amounts):
        rank, kickers, *_ = best_plo_hand(snap.hero_cards, snap.board)
        exclude_bet_fold = rank >= 3 and nuts(
            snap.hero_cards, snap.board, rank, tuple(kickers)
        )
        if exclude_bet_fold:
            fit_bet = False
    price = heads_up_price(snap) if fit_call else None

    def choice(point, calling=0.0):
        if fit_call:
            return ("call" if point.equity > price else "fold"), ()
        if fit_bet:
            check = point.equity * snap.pot
            ev = tuple(
                (
                    s,
                    _bet_ev(
                        point.by_strength,
                        snap.pot,
                        min(s, opponent.stack),
                        calling,
                        s < min(hero.stack, opponent.stack),
                        early=True,
                    )
                    - check,
                )
                for s in amounts
            )
            size, gain = max(ev, key=lambda pair: pair[1])
            # Keep the resolution of the sizing grid visible when near even.
            move = (
                f"bet {size / snap.pot:.0%}"
                if gain > CHECK_MARGIN * snap.pot
                else "check"
            )
            return move, ev
        return "", ()

    lines = [
        f"{snap.room} #{snap.number} · {snap.ref} · {snap.street} · {opponent.position} ({opponent.name})",
        f"Guide · {base}",
        "",
        "RANGE AFTER EACH ACTION · 768 sampled PLO4 holdings per street",
    ]
    hist_comparison = ()
    responses_by_size = {}
    history_lines = []
    if fit_call or fit_bet:
        before = choice(current.uniform)[0]
        after, ev = choice(current.steps[-1])
        lines.append(
            f"Checkdown model on the current board · uniform range → action-weighted range · {before} → {after}"
        )
        if fit_bet:
            effective = min(hero.stack, opponent.stack)
            responses_by_size = {
                s: history.sample(snap, s) for s in amounts if s < effective
            }
            baseline = 0 if after == "check" else max(ev, key=lambda pair: pair[1])[0]
            picked, reason, history_lines = _sizing_history(
                snap, current.steps[-1], baseline, responses_by_size
            )
            if len(responses_by_size) < len(amounts):
                history_lines.append(
                    "The personal comparison excludes all-in sizes because they have a different response context."
                )
            picked_label = f"bet {picked / snap.pot:.0%}" if picked else "check"
            if picked and picked != baseline:
                group = sizing(picked / snap.pot)
                if sum(sizing(s / snap.pot) == group for s in responses_by_size) > 1:
                    picked_label = f"bet in the {group} group"
            hist_comparison = (after, picked_label)
            lines.extend(
                (
                    f"Bet/fold scenario with no later betting · Model recommendation → recommendation with history · {after} → {picked_label}",
                    reason,
                )
            )
    else:
        before = after = ""
    if snap.street == "Turn" and (fit_call or fit_bet):
        lines[2:2] = [
            "",
            *turn_sensitivity(
                snap,
                hero,
                opponent,
                current.steps[-1],
                amounts,
                _response_rates,
                fit_call,
            ),
            "",
        ]
    for stage in timeline:
        board = " ".join(stage.board) or "before the flop"
        lines.append(f"{stage.street} · {board} · {stage.legal_total:,} legal holdings")
        for i, step in enumerate(stage.steps):
            group = " · ".join(
                f"{label} {share:.0%}"
                for label, share in step.classes
                if share >= 0.005
            )
            lines.append(
                f"  {step.action} · effective sample {step.effective_n:.0f} · {group}"
            )
            if step.equity is not None:
                decision = choice(step)[0] if stage.street == snap.street else ""
                change = (
                    f" ({(step.equity - stage.steps[i - 1].equity) * 100:+.1f} pp)"
                    if i
                    else ""
                )
                lines.append(
                    f"    Showdown equity {step.equity:.1%}{change} · share of draws {step.draws:.0%}"
                    + (
                        f" · model choice at the current pot and price {decision}"
                        if decision
                        else ""
                    )
                )
            if step.rule:
                lines.append(
                    f"    {step.rule} Example holding · {step.example}"
                    if step.example
                    else f"    {step.rule}"
                )

    lines.extend(("", f"SCENARIOS FOR {snap.street.upper()}"))
    models = []
    if fit_call:
        lines.append(
            f"Call costs {snap.to_call / snap.bb:g} BB · threshold {price:.1%} if the remaining streets go to showdown without further betting."
        )
        for intensity in MODEL_INTENSITY:
            stage = (
                current
                if intensity == 1
                else early_ranges(
                    snap, opponent, intensity=intensity, current_only=True
                )[0]
            )
            start, end = stage.steps[0], stage.steps[-1]
            pick = choice(end)[0]
            models.append(pick)
            lines.append(
                f"Action weight {intensity:g}× · {start.equity:.1%} before this street's observed actions → {end.equity:.1%} after · {pick}"
            )
        margin = (current.steps[-1].equity - price) * 100
        lines.append(f"Base scenario relative to the call threshold · {margin:+.1f} pp")
    elif fit_bet:
        for name, calling in RESPONSE_SCENARIOS:
            pick, ev = choice(current.steps[-1], calling)
            models.append(pick)
            values = " · ".join(
                f"{s / snap.pot:.0%} pot ({s / snap.bb:g} BB) {gain / snap.bb:+.2f} BB"
                for s, gain in ev
            )
            lines.append(f"{name} · {pick} · {values}")
            if calling == 0.0:
                best = sorted([0.0, *(gain for _, gain in ev)], reverse=True)
                gap = (best[0] - best[1]) / snap.bb
                lines.append(
                    f"Model gap between the two leading options · {gap:.2f} BB."
                )
        lines.append(
            "Values are relative to checking back. A bet needs an edge above 2% of the pot. Uncalled chips are returned to Hero. Hero folds to a raise. After a call, the hand goes to showdown without further betting. See the guide for lines that continue against a raise."
        )
    elif exclude_bet_fold:
        lines.append(
            "Hero holds the current nuts. Comparing bet sizes here requires a policy for continuing against raises. The bet/fold sizing model assumes Hero always surrenders the pot to a raise."
        )
    else:
        lines.append(
            "The sizing comparison requires a heads-up check to Hero. Other spots show the selected opponent's range and equity, alongside the guide."
        )
    lines.append(
        f"The model uses {current.runouts_per_hand} runouts per holding on this street. The flop samples turn and river pairs. The turn checks all 40 remaining river cards. This scenario excludes rake and further betting. The weights are modeling assumptions."
    )

    lines.extend(("", "EARLIER HANDS AGAINST THIS OPPONENT"))
    matched, sources = {}, {}
    if not heads_up:
        lines.append("Past samples in this view cover heads-up hands against you.")
    elif facing:
        sample = history.sample(snap)
        lines.append(
            f"{len(sample.comparable)} comparable out of {sample.broader} earlier opening bets on this street. {sample.days} dates. Raises belong to a separate context."
        )
        matched.update(((r.hand, r.ref), r) for r in sample.comparable)
        sources.update(
            ((r.hand, r.ref), r)
            for r in sorted(sample.previous, key=lambda r: r.end, reverse=True)[:12]
        )
        lines.append(
            "Folds, calls and raises against your bets describe a different decision. This call estimate uses the opponent's range and bet price."
        )
    else:
        lines.extend(history_lines)
        for sample in responses_by_size.values():
            matched.update(((r.hand, r.ref), r) for r in sample.comparable)
            sources.update(
                ((r.hand, r.ref), r)
                for r in sorted(sample.previous, key=lambda r: r.end, reverse=True)[:12]
            )
    sources.update(matched)
    lines.append(
        "History uses completed earlier hands or sessions against the same opponent on the same network."
    )
    if fit_bet:
        lines.append(
            f"Changing the recommendation requires at least {MIN_CASES} comparable responses per size group across {MIN_DATES} dates. "
            "The comparison matches stakes and antes, preflop role and raise count, position, board texture, stack depth and prior action. "
            "A fold wins the pot. A call goes to showdown against a modeled calling range. A raise costs Hero the bet in this bet/fold scenario. "
            "The range combines Wilson intervals for folding and raising after continuing with three assumptions about the calling range. Actions from the same session can be correlated. "
            "Sizes in the same history group share response counts. Each scenario uses its own bet amount."
        )
    links = tuple(
        sorted(
            sources.values(),
            key=lambda r: ((r.hand, r.ref) in matched, r.end),
            reverse=True,
        )
    )
    return RangeReview(
        "\n".join(lines),
        links,
        base,
        hist_comparison[-1] if hist_comparison else base,
        tuple(models),
        frozenset(matched),
        (before, after),
        hist_comparison,
    )


def river_price_note(snap, history):
    price = heads_up_price(snap)
    sample = history.sample(snap)
    n = len(sample.comparable)
    known = sum(r.category is not None for r in sample.comparable)
    note = f"Call {snap.to_call / snap.bb:g} BB. Break-even showdown equity {price:.1%} before rake."
    holdings = river_holdings(snap.hero_cards, snap.board)
    if not holdings.weaker and holdings.stronger:
        note += (
            f" Hero can only tie or lose. A heads-up call needs at least {2 * price:.1%} splits."
            if holdings.ties else " Every legal opposing holding beats Hero at showdown."
        )
    elif holdings.stronger:
        note += f" A pure bluff-catcher needs {price:.1%} bluffs if it loses to all value, beats all bluffs and never ties."
    if any(a.street == "River" and a.action == "raise" for a in snap.history):
        return (
            note
            + " Earlier opening bets cannot establish this opponent's river raising frequency."
        )
    return (
        note
        + f" Earlier matched bets: {n}, with {known} shown and {n - known} unknown. "
        "A mucked hand could be value or a bluff."
    )


def _river_pivot(snap, stage, history):
    price = heads_up_price(snap)
    equity = stage.weaker + stage.tied / 2
    lines = [
        "WHAT WOULD CHANGE THIS DECISION?",
        river_price_note(snap, history),
        (
            f"Modeled betting range: Hero beats {stage.weaker:.1%}, ties {stage.tied:.1%} and loses to {stage.stronger:.1%}. "
            f"Equity {equity:.1%}, {abs(equity - price) * 100:.1f} percentage points {'above' if equity >= price else 'below'} the call price."
        ),
    ]
    needed = price * (stage.stronger + stage.tied) - stage.tied / 2
    if stage.stronger + stage.tied == 0:
        lines.append(
            "Hero beats every holding in this sample. Test a range with more strong value hands."
        )
    elif needed <= 0:
        lines.append(
            "At this price the modeled ties cover the call even if weaker hands never bet."
        )
    elif stage.weaker > 0 and 0 < price < 1:
        threshold = needed / ((1 - price) * stage.weaker)
        lines.append(
            f"Holding stronger hands and ties fixed, call breaks even when holdings Hero beats bet {threshold:.2f}× "
            "their modeled weight. They include weaker value bets as well as bluffs."
        )
    for key, label in (
        ("value", "Stronger value"),
        ("weak_call", "Weaker made hand"),
        ("bluff", "Possible bluff"),
    ):
        example = stage.examples.get(key)
        if example:
            lines.append(f"{label} · {example.text}.")
    lines.extend(river_holdings(snap.hero_cards, snap.board).blockers)
    return lines


def build_range_review(snap, hero, opponent, history):
    if snap.street in {"Flop", "Turn"}:
        return _early_review(snap, hero, opponent, history)
    base = advice_at(snap)
    adjusted = apply_history(snap, base, history)
    heads_up = river_opponent(snap) is not None and not any(
        a.street == "River" and a.action == "fold" for a in snap.history
    )
    facing = bool(snap.to_call)
    stages = condition_range(snap, opponent)
    if stages is None:
        return RangeReview(
            "This view requires four known Hero cards and a complete river board.",
            (),
            _play(base),
            _play(adjusted),
            (),
        )
    lines = [
        f"{snap.room} #{snap.number} · {snap.ref} · {opponent.position} ({opponent.name})",
        f"Guide without history · {_play(base)}",
        f"Recommendation with history · {_play(adjusted)}",
    ]
    if base:
        lines.extend(("", (adjusted or base).why))
    if heads_up and facing:
        lines[3:3] = ["", *_river_pivot(snap, stages.stages[-1], history), ""]
        stage = stages.stages[-1]
        model_choice = (
            "call" if stage.weaker + stage.tied / 2 > heads_up_price(snap) else "fold"
        )
        if adjusted and model_choice not in adjusted.choices:
            lines.insert(
                4,
                f"Review this range assumption. The modeled bet range favors {model_choice} when comparing calling with folding. "
                f"The guide lists {_play(adjusted)}.",
            )
    lines.extend(("", "EARLIER HANDS AGAINST THIS OPPONENT"))
    sources = {}
    matched = {}
    broader = ()
    category = best_plo_hand(snap.hero_cards, snap.board)[0]
    sizes = (
        tuple(
            dict.fromkeys(
                bet_size(snap, hero, [opponent], pct).contestable
                for pct in (33, 65, 85)
            )
        )
        if not facing
        else ()
    )
    if not heads_up:
        lines.append(
            "Historical response frequencies apply to heads-up play. The model here describes the selected opponent."
        )
    elif facing:
        sample = history.sample(snap)
        air_range, win_range, known, air = betting_limits(
            sample, category, current_categories(snap)
        )
        n = len(sample.comparable)
        lines.append(
            f"{n} comparable out of {sample.broader} earlier heads-up opening bets against you. {sample.days} dates."
        )
        if any(a.street == "River" and a.action == "raise" for a in snap.history):
            lines.append(
                "This is a raise spot. Opening-bet history cannot estimate the opponent's raising range."
            )
        if n:
            lines.append(
                f"Cards shown {known}/{n}. Hidden or incomplete {n - known}/{n}. Showed high card or a board-only pair {air}/{known if known else '—'}."
            )
            lines.append(
                f"The share of these weak hands in the sample could be {air_range[0]:.1%}–{air_range[1]:.1%} , depending on the hidden cards. Overall bluff frequency and intent remain uncertain."
            )
            if river_holdings(snap.hero_cards, snap.board).weaker:
                lines.append(
                    f"Earlier hand categories and sampling uncertainty give a range of {win_range[0]:.1%}–{win_range[1]:.1%} for this comparison. The call price is {heads_up_price(snap):.1%} before rake."
                )
            else:
                lines.append("Hero cannot win outright on this board. Earlier hand categories cannot identify how often this bet ties.")
        matched.update(((r.hand, r.ref), r) for r in sample.comparable)
        broader = sample.previous
        other_known = sum(row.category is not None for row in broader)
        other_air = sum(row.air for row in broader)
        if sample.broader > n:
            lines.append(
                f"The broader sample across lines includes {sample.broader} bets, {other_known} with cards shown and {other_air} with weak made hands shown."
            )
    else:
        effective = min(hero.stack, opponent.stack)
        seen = set()
        for size in sizes:
            group = sizing(size / snap.pot)
            all_in = size >= effective
            if (group, all_in) in seen:
                continue
            seen.add((group, all_in))
            sample = history.sample(snap, size)
            f, c, r = responses(sample)
            n = f + c + r
            lo, hi = wilson(f / n, n) if n else (0.0, 1.0)
            fold_interval = f" · Wilson {lo:.1%}–{hi:.1%}" if n else ""
            lines.append(
                f"Size group {group}{' · all-in' if all_in else ''} · {n}/{sample.broader} past opportunities in this group · fold {f}, call {c}, raise {r}{fold_interval}."
            )
            matched.update(((row.hand, row.ref), row) for row in sample.comparable)
            broader = sample.previous
    sources.update(matched)
    for row in sorted(
        broader, key=lambda entry: (entry.category is not None, entry.end), reverse=True
    )[:12]:
        sources.setdefault((row.hand, row.ref), row)
    lines.append(
        "The comparison matches network, stakes, antes, preflop role, position, turn action, board rank and suit patterns, stack-to-pot group, all-in status and effective size group. The guide requires at least 30 comparable decisions across three dates and a gap of at least 3 percentage points between the call price and the interval. It uses only completed earlier hands or sessions. The comparison assumes behavior carries over. Hand categories that are impossible on the current board remain uncertain."
    )
    lines.extend(("", "MODELED RANGE FROM PREFLOP TO RIVER", stages.summary))
    for stage in stages.stages:
        actions = " · ".join(stage.actions) or "no recorded action"
        lines.append(f"{stage.street} · {actions}")
        lines.append(
            f"  On the final board · weaker {stage.weaker:.1%} · tie {stage.tied:.1%} · stronger {stage.stronger:.1%} · effective sample {stage.effective_n:.0f}"
        )
        classes = " · ".join(
            f"{name} {weight:.0%}"
            for name, weight in list(stage.categories.items())[:4]
        )
        if stage.draw_share is not None:
            classes += f" · draw {stage.draw_share:.0%}"
        lines.append(f"  Largest groups · {classes}")
        if stage.current_relation:
            weak, tie, strong = stage.current_relation
            lines.append(
                f"  On the board at that point · weaker {weak:.1%} · tie {tie:.1%} · stronger {strong:.1%}."
            )
        lines.append(f"  {stage.reason}")
        if stage.rules:
            lines.append("  " + " ".join(stage.rules))
    for label, example in stages.stages[-1].examples.items():
        translated = {
            "value": "stronger hand",
            "bluff": "possible bluff",
            "weak_call": "possible weaker call",
            "tie": "tie",
        }
        lines.append(
            f"{translated.get(label, label).capitalize()} · {example.text} · {example.reason}"
        )
    lines.extend(("", stages.limitations))
    lines.extend(("", "DECISION UNDER DIFFERENT MODEL ASSUMPTIONS"))
    models = []
    if heads_up and facing:
        price = heads_up_price(snap)
        for intensity in MODEL_INTENSITY:
            model = (
                stages
                if intensity == 1
                else condition_range(snap, opponent, intensity=intensity)
            )
            before, end = model.stages[-2:]
            early = before.weaker + before.tied / 2
            late = end.weaker + end.tied / 2
            choice = "call" if late > price else "fold"
            old = "call" if early > price else "fold"
            models.append(choice)
            lines.append(
                f"Action weight {intensity:g}× · before the river action {early:.1%} ({old}) → after the action {late:.1%} ({choice}) · call threshold {price:.1%}."
            )
    elif (
        heads_up
        and not facing
        and any(
            a.name == opponent.name and a.street == "River" and a.action == "check"
            for a in snap.history
        )
        and snap.can_raise
        and not opponent.all_in
        and bool(river_holdings(snap.hero_cards, snap.board).stronger)
    ):
        end = stages.stages[-1]
        check = (end.weaker + end.tied / 2) * snap.pot
        for name, calling in (
            ("more calls", -0.15),
            ("middle scenario", 0.0),
            ("more folds", 0.15),
        ):
            gains = [
                (
                    size,
                    _bet_ev(
                        end.by_strength,
                        snap.pot,
                        size,
                        calling,
                        size < min(hero.stack, opponent.stack),
                    )
                    - check,
                )
                for size in sizes
            ]
            size, gain = max(gains, key=lambda entry: entry[1])
            choice = f"bet {size / snap.pot:.0%}" if gain > 0 else "check"
            models.append(choice)
            values = " · ".join(
                f"{s / snap.pot:.0%} pot {ev / snap.bb:+.2f} BB" for s, ev in gains
            )
            lines.append(f"{name.capitalize()} · {choice} · {values}")
        lines[3:3] = [
            "",
            "WHAT WOULD CHANGE THIS DECISION?",
            "With the same board and range, vary the opponent's responses to each bet size. "
            + " · ".join(
                f"{label}: {pick}"
                for (label, _), pick in zip(RESPONSE_SCENARIOS, models)
            ),
            "These are bet/fold scenarios against a possible raise.",
            "",
        ]
        lines.append(
            "Values are relative to checking back. Responses depend on hand strength and sizing in this model. Hero folds to a raise. Actual rake and behavior can change the estimate."
        )
    else:
        lines.append(
            "The numerical comparison assumes bet/fold after a heads-up check to Hero. With the nuts, in a multiway pot or out of position, the view shows the line and example holdings."
        )
    lines.append(
        "These scenarios use assumptions for study. Comparable earlier hands against this opponent can help evaluate them."
    )
    return RangeReview(
        "\n".join(lines),
        tuple(
            sorted(
                sources.values(),
                key=lambda r: ((r.hand, r.ref) in matched, r.end),
                reverse=True,
            )
        ),
        _play(base),
        _play(adjusted),
        tuple(models),
        frozenset(matched),
    )
