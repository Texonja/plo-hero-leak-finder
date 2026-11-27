HeroDemo contains 320 sanitized CoinPoker hands and 324 sanitized iPoker hands.

The CoinPoker records come from cash_example.txt at revision 6e50528 of
https://github.com/Texonja/coinpoker-population-strength-analyzer
The first 320 iPoker records come from database.pghembk at revision 2956860 of
https://github.com/Texonja/plo-multiaccount-detector

The original 640 hands use eight blocks of 40 accepted hands per network, spaced through the
public corpora. The recording player's public alias was replaced with HeroDemo.
Those iPoker XML records are packaged in a small HM2-compatible COPY excerpt.
The original public hand numbers, poker actions and cards are retained. Sessions
and trends in these samples describe the selected public hands only.

Four additional iPoker hands in iPoker-HeroDemo-Review.xml illustrate two
preflop and two flop decisions. They preserve the cards, stacks, betting and
results. Player names, hand numbers, table, session and dates are fictional.

The desktop application opens these samples in a separate local demo database.
Use Import > Import files… for your own completed hands and database backups.
