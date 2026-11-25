"""Full-width alternating CFR+, average-policy best responses, exact weighted card removal."""

import time

import numpy as np

from .model import GROUPS, Game, Leaf, cards, digest


def normalized(x):
    totals = x.sum(axis=0)
    return np.divide(x, totals, out=np.full_like(x, 1 / len(x)), where=totals > 0)


def walk(node, game, seat, theirs, ours, t, mode):
    if isinstance(node, Leaf):
        if game.products is not None:
            mass = game.products[seat](theirs, node.winner is None)
            return mass * (node.value if node.winner in (None, seat) else -node.value)
        if node.winner is None:
            return (game.show[seat] @ theirs) * node.value
        return (game.legal[seat] @ theirs) * (
            node.value if seat == node.winner else -node.value
        )
    policy = node.policy
    if node.seat != seat:
        return sum(
            walk(child, game, seat, theirs * p, ours, t, mode)
            for child, p in zip(node.children.values(), policy)
        )
    scores = np.array(
        [
            walk(child, game, seat, theirs, ours * p, t, mode)
            for child, p in zip(node.children.values(), policy)
        ]
    )
    if mode == "br":
        return scores.max(axis=0)
    result = (policy * scores).sum(axis=0)
    if mode == "update":
        node.sums += t * ours * policy
        np.maximum(node.regrets + scores - result, 0, out=node.regrets)
        node.policy = normalized(node.regrets)
    return result


def nodes(node):
    if not isinstance(node, Leaf):
        yield node
        for child in node.children.values():
            yield from nodes(child)


def root_values(game, mode):
    return [
        float(
            game.weights[s]
            @ walk(game.root, game, s, game.weights[1 - s], game.weights[s], 0, mode)
            / game.mass
        )
        + game.pot / 2
        for s in (0, 1)
    ]


def describe(game, spec):
    path = spec["query"]["path"]
    node = game.root
    reach = [w.copy() for w in game.weights]
    for action in path:
        i = list(node.children).index(action)
        reach[node.seat] *= node.policy[i]
        node = node.children[action]
    seat = node.seat
    index = game.hands[seat].index(tuple(sorted(cards(spec["query"]["hero"], 4))))
    opp = reach[1 - seat]
    oppmass = game.legal[seat] @ opp
    total = oppmass * reach[seat]
    ops = game.ranks[1 - seat]
    hero_rank = game.ranks[seat][index]
    mask = game.legal[seat][index]
    weights = opp * mask

    def distribution(keys, w):
        return (
            {
                str(k): float(sum(v for key, v in zip(keys, w) if key == k) / w.sum())
                for k in sorted(set(keys))
            }
            if w.sum() > 0
            else {}
        )

    groups = {}
    for category in sorted({h[0] for h in game.ranks[seat]}):
        w = total * np.array([h[0] == category for h in game.ranks[seat]])
        if w.sum() > 0:
            groups[GROUPS[category]] = {
                a: float(p @ w / w.sum()) for a, p in zip(node.children, node.policy)
            }
    ev = {}
    optimal_continuation = {}
    responses = {}
    if oppmass[index] > 0:
        for action, child in node.children.items():
            vector = walk(child, game, seat, opp, reach[seat], 0, "eval")
            ev[action] = float(vector[index] / oppmass[index] + game.pot / 2)
            best = walk(child, game, seat, opp, reach[seat], 0, "br")
            optimal_continuation[action] = float(
                best[index] / oppmass[index] + game.pot / 2
            )
            if not isinstance(child, Leaf) and child.seat != seat:
                responses[action] = {}
                for response, p in zip(child.children, child.policy):
                    mass = weights * p
                    responses[action][response] = {
                        "frequency": float(mass.sum() / weights.sum()),
                        "weaker_fraction": float(
                            sum(w for w, r in zip(mass, ops) if r < hero_rank)
                            / mass.sum()
                        )
                        if mass.sum() > 0
                        else None,
                    }
    available = float(mask @ game.weights[1 - seat])
    return {
        "actor": ("oop", "ip")[seat],
        "hero_action_ev_bb": ev,
        "hero_ev_with_best_continuation_vs_fixed_opponent_bb": optimal_continuation,
        "opponent_reply_given_hero": responses,
        "ev_basis": "Net of all river contributions, relative to start of river; continuation follows average policy.",
        "hero_strategy": {
            a: float(p[index]) for a, p in zip(node.children, node.policy)
        },
        "range_strategy_by_made_hand": groups,
        "node_probability": float(total.sum() / game.mass),
        "opponent_path_probability_given_hero": float(oppmass[index] / available)
        if available
        else 0,
        "opponent_categories_given_hero_and_path": distribution(
            [GROUPS[r[0]] for r in ops], weights
        ),
        "hero_equity_given_path": float(
            sum(
                w * ((r < hero_rank) + 0.5 * (r == hero_rank))
                for r, w in zip(ops, weights)
            )
            / weights.sum()
        )
        if weights.sum()
        else None,
    }


def solve(spec, *, iterations=8000, target_pct=0.1, minimum_iterations=100):
    if iterations < 1 or minimum_iterations < 1 or target_pct < 0:
        raise ValueError("Invalid convergence controls")
    start = time.perf_counter()
    # The public reliability ceiling is 0.1% exploitability (0.2% total gain).
    required_gap_pct = min(target_pct, 0.2)
    game = Game(spec)
    all_nodes = list(nodes(game.root))
    for t in range(1, iterations + 1):
        for s in (0, 1):
            walk(game.root, game, s, game.weights[1 - s], game.weights[s], t, "update")
        if t == iterations or (t >= minimum_iterations and t % 100 == 0):
            live = [n.policy for n in all_nodes]
            for n in all_nodes:
                n.policy = normalized(n.sums)
            br = root_values(game, "br")
            gap = max(0, sum(br) - game.pot)
            if 100 * gap / game.pot <= required_gap_pct or t == iterations:
                break
            for n, p in zip(all_nodes, live):
                n.policy = p
    ev = root_values(game, "eval")
    if abs(sum(ev) - game.pot) > 1e-8 * max(1, game.pot):
        raise ArithmeticError("Money conservation failed")
    if any(b + 1e-8 < e for b, e in zip(br, ev)):
        raise ArithmeticError("Best response below policy value")
    result = {
        "input": spec,
        "input_sha256": digest(spec),
        "ranges_sha256": digest(spec["ranges"]),
        "iterations": t,
        "max_iterations": iterations,
        "target_gap_pct": target_pct,
        "best_response_ev_bb": br,
        "game_ev_bb": ev,
        "nashconv_pct_pot": 100 * gap / game.pot,
        "exploitability_pct_pot": 50 * gap / game.pot,
        "reliable": 100 * gap / game.pot <= required_gap_pct,
        "legal_joint_deals": int(game.legal[0].sum()),
        "query": describe(game, spec),
    }
    result["solution_sha256"] = digest(result)
    return result, {"elapsed_seconds": time.perf_counter() - start}
