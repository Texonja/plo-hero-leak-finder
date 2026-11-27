# PLO Hero Leak Finder for Windows x64

## Getting started

1. Right-click the ZIP in File Explorer and choose Extract all.
2. Open the extracted folder and double-click START.BAT. Both the Windows
   release and GitHub's Code > Download ZIP include the Windows x64 runtime.
3. Select Library > HeroDemo to try the public sample from both networks.

## Upgrading an existing library

Close the older app before opening this build. Before upgrading an existing
library, the app saves a SQLite copy in the backups folder next to that
library. The copy includes your private hands and aliases. Allow enough disk
space for a copy of the database. If the backup fails, the upgrade stops.
To use the older app again, copy its backup file to a separate folder and
open that copy using the older app's START.BAT --library "path-to-copy".

## Importing completed hands

Use Import > Import files to select individual files. Use Import > Import
folder to select an archive root. Folder import includes all subfolders.
You can select the same folder after each session. Latest import shows
only newly added hands. If every hand is already present, your previous
selection stays open. A changed copy can supply missing information or
flag conflicting records. The review index updates affected hands.
Importing from HeroDemo opens your personal library.

CoinPoker uses native text. iPoker uses XML with one or more hands or
an HM2 .pghembk or .sql export containing native iPoker histories.
You can select a .zip of native .txt and .xml histories directly.
The app reads histories in its subfolders and skips hands already imported.
ZIPs containing an HM2 hand-history dump also work.
HUD summaries lack the original actions required for analysis.

Use Player > Personal Hero aliases to enter your exact screen names,
one per line and under the correct network. The app analyzes hands
with a single matching Hero.

Hands to review lists each flagged hand once, with up to twelve per page.
Open Action critiques to see individual decisions, or Guide differences only
to see where your action differed from a comparable guide recommendation.
Select Study spots to explore other decisions, including range-dependent calls.
Open a table to replay the hand. Reveal action & outcome shows the recorded
play and any specific alternative. The coverage count shows how many decisions
the guide could compare in this selection.
The small-blind solver reference covers six seats, four folds, no ante and
95–105 BB effective blinds. Its 100 BB reference uses 5% rake capped at 1 BB.
The guide compares fold versus continue when the model chooses one at least
99% of the time. With mixed frequencies it shows the percentage for study.
The other positions use authored guidelines. For first-in opens these consider
all four ranks, suit quality, position and effective stacks. A short or deep
player behind is identified explicitly. Facing a single open, the price and
the opener's possible range can leave more than one line worth studying.
Pot size orders examples within a critique rule.

An in-turn fold with a free check keeps the hand in the library. When Hero
does this, Action critiques can show the decision even if the fold was a
timeout. Reimport the original file to recover these hands from older imports.

Review patterns groups guide differences.
Each fraction counts decisions that differed from the guide out of all
comparable decisions with a recommendation. Recurring differences across
sessions come first, then the lower end of the descriptive 95% interval.
Compare decisions opens both differences and agreements at the original
action. Selected hands opens the individual hand shortlist.
Use Room, Street and Hands to filter. Last 5 sessions applies per room.
To review a specific day, choose **Hands → By date**, enter the date shown in
the hand histories (YYYY-MM-DD) and click **Review date**. The date starts at
yesterday. The review shows flagged hands for that date. Study
spots, guide differences and the replay links also use the selected day.
Reimporting an identical file makes its hands available once.
Import > Import history shows duplicates and exclusion reasons.
The title bar shows the application version. If startup fails, the error
dialog gives the location of the local diagnostic log. On Windows it is
normally %LOCALAPPDATA%\PLO Hero Leak Finder\errors.log.

## Reviewing a decision

Open a hand to see the table just before the selected decision. It shows
the board, pot, call price, and preceding actions. An orange panel marks
Hero. Known dealt cards are visible. Previous decision and Next decision
navigate the same hand. Play hand starts at the blinds and runs to the
result. Pause or change Speed, use the step buttons or arrow keys for a
single action, or click a street to jump to its first frame. Back to decision
returns to the spot you opened. Chips in front of a seat show its total live
bet on this street. Pot includes those chips. For raises, (+amount) is the
additional payment. Recorded payouts and known showdown cards appear at the end.
Use Amounts below the table to switch between the hand's currency and
big blinds (BB), including the pot, stacks, bets, call price and payouts.
Full hand history opens the original record at the selected action.
On any street, Reveal action & outcome shows what Hero did, later actions,
the final board, pot awards and opponent cards recorded in the source.
Verified Hero results show the net result in the selected unit. Hidden cards
are labeled as unrecorded. Hide action & outcome returns to the pre-decision view.

Guide differences only lists decisions where the guide's recommendation
differs from the played action in a supported guide situation.
Suggested bet sizes are illustrations. Review priorities reflect
study rules and repetition, without a solver or an estimate of EV lost.
Study spots opens conditional and manual decisions, largest pot first.
Use Large river calls after a turn call to review hands where you called
the same opponent on both streets. This filter does not grade the call.
Each entry shows the original action and opens the table at that decision.
Why review this first? explains the priority. Open Related hands to see
more examples of the same pattern. Previous and Next move between pages.
The app saves the review index and population comparisons locally.
While the index is being built, the review shows its progress.

