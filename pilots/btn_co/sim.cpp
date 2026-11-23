// Independent offline PLO4 policy model, not an equilibrium solver. All amounts
// are in BB. The input contains only public pre-action state and Hero's cards.
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <map>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using std::array;
using Hand = array<int, 4>;
using Four = array<double, 4>;
constexpr int CO = 0, BTN = 1, SB = 2, BB = 3;
constexpr double eps = 1e-8;
const std::string ranks = "23456789TJQKA", suits = "cdhs";
void require(bool ok, const std::string &message) {
  if (!ok)
    throw std::runtime_error(message);
}
double bounded(double x) { return std::max(0., std::min(1., x)); }
uint64_t mix(uint64_t x) {
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}
struct Rng {
  uint64_t state;
  uint64_t next() {
    state += 0x9e3779b97f4a7c15ULL;
    return mix(state);
  }
  int index(int n) {
    const uint64_t bound = static_cast<uint64_t>(n),
                   limit = uint64_t(-bound) % bound;
    uint64_t x;
    do {
      x = next();
    } while (x < limit);
    return int(x % bound);
  }
};
double noise(uint64_t key, int stage, int seat, int event) {
  return (mix(key ^ (uint64_t(257 * stage + 13 * seat + event) *
                     0x9e3779b97f4a7c15ULL)) >>
          11) *
         (1.0 / 9007199254740992.0);
}
int read_card(const std::string &s) {
  require(s.size() == 2, "Cards use two characters, e.g. As.");
  size_t r = ranks.find(s[0]), u = suits.find(s[1]);
  require(r != std::string::npos && u != std::string::npos,
          "Invalid card: " + s);
  return int(4 * r + u);
}
Hand read_hand(std::string s) {
  s.erase(std::remove_if(s.begin(), s.end(),
                         [](char c) { return c == ' ' || c == ','; }),
          s.end());
  require(s.size() == 8, "Hero must have four cards.");
  Hand h;
  uint64_t seen = 0;
  for (int i = 0; i < 4; ++i) {
    h[i] = read_card(s.substr(2 * i, 2));
    uint64_t m = 1ULL << h[i];
    require(!(seen & m), "Duplicate Hero card.");
    seen |= m;
  }
  return h;
}
double number(const std::string &s) {
  size_t n = 0;
  double x = std::stod(s, &n);
  require(n == s.size() && std::isfinite(x), "Invalid numeric argument: " + s);
  return x;
}
struct Config {
  Hand hero{};
  double pot = 0, open = 0, sb = .5, bb = 1, ante = 0, rake = .05, cap = 3;
  double open_pct = 32, co_continue = 18, blind_scale = 1;
  Four stacks{};
  int trials = 10000;
  uint64_t seed = 712;
  std::string profile = "base", post = "simple";
};
Config config(int argc, char **argv) {
  std::map<std::string, std::string> args;
  for (int i = 1; i < argc; i += 2) {
    require(i + 1 < argc, "Missing option value.");
    require(args.emplace(argv[i], argv[i + 1]).second, "Repeated option.");
  }
  auto get = [&](std::string k, std::string d = "") {
    auto it = args.find(k);
    if (it == args.end())
      return d;
    auto v = it->second;
    args.erase(it);
    return v;
  };
  Config c;
  c.hero = read_hand(get("--hero"));
  c.pot = number(get("--pot"));
  c.open = number(get("--open"));
  c.sb = number(get("--sb", ".5"));
  c.bb = number(get("--bb", "1"));
  c.rake = number(get("--rake", ".05"));
  c.cap = number(get("--cap", "3"));
  c.ante = number(get("--ante", "0"));
  c.profile = get("--profile", "base");
  c.post = get("--postflop", "simple");
  require(c.profile == "base" || c.profile == "tight" || c.profile == "loose",
          "Unknown profile.");
  require(c.post == "simple" || c.post == "checkdown",
          "Unknown postflop model.");
  c.open_pct = number(get("--open-pct", c.profile == "tight"   ? "24"
                                        : c.profile == "loose" ? "42"
                                                               : "32"));
  c.co_continue = number(get("--co-continue", c.profile == "tight"   ? "12"
                                              : c.profile == "loose" ? "25"
                                                                     : "18"));
  c.blind_scale = number(get("--blind-scale", c.profile == "tight"   ? ".8"
                                              : c.profile == "loose" ? "1.35"
                                                                     : "1"));
  double n = number(get("--trials", "10000"));
  require(n >= 2 && n <= 10000000 && n == std::floor(n), "Invalid trials.");
  c.trials = int(n);
  std::string seed = get("--seed", "712");
  size_t used = 0;
  c.seed = std::stoull(seed, &used);
  require(used == seed.size() && seed[0] != '-', "Invalid seed.");
  std::stringstream list(get("--stacks"));
  std::string token;
  for (double &v : c.stacks) {
    require(bool(std::getline(list, token, ',')),
            "Provide four remaining stacks CO,BTN,SB,BB.");
    v = number(token);
    require(v > 0, "All four seats must have chips behind at node.");
  }
  require(!std::getline(list, token, ','), "Provide four stacks only.");
  require(args.empty(), "Unknown command line option.");
  require(c.bb > 0 && c.sb > 0 && c.sb <= c.bb && c.ante >= 0 &&
              c.open >= 2 * c.bb - eps,
          "Unsupported blinds or opening amount.");
  require(c.pot >= c.open + c.sb + c.bb + 4 * c.ante - eps,
          "Known live contributions exceed pot; check antes and amounts.");
  require(c.rake >= 0 && c.rake < 1 && c.cap >= 0, "Invalid rake/cap.");
  require(c.open_pct >= 1 && c.open_pct <= 100 && c.co_continue >= 0 &&
              c.co_continue <= 100 && c.blind_scale > 0 && c.blind_scale <= 3,
          "Invalid policy cutoff.");
  return c;
}

