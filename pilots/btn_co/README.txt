BTN vs CO PLO4 CPU pilot

Open BTN-vs-CO.html for the ten-hand report. The report runs offline and
includes the complete source record alongside each table. hands.json
contains the public source hands, locators, hashes, and decision snapshots.
results.json contains the saved simulations.

On Windows, run Run BTN-CO pilot.cmd from the extracted application.
The default run uses four CPU processes. The report opens when it finishes
and is saved under %LOCALAPPDATA%\PLO Hero Leak Finder\BTN CO pilot.
The command below lets you choose a different process count or output directory.

runtime\python.exe -B -m pilots.btn_co.run_pilot --jobs 2 --output-dir pilot-rerun

The saved run uses eight scenarios per hand and three independent batches
of 100,000 accepted deals per scenario, conditional on the CO opening.
Fold has a value of zero at the decision. Call and pot 3-bet use paired
deals and policy draws. See How to read the results for rake, allowed
sizes, and tree limits. The report shows modeled EV differences in big
blinds. Strategic and solver validation would require separate evidence.
validation.json checks forecasts of later actions against a public
holdout. validate_behavior.py repeats that check with the same library.

sim.cpp builds as a standalone C++17 program. The Windows x64 executable
uses a statically linked C++ runtime. Build it in a Visual Studio x64
Native Tools Command Prompt.

cl /nologo /EHsc /O2 /MT /std:c++17 /utf-8 sim.cpp /Fe:sim.exe

Use the following command with GCC or Clang.

c++ -std=c++17 -O2 sim.cpp -o sim

Run the Windows self-test from this folder.

sim.exe --self-test

select_hands.py repeats the selection from an imported public test
library with the matching Hero profile. --verify-originals checks source
records against cash_example.txt and database.pghembk. The shipped pilot
runs offline with its bundled public sample.