## Range and History

Open Range and History on the flop or What would change this decision?
on the turn and river. The
display shows how observed actions change the weights of possible PLO4
holdings. The estimates use Hero's cards and the board visible at the
decision. On the flop and turn they also sample or enumerate later cards.

After a heads-up check to Hero, the flop and turn views compare checking
with multiple bet sizes. Model recommendation and recommendation with
history appear side by side. The history comparison uses earlier folds,
calls, and raises against Hero in comparable contexts. Changing a
recommendation requires at least 30 matched responses per size group
across three dates and a clear estimate gap. This individual-opponent
model uses the same sizing groups as the cash reports.
Smaller samples remain visible for review. The historical sizing comparison
assumes a fold to a raise and no further betting after a call. The heads-up turn scenarios
also compare calling the raise and facing a half-pot river bet on the same
possible runouts. The river call view gives the break-even equity, hand categories
and how much the modeled range would need to change.
River comparisons with personal history also require at least
30 comparable decisions across three dates. Hidden cards widen the
range of plausible outcomes. Use Open source to inspect earlier hands.

## Reports

Reports include breakdowns by position, street, bet size, hand class,
stack depth, stake, room, opponent, time period, and session. Each
frequency shows an action count and an eligible opportunity count.
Hand classes can overlap. A hand can be AAxx and double-suited.
Hover over a statistic or report cell for a short explanation. The 95%
interval describes sampling uncertainty around the displayed estimate.

Population review uses matched opponent decisions in your local library.
Postflop sizing groups use the bet as a fraction of the pot before the bet.
The upper bounds are 40%, 62.5%, 87.5% and 101%, followed by a group above
101%. For example, 74.9% and 75.1% both fall in the three-quarter-pot group.
The near-pot group includes up to 101% for rounding. The replay retains
the recorded amount. A group cannot identify which slider button was used.
Supported difference flags a frequency difference in similar situations.
Exploratory also displays small samples and spots without a matched
comparison. A frequency difference or a negative result can guide hand
review. Assessing a strategic mistake requires additional evidence.

Fourteen of 42 metrics have local matched comparisons. Two predefined
nodes qualify for the Supported label: a three-bet against one open
with no callers, and a flop bet in position after the preflop aggressor
checks. The reference is weighted to Hero's opportunities. Matching
uses network, currency, blinds, antes, player count, positions,
effective stack, prior action, and flop texture where applicable.

A supported finding requires at least 50 matched Hero decisions, 200
population decisions, 20 days on each side, and 80% coverage of Hero's
opportunities by cells with at least 20 population decisions. The
ranking interval accounts for six predefined comparisons and must lie
more than five percentage points from zero. The combined report requires
both networks and matching directions.

The PLO4 guide and the flop, turn, and opponent-history audits include
public source hands. Reports > BTN vs CO opens the qualitative guide or
the CPU pilot. Run BTN-CO pilot.cmd reruns the public pilot locally.

## Local data and supported games

Personal hands, aliases, and source paths are stored in
%LOCALAPPDATA%\PLO Hero Leak Finder\library.sqlite3. Close the app before
backing up its folder. The local database is unencrypted.

Analysis covers regular single-board PLO4 cash. Straddles, bomb pots,
multiple runouts, tournaments, and incomplete or inconsistent records
can be excluded. Most postflop frequencies use heads-up spots. Fold
versus flop c-bet remains descriptive while its definition is under
review. Recommendations use qualitative rules and modeled assumptions.
Solver verification and five-card Omaha are outside this release's scope.

## Testing

Run the suite from the extracted folder with the bundled Python.

```powershell
.\runtime\python.exe -B -m unittest discover -s tests -v
```

[Feature-coverage.html](../Feature-coverage.html) documents the metrics. [SOURCES.txt](../SOURCES.txt) lists the code
and public sample provenance.

## Luck and variance

Open **Reports → Luck and variance**. Room, stake (currency and big blind), date and active Hero aliases filter the table. Dates use YYYY-MM-DD and include both endpoints. The first visit indexes the library. Later visits reuse each hand's saved contribution. Imports also update the current selection's totals incrementally.

Each preflop opportunity is one deal. The two pocket-pair flop rows count a flop that hits the rank of an original pair. Flop rows default to hands where Hero saw the flop. Use the checkbox to include all recorded flops after preflop folds. Turn and river rows always include recorded cards after a Hero fold. Each draw is classified before the card is dealt. A single card can contribute to more than one target. Combined rows pool turn and river opportunities. The denominator is 45 unseen cards on the turn and 44 on the river. Only Hero's dealt pocket and the board so far remove cards from the deck.

Expected is the sum of the individual hit probabilities. Difference is observed minus expected, and Δ pp is that difference divided by opportunities, times 100. The z-score measures the difference in standard deviations. Below 30 expected hits the percentile uses the exact Poisson-binomial mid-distribution (half the probability of a tie). Larger samples use the normal approximation. A dash means the quantity is undefined. The thresholds are descriptive and the rows overlap. A parser alert opens examples for inspection. Opponent actions can select which boards get recorded. Luck has no bearing on the quality of your decisions.

Exclusions are grouped by reason. Older libraries may need their original files to identify the room, Hero, stake and date of rejected records. Unattributable records are shown separately.