// Rank-only ordered five-card lookup plus a separate flush table. Ten board
// triples times six hole pairs on a river, always exactly two plus three.
uint32_t normal[371293], suited[371293];
uint32_t rank5(array<int, 5> r, bool flush) {
  array<int, 13> count{};
  for (int x : r)
    ++count[x];
  array<int, 5> group{};
  int n = 0, pairs = 0, trip = 0, quad = 0;
  for (int f = 4; f >= 1; --f)
    for (int x = 12; x >= 0; --x)
      if (count[x] == f) {
        for (int j = 0; j < f; ++j)
          group[n++] = x + 2;
        pairs += f == 2;
        trip += f == 3;
        quad += f == 4;
      }
  if (n != 5)
    return 0; // five identical ranks cannot occur with real cards
  int sh = 0, run = 0;
  for (int x = 0; x < 13; ++x) {
    run = count[x] ? run + 1 : 0;
    if (run == 5)
      sh = x + 2;
  }
  if (!sh && count[12] && count[0] && count[1] && count[2] && count[3])
    sh = 5;
  int cat = flush && sh     ? 8
            : quad          ? 7
            : trip && pairs ? 6
            : flush         ? 5
            : sh            ? 4
            : trip          ? 3
            : pairs == 2    ? 2
            : pairs         ? 1
                            : 0;
  if (sh) {
    group = {sh, 0, 0, 0, 0};
  }
  uint32_t out = cat;
  for (int x : group)
    out = out * 16 + x;
  return out;
}
int rank_key(const array<int, 5> &r) {
  int key = 0;
  for (int x : r)
    key = key * 13 + x;
  return key;
}
void make_ranks() {
  for (int a = 0; a < 13; ++a)
    for (int b = a; b < 13; ++b)
      for (int c = b; c < 13; ++c)
        for (int d = c; d < 13; ++d)
          for (int e = d; e < 13; ++e) {
            array<int, 5> r{a, b, c, d, e};
            uint32_t v = rank5(r, false), s = rank5(r, true);
            do {
              int k = rank_key(r);
              normal[k] = v;
              suited[k] = s;
            } while (std::next_permutation(r.begin(), r.end()));
          }
}
struct Triple {
  int rank, suit;
};
struct Feature {
  uint32_t value = 0;
  int category = 0;
  bool draw = false, top = false;
};
Feature evaluate(const Hand &h, const array<int, 5> &board, int n) {
  Triple triples[10];
  int tn = 0, top = 0, board_mask = 0;
  array<int, 4> sc{};
  for (int a = 0; a < n; ++a) {
    top = std::max(top, board[a] / 4);
    board_mask |= 1 << (board[a] / 4);
    sc[board[a] % 4]++;
    for (int b = a + 1; b < n; ++b)
      for (int c = b + 1; c < n; ++c)
        triples[tn++] = {(board[a] / 4 * 13 + board[b] / 4) * 13 + board[c] / 4,
                         board[a] % 4 == board[b] % 4 &&
                                 board[a] % 4 == board[c] % 4
                             ? board[a] % 4
                             : -1};
  }
  Feature f;
  for (int i = 0; i < 4; ++i) {
    f.top |= h[i] / 4 == top;
    for (int j = i + 1; j < 4; ++j) {
      int pref = (h[i] / 4 * 13 + h[j] / 4) * 2197;
      int suit = h[i] % 4 == h[j] % 4 ? h[i] % 4 : -2;
      if (n < 5 && suit >= 0 && sc[suit] == 2)
        f.draw = true;
      for (int b = 0; b < tn; ++b) {
        const auto t = triples[b];
        int k = pref + t.rank;
        f.value = std::max(f.value, suit == t.suit ? suited[k] : normal[k]);
      }
      if (n < 5 && h[i] / 4 != h[j] / 4 && !f.draw) {
        int pair = (1 << (h[i] / 4)) | (1 << (h[j] / 4));
        for (int hi = 4; hi <= 13; ++hi) {
          int w = hi == 13 ? 4111 : (31 << (hi - 4));
          int v = board_mask & w & ~pair;
          if ((pair & w) == pair && v && (v & (v - 1))) {
            f.draw = true;
            break;
          }
        }
      }
    }
  }
  f.category = int(f.value >> 20);
  return f;
}

