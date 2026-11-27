# PLO Hero Leak Finder

PLO Hero Leak Finder is a desktop app for offline PLO4 hand review on CoinPoker and iPoker, and your hands never leave your computer.

**0.3.17 beta** · [Release notes](docs/RELEASE-0.3.17-beta.md) · [Latest public download for Windows](https://github.com/Texonja/plo-hero-leak-finder/releases/latest)

![Leak Finder](docs/screenshots/main.png)

## What it finds

- **Decisions worth replaying.** Action critiques flag spots such as a check-back with a possible value bet or a costly call, and show what could make your original line reasonable.
- **Recurring spots.** Review patterns groups decisions with comparable spots and highlights differences from the guide. It shows the number of opportunities and whether differences recur across sessions.
- **Questions about ranges.** On supported postflop lines, inspect example value hands and bluffs, see the equity you need to call, or compare bet sizes under explicit assumptions.
- **Trends and comparisons.** Cash reports break down action frequencies and sample sizes by position, street, bet size, hand class, and session. Population review compares similar decisions in your local library. Luck and variance compares your deals and draw completions with their exact PLO4 card probabilities.

In HeroDemo's CoinPoker hand `9000004202`, Hero called **45.6 BB** on a river with four hearts and held no heart to block a flush. The call needed **31.8% equity before rake**. The app asks you to compare calling with folding and explains what could justify the call. Open the replay at the river decision to see the original board, bet, and action, then reveal what happened.

## Coverage, honestly

On HeroDemo, the guide compares **26 of 1,008 indexed decisions (2.6%)** and presents prompts as study questions that leave any solver assessment to a separate analysis.

## Quality

The source suite has **321 tests** covering imports, chip accounting, replay, reports, and the standalone river pilot. Selected parsing and hand-strength results are cross-checked against the separate engines and source records listed in SOURCES.txt. The tests also check that incremental review updates agree with a fresh pass through the hands.

## Quick start

1. Download the Windows x64 ZIP from the [latest release](https://github.com/Texonja/plo-hero-leak-finder/releases/latest). Right-click the ZIP in File Explorer and choose **Extract all**.
2. Open the extracted **PLO Hero Leak Finder** folder and double-click **START.BAT**. The package includes Python.
3. Open **Library → HeroDemo** to try 640 sample hands from both networks. For your own completed hands, use **Import → Import files** or **Import → Import folder**. Set your screen names under **Player → Personal Hero aliases**, one per line under the correct network.

## Run from source

With Python and Tk installed, run this command from the repository root.

```console
python launch.pyw
```

[User guide](docs/USER-GUIDE.md) · [Sources and sample provenance](SOURCES.txt) · [Standalone river solver pilot](https://github.com/Texonja/plo-hero-leak-finder/blob/main/pilots/river_solver/README.md)
