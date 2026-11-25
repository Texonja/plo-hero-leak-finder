# Heads-up PLO4 river pilot

Run from the project root on a CPU. The pilot uses the project's Omaha evaluator. Its numerical dependencies belong in a separate environment. The desktop application does not import the pilot.

```console
python -m pip install -r pilots/river_solver/requirements.txt
python -m unittest discover -s tests -p "test_river_solver*.py" -v
python -m pilots.river_solver.run --out river-audit --workers 6
python -m pilots.river_solver river-audit/inputs/river_set_small_lead-00-500-middle-middle.json one-result.json
```

Open `river-audit/Izvestaj.html`. Each case links to its input and outputs. The batch runs the validation gate before reading the demo. A failed or skipped test stops the run. One BLAS thread is used per process. The pinned wheels match the tested Python 3.14 environment.

## Input and payoff conventions

Each input includes five board cards, the starting pot and effective stack in BB, and two lists of four-card combinations with positive relative weights. Suits use c, d, h and s. Cards may overlap between the two lists. The engine removes impossible joint deals and normalizes the remaining product of the two priors. It evaluates every listed holding using exactly two hole cards and three board cards. The dense implementation accepts at most sixteen million pairs before blocker removal.

Bet fractions refer to the starting river pot. Raise fractions refer to the pot after matching the previous bet. Action labels give total river commitment in BB. A short all-in is legal at the stack cap. The tree clips full pot raises to that cap, removes duplicated sizes and enforces the previous full raise increment. A custom tree can specify the raise cap, disable all-ins or limit IP betting after a check. The supplied audit trees permit two raises. Root action belongs to OOP.

The query specifies a path of action labels from the river root and the acting player's four-card holding. Root EV includes the starting pot and is net of further investments. Folding after betting can therefore have a negative river EV. No rake is charged. The two players' root EVs sum to the starting pot.

## Precision and interpretation

Full-width alternating CFR+ uses linearly weighted average policies. Each best response respects the responding player's information sets and all card blockers. Exploitability is half the sum of the two players' unilateral gains. The batch tests the whole sum against its convergence threshold, checks Hero's continuation against a fixed opponent and repeats each case at finer precision. Global exploitability does not certify an individual rare branch.

Determinism covers strategy, EV, iteration count and solution hash with the same numerical environment and stopping settings. Elapsed time is a separate observation. `run.py` records both coarse and fine times. To reproduce one exact batch solution hash with the Python API, pass the input, the recorded fine target and maximum iterations, and `minimum_iterations=coarse_iterations+100`.

Each player starts with all 178,365 four-card holdings legal on the five-card board. The preflop structure and the observed sequence of bets, raises, calls and checks through the turn determine a likelihood for each hand. The input lists the model coefficients, profile-specific support, discarded mass and full/represented category composition. The exact queried hand occupies its own group. Profiles are research assumptions.

The frozen manifest specifies the 102 starting situations. All eight demo hands have nine opponent/Hero profile pairs. Constructed situations use the three matched profile pairs. Three sensitivity cases compare 500 and 1,500 representatives per seat. A change of direction or best legal action triggers 2,250 representatives across the corresponding profile combinations. The report retains both signed EV and material changes. Computation uses one numerical thread per solve.

The original dense terminal operation is available by default. For larger ranges compile the optional exact card-removal kernel from a Visual Studio x64 Native Tools prompt and set the absolute DLL path in PowerShell before running:

```console
cl /O2 /LD /EHsc pilots/river_solver/terminal.cpp /Fe:river_terminal.dll
```

```powershell
$env:RIVER_TERMINAL_LIBRARY=(Resolve-Path river_terminal.dll).Path
```

On Linux compile with `c++ -O3 -shared -fPIC pilots/river_solver/terminal.cpp -o terminal.so` and point the same environment variable to the shared object. To reproduce the numerical hash for a saved result, use the same backend and environment and set `--target`, `--iterations` and `--min-iterations` from its target, maximum and recorded coarse iteration count plus 100.

The optional local cohort audit reads SQLite in read-only mode. Its output contains aggregates. To include more confirmed historical aliases use the optional alias argument shown by `python -m pilots.river_solver.population --help`. Make the full public benchmark with `python -m pilots.river_solver.run --out river-audit --workers 6`. Reanalyze from a saved spec with `python -m pilots.river_solver INPUT.json OUTPUT.json`. An existing first-round directory may be passed with `--previous`. The generated report links to its validation inputs and solver outputs.

## Round three

Use the round-two output directory as the reference for round three. Fit the coefficients from your local SQLite library with `python -m pilots.river_solver.calibration LIBRARY OUT`. Use the optional `--alias ROOM:NAME` argument for another confirmed owner alias. Each solver phase below accepts `--workers N`. Keep the optional CPU kernel enabled for practical runtime and use fewer simultaneous workers for the 4,000-hand controls.

```console
python -m pilots.river_solver.round3 OUT ROUND2_REFERENCE series
python -m pilots.river_solver.round3 OUT ROUND2_REFERENCE grid
python -m pilots.river_solver.round3 OUT ROUND2_REFERENCE validation
python -m pilots.river_solver.round3 OUT ROUND2_REFERENCE convergence --workers 3
python -m pilots.river_solver.round3 OUT ROUND2_REFERENCE lp --workers 2
python -m pilots.river_solver.round3_report OUT ROUND2_REFERENCE
```

The coefficient scenarios are recoverable from the saved design, public snapshot manifest and fit. Their exact range and input hashes are stored alongside the EVs. Validation inputs use seat roles instead of screen names. The library snapshot itself stays local.
