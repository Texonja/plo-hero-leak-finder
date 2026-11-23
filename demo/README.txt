HeroDemo contains 320 sanitized CoinPoker hands and 320 sanitized iPoker hands.

The CoinPoker records come from cash_example.txt at revision 6e50528 of
https://github.com/Texonja/coinpoker-population-strength-analyzer
The iPoker XML records come from database.pghembk at revision 2956860 of
https://github.com/Texonja/plo-multiaccount-detector

The sample uses eight blocks of 40 accepted hands per network, spaced through the
public corpora. The recording player's public alias was replaced with HeroDemo.
The iPoker XML records are packaged in a small HM2-compatible COPY excerpt.
The original public hand numbers, poker actions and cards are retained. Sessions
and trends in these samples describe the selected public hands only.

The desktop application opens these samples in a separate local demo database.
Use Import > Import files… for your own completed hands and database backups.
