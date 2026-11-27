# 0.3.17 beta

- iPoker imports use the original hand's number format to distinguish thousands from decimals. The importer flags amounts whose separators cannot be resolved.
- Called all-ins require a complete board. Hands won by a fold can still end on any street.
- Heads-up flop and turn opponent history groups bets by the amount the caller can cover. Bets that put either player all-in have their own group.

When you open an older library, the app saves a backup and checks the stored hands once. To retry older iPoker hands that were excluded because the chip amounts failed validation, import the original folder or backup again.