// Original score, intentionally a transparent range assumption. Rank bands
// use mid-percentiles over all 270,725 exact combinations, before blockers.
int hand_score(const Hand &h) {
  array<int, 13> rc{};
  array<int, 4> sc{};
  int mask = 0, nut = 0, score = 0;
  for (int c : h) {
    int r = c / 4;
    rc[r]++;
    sc[c % 4]++;
    mask |= 1 << r;
    score += 2 * r;
  }
  for (int c : h)
    if (c / 4 == 12 && sc[c % 4] >= 2)
      nut++;
  int suits2 = 0;
  for (int n : sc) {
    suits2 += n >= 2;
    if (n == 3)
      score -= 6;
    if (n == 4)
      score -= 14;
  }
  score += 8 * suits2 + 8 * (suits2 == 2) + 8 * nut;
  for (int r = 0; r < 13; ++r) {
    if (rc[r] == 2)
      score += (r == 12 ? 55 : r == 11 ? 34 : r == 10 ? 27 : 8 + r);
    if (rc[r] == 3)
      score -= 18;
    if (rc[r] == 4)
      score -= 43;
  }
  int near = 0;
  for (int start = 0; start < 10; ++start) {
    int n = 0;
    int w = start == 9 ? 4111 : 31 << start;
    for (int z = mask & w; z; z &= z - 1)
      ++n;
    near = std::max(near, n);
  }
  return 128 + score + 6 * near + 7 * (near == 4);
}
array<double, 512> percentile{};
array<int, 512> frequencies{};
void range_table() {
  for (int a = 0; a < 49; ++a)
    for (int b = a + 1; b < 50; ++b)
      for (int c = b + 1; c < 51; ++c)
        for (int d = c + 1; d < 52; ++d)
          ++frequencies.at(hand_score({a, b, c, d}));
  int above = 0;
  for (int i = 511; i >= 0; --i) {
    percentile[i] = 100. * (above + .5 * frequencies[i]) / 270725.;
    above += frequencies[i];
  }
  require(above == 270725, "Incomplete range table.");
}
struct Deal {
  array<Hand, 4> hands{};
  array<double, 4> pct{};
  array<int, 5> board{};
  array<array<Feature, 4>, 3> hand{};
  uint64_t key = 0;
};
Deal draw(const Config &c, Rng &rng, const array<int, 48> &deck) {
  Deal d;
  d.hands[BTN] = c.hero;
  array<int, 48> current;
  // Draw the opener first and condition on opening. The blinds and runout
  // remain uniform without replacement given the accepted CO hand and Hero.
  // Earlier folded seats' cards are marginalized without conditioning on
  // their folds. Only their publicly committed dead money enters the ledger.
  do {
    current = deck;
    for (int i = 0; i < 4; ++i) {
      std::swap(current[i], current[i + rng.index(48 - i)]);
      d.hands[CO][i] = current[i];
    }
    d.pct[CO] = percentile[hand_score(d.hands[CO])];
  } while (d.pct[CO] > c.open_pct);
  for (int i = 4; i < 17; ++i)
    std::swap(current[i], current[i + rng.index(48 - i)]);
  for (int i = 0; i < 4; ++i) {
    d.hands[SB][i] = current[4 + i];
    d.hands[BB][i] = current[8 + i];
  }
  for (int i = 0; i < 5; ++i)
    d.board[i] = current[12 + i];
  for (int p = 0; p < 4; ++p) {
    d.pct[p] = percentile[hand_score(d.hands[p])];
    for (int st = 0; st < 3; ++st)
      d.hand[st][p] = evaluate(d.hands[p], d.board, 3 + st);
  }
  d.key = rng.next();
  return d;
}
struct State {
  Four stack{}, paid{}, street{};
  array<bool, 4> live{true, true, true, true};
  double dead = 0, price = 0, last = 0;
  int aggressor = CO, tier = 1;
  State(const Config &c) : stack(c.stacks), street{c.open, 0, c.sb, c.bb} {
    for (int i = 0; i < 4; ++i)
      paid[i] = street[i] + c.ante;
    dead = std::max(0., c.pot - std::accumulate(paid.begin(), paid.end(), 0.));
    price = c.open;
    last = c.open - c.bb;
  }
  int active() const { return int(std::count(live.begin(), live.end(), true)); }
  double pot() const {
    return dead + std::accumulate(paid.begin(), paid.end(), 0.);
  }
  void pay(int p, double amount) {
    require(amount >= -eps && amount <= stack[p] + eps, "Invalid payment.");
    double x = std::clamp(amount, 0., stack[p]);
    stack[p] -= x;
    paid[p] += x;
    street[p] += x;
  }
  double owed(int p) const { return std::max(0., price - street[p]); }
  bool contested(int p) const {
    for (int i = 0; i < 4; ++i)
      if (i != p && live[i] && stack[i] > eps &&
          stack[i] + street[i] > price + eps)
        return true;
    return false;
  }
  double raise_to(int p) const {
    if (!contested(p))
      return price;
    double to = std::min(street[p] + stack[p], street[p] + pot() + 2 * owed(p));
    if (to < price + last - eps && std::abs(to - street[p] - stack[p]) > eps)
      return price;
    return to;
  }
  void refund() {
    int leader = 0;
    for (int i = 1; i < 4; ++i)
      if (street[i] > street[leader])
        leader = i;
    double second = 0;
    for (int i = 0; i < 4; ++i)
      if (i != leader)
        second = std::max(second, street[i]);
    double diff = street[leader] - second;
    if (diff > eps) {
      stack[leader] += diff;
      paid[leader] -= diff;
      street[leader] -= diff;
    }
  }
};
struct Outcome {
  double ev = 0;
  bool flop = false, multi = false, fourbet = false, squeeze = false,
       wonpre = false;
};
enum Move { fold, call, raise };
Move pre_policy(const Config &c, const State &s, const Deal &d, int p,
                bool rights) {
  double h = d.pct[p], to_call = s.owed(p), pr = std::min(to_call, s.stack[p]);
  double odds = pr / (s.pot() + pr);
  double call_cut = 0, raise_cut = 0;
  bool blind = p == SB || p == BB;
  if (s.tier == 1) { // blinds facing an open and a BTN cold-call
    call_cut = (p == BB ? 38. : 23.) * c.blind_scale;
    raise_cut = (p == BB ? 6. : 5.) * c.blind_scale;
  } else if (s.tier == 2) {
    if (p == CO) {
      call_cut = c.co_continue;
      raise_cut = 3.;
    } else if (blind && s.street[p] <= c.bb + eps) {
      call_cut = (p == BB ? 11. : 8.) * c.blind_scale;
      raise_cut = 2.4 * c.blind_scale;
    } else {
      call_cut = 22.;
      raise_cut = 3.5;
    }
  } else {
    call_cut = std::clamp(13. + 50. * (.28 - odds), 6., 30.);
  }
  bool cont = h <= call_cut;
  if (rights && s.tier < 3 && cont && h <= raise_cut &&
      noise(d.key, 1 + s.tier, p, 1) < .82 && s.raise_to(p) > s.price + eps)
    return raise;
  if (!to_call || cont)
    return call;
  return fold;
}
void preflop(const Config &c, State &s, const Deal &d, bool threebet,
             Outcome &out) {
  array<bool, 4> pending{false, false, true, true};
  Four acted{-1, -1, -1, -1};
  if (threebet) {
    double to = s.raise_to(BTN);
    require(to > s.price + eps, "BTN cannot make a legal 3bet.");
    double increase = to - s.price;
    s.pay(BTN, to - s.street[BTN]);
    s.price = to;
    s.last = std::max(s.last, increase);
    s.tier = 2;
    s.aggressor = BTN;
    pending[CO] = true;
  } else {
    s.pay(BTN, std::min(s.stack[BTN], s.owed(BTN)));
  }
  acted[BTN] = s.price;
  acted[CO] = c.open;
  int p = SB, steps = 0;
  while (
      std::any_of(pending.begin(), pending.end(), [](bool x) { return x; }) &&
      s.active() > 1) {
    require(++steps < 64, "Preflop action loop did not finish.");
    if (pending[p] && s.live[p] && s.stack[p] > eps) {
      bool rights = acted[p] < 0 || s.price - acted[p] >= s.last - eps;
      Move m = pre_policy(c, s, d, p, rights);
      if (m == fold)
        s.live[p] = false;
      else if (m == call)
        s.pay(p, std::min(s.stack[p], s.owed(p)));
      else {
        double to = s.raise_to(p), increase = to - s.price;
        s.pay(p, to - s.street[p]);
        s.price = to;
        s.last = std::max(s.last, increase);
        ++s.tier;
        s.aggressor = p;
        if (s.tier == 3)
          out.fourbet = true;
        if (s.tier == 2 && (p == SB || p == BB))
          out.squeeze = true;
        for (int i = 0; i < 4; ++i)
          pending[i] =
              i != p && s.live[i] && s.stack[i] > eps && s.owed(i) > eps;
      }
      acted[p] = s.price;
    }
    pending[p] = false;
    p = (p + 1) % 4;
  }
  s.refund();
  out.flop = s.active() > 1;
  out.multi = s.active() >= 3;
  out.wonpre = s.active() == 1 && s.live[BTN];
}
double bet_probability(const Feature &f) {
  if (f.category >= 4)
    return .87;
  if (f.category == 3)
    return .73;
  if (f.category == 2)
    return .52;
  if (f.draw)
    return .42;
  if (f.category == 1 && f.top)
    return .30;
  if (f.category == 1)
    return .18;
  return .12;
}
double call_probability(const Feature &f) {
  if (f.category >= 4)
    return .97;
  if (f.category == 3)
    return .93;
  if (f.category == 2)
    return .81;
  if (f.draw)
    return .65;
  if (f.category == 1 && f.top)
    return .59;
  if (f.category == 1)
    return .28;
  return .055;
}
void postflop(State &s, const Deal &d, int st) {
  if (s.active() < 2)
    return;
  s.street.fill(0);
  s.price = 0;
  int bettor = -1, old = s.aggressor;
  for (int j = 0; j < 4; ++j) {
    int p = (SB + j) % 4;
    if (!s.live[p] || s.stack[p] <= eps || !s.contested(p))
      continue;
    double prob = bet_probability(d.hand[st][p]) + .065 * (p == old) -
                  .055 * (s.active() - 2);
    if (noise(d.key, 10 + st, p, 1) >= bounded(prob))
      continue;
    double amount = std::min(s.stack[p], .5 * s.pot());
    s.pay(p, amount);
    s.price = amount;
    bettor = p;
    s.aggressor = p;
    break;
  }
  if (bettor < 0)
    return;
  for (int j = 1; j < 4; ++j) {
    int p = (bettor + j) % 4;
    if (!s.live[p] || s.stack[p] <= eps)
      continue;
    double need = std::min(s.stack[p], s.owed(p));
    double odds = need / (s.pot() + need);
    double prob = call_probability(d.hand[st][p]) + 1.4 * (.25 - odds) -
                  .035 * (s.active() - 2);
    if (noise(d.key, 10 + st, p, 2) < bounded(prob))
      s.pay(p, need);
    else
      s.live[p] = false;
  }
  s.refund();
}
Four awards(const State &s, const array<uint32_t, 4> &ranks, double rake,
            double cap, bool flop) {
  Four result{}, levels = s.paid;
  std::sort(levels.begin(), levels.end());
  double previous = 0, dead = s.dead;
  double pot = s.pot(), fee = flop ? std::min(cap, rake * pot) : 0,
         scale = pot > 0 ? (pot - fee) / pot : 1;
  for (double next : levels) {
    if (next <= previous + eps)
      continue;
    double layer = dead;
    dead = 0;
    uint32_t best = 0;
    int winners = 0;
    for (int p = 0; p < 4; ++p)
      if (s.paid[p] >= next - eps) {
        layer += next - previous;
        if (s.live[p]) {
          if (!winners || ranks[p] > best) {
            best = ranks[p];
            winners = 1;
          } else if (ranks[p] == best)
            ++winners;
        }
      }
    require(winners > 0, "Side pot has no eligible player.");
    for (int p = 0; p < 4; ++p)
      if (s.paid[p] >= next - eps && s.live[p] && ranks[p] == best)
        result[p] += layer * scale / winners;
    previous = next;
  }
  require(std::abs(std::accumulate(result.begin(), result.end(), 0.) + fee -
                   pot) < 1e-6 * std::max(1., pot),
          "Award ledger unbalanced.");
  return result;
}
Outcome rollout(const Config &c, const Deal &d, bool threebet) {
  State s(c);
  Outcome out;
  preflop(c, s, d, threebet, out);
  if (out.flop && c.post == "simple")
    for (int st = 0; st < 3; ++st)
      postflop(s, d, st);
  array<uint32_t, 4> scores;
  for (int i = 0; i < 4; ++i)
    scores[i] = d.hand[2][i].value;
  Four won = awards(s, scores, c.rake, c.cap, out.flop);
  out.ev = won[BTN] - (c.stacks[BTN] - s.stack[BTN]);
  return out;
}
struct Moment {
  int count = 0;
  double mean = 0, m2 = 0;
  void add(double x) {
    ++count;
    double diff = x - mean;
    mean += diff / count;
    m2 += diff * (x - mean);
  }
  double se() const { return std::sqrt(m2 / (count - 1) / count); }
};
struct Diagnostics {
  int flop = 0, multi = 0, fourbet = 0, squeeze = 0, wonpre = 0;
  void add(const Outcome &o) {
    flop += o.flop;
    multi += o.multi;
    fourbet += o.fourbet;
    squeeze += o.squeeze;
    wonpre += o.wonpre;
  }
  void print() const {
    std::cout << "{\"flop\":" << flop << ",\"multiway_flop\":" << multi
              << ",\"fourbet\":" << fourbet << ",\"blind_squeeze\":" << squeeze
              << ",\"won_preflop\":" << wonpre << "}";
  }
};
void simulation(const Config &c) {
  auto start = std::chrono::steady_clock::now();
  range_table();
  State start_state(c);
  double to = start_state.raise_to(BTN);
  require(to > c.open + eps, "No legal BTN pot 3bet at this stack depth.");
  array<int, 48> deck{};
  int j = 0;
  for (int i = 0; i < 52; ++i)
    if (std::find(c.hero.begin(), c.hero.end(), i) == c.hero.end())
      deck[j++] = i;
  Rng rng{c.seed};
  Moment ce, te, delta;
  Diagnostics cd, td;
  for (int k = 0; k < c.trials; ++k) {
    Deal d = draw(c, rng, deck);
    Outcome a = rollout(c, d, false), b = rollout(c, d, true);
    ce.add(a.ev);
    te.add(b.ev);
    delta.add(b.ev - a.ev);
    cd.add(a);
    td.add(b);
  }
  std::cout << std::setprecision(14) << "{\"trials\":" << c.trials
            << ",\"seed\":" << c.seed << ",\"threebet_to\":" << to
            << ",\"fold_ev\":0,\"call_ev\":" << ce.mean
            << ",\"threebet_ev\":" << te.mean
            << ",\"difference_ev\":" << delta.mean << ",\"call_se\":" << ce.se()
            << ",\"threebet_se\":" << te.se()
            << ",\"difference_se\":" << delta.se()
            << ",\"open_pct\":" << c.open_pct
            << ",\"co_continue\":" << c.co_continue
            << ",\"blind_scale\":" << c.blind_scale << ",\"rake\":" << c.rake
            << ",\"cap\":" << c.cap << ",\"postflop\":\"" << c.post
            << "\",\"call_events\":";
  cd.print();
  std::cout << ",\"threebet_events\":";
  td.print();
  std::cout << ",\"seconds\":"
            << std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                             start)
                   .count()
            << "}\n";
}
Feature score_text(const Hand &h, const std::string &text) {
  require(text.size() == 6 || text.size() == 8 || text.size() == 10,
          "Board requires 3, 4, or 5 cards.");
  uint64_t seen = 0;
  for (int card : h)
    seen |= 1ULL << card;
  array<int, 5> board{};
  int n = int(text.size() / 2);
  for (int i = 0; i < n; ++i) {
    board[i] = read_card(text.substr(i * 2, 2));
    uint64_t mask = 1ULL << board[i];
    require(!(seen & mask), "Board or hole card repeated.");
    seen |= mask;
  }
  return evaluate(h, board, n);
}
void expect_near(double got, double target, const std::string &label) {
  require(std::abs(got - target) < eps, label);
}
void self_test() {
  auto royal = score_text(read_hand("AsKs9d8c"), "QsJsTs");
  require(royal.category == 8 && (royal.value & 0xfffff) == 14 * 65536,
          "Royal on the flop.");
  require(score_text(read_hand("2c4d6h8c"), "AsKsQsJsTs").category == 0,
          "Cannot play board-only royal.");
  require(score_text(read_hand("AcKd3c4h"), "QsJdTc2h9c").value ==
              score_text(read_hand("AdKh5s6h"), "QsJdTc2h9c").value,
          "Equal Broadway straights.");
  auto wheel = score_text(read_hand("Ac2d9hJh"), "3s4d5c");
  require(wheel.category == 4 && (wheel.value & 0xfffff) == 5 * 65536,
          "Five-high straight.");
  Config c;
  c.open = 3.5;
  c.pot = 5.6;
  c.ante = .1;
  c.stacks = {86, 97, 28, 42};
  State s(c);
  expect_near(s.pot(), 5.6, "Starting pot.");
  expect_near(s.dead, .2, "Dead folded antes.");
  expect_near(s.raise_to(BTN), 12.6, "Pot-sized BTN 3bet.");
  s.street = {0, 7, 25, 15};
  s.paid = s.street;
  double before = s.pot(), stack = s.stack[SB];
  s.refund();
  expect_near(s.pot(), before - 10, "Uncalled return.");
  expect_near(s.stack[SB], stack + 10, "Returned to correct seat.");
  s.paid = {20, 60, 100, 100};
  s.dead = 2;
  auto w = awards(s, {4, 3, 2, 1}, .05, 3, false);
  expect_near(w[CO], 82, "Main pot and dead money.");
  expect_near(w[BTN], 120, "First side pot.");
  expect_near(w[SB], 80, "Second side pot.");
  w = awards(s, {4, 3, 2, 2}, .05, 3, true);
  expect_near(w[CO], 82 * 279. / 282, "Capped proportional rake.");
  expect_near(w[SB], 40 * 279. / 282, "Split side pot.");
  expect_near(w[BB], w[SB], "Tied payout.");
  s.live[CO] = false;
  w = awards(s, {9, 3, 2, 1}, 0, 0, true);
  expect_near(w[CO], 0, "Folded hand ineligible.");
  expect_near(w[BTN], 202, "Folded contribution stays in pot.");
  c.stacks[BTN] = 4.5;
  State shorty(c);
  expect_near(shorty.raise_to(BTN), 4.5, "Legal short all-in increase.");
  Deal d{};
  d.pct = {1, 10, 100, 100};
  Outcome out;
  preflop(c, shorty, d, true, out);
  expect_near(shorty.stack[BTN], 0, "All-in stack exhausted.");
  require(shorty.tier == 2 && shorty.active() == 2 && !out.fourbet,
          "Short raise cannot reopen for the opener.");
  expect_near(shorty.pot(), 11.1,
              "Short all-in called in full including six antes.");
  w = awards(shorty, {2, 3, 9, 9}, .05, 3, true);
  expect_near(w[BTN], 11.1 * .95, "Short all-in main-pot award.");
  range_table();
  require(percentile[hand_score(read_hand("AsAhKdKc"))] <
              percentile[hand_score(read_hand("2c2d2h3s"))],
          "Starting hand ordering.");
}
} // namespace

int main(int argc, char **argv) {
  try {
    make_ranks();
    if (argc == 2 && std::string(argv[1]) == "--self-test") {
      self_test();
      std::cout << "OK\n";
    } else if (argc == 2 && std::string(argv[1]) == "--eval-batch") {
      std::string hole, board;
      while (std::cin >> hole) {
        require(bool(std::cin >> board), "Missing board.");
        std::cout << score_text(read_hand(hole), board).value << '\n';
      }
    } else
      simulation(config(argc, argv));
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 2;
  }
}
