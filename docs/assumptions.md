# Assumptions

This page tracks current assumptions that operators or developers should
know about. Keep this file short; resolved or obsolete assumptions should
be removed instead of preserved indefinitely.

## Current

- `exit.time_exit_utc` is a legacy field name. Current behavior treats it
  as America/New_York local time.
- The dashboard is a SQLite command and monitoring plane. It must not
  import the broker or call execution components directly.
- Dashboard commands are durable and may survive runner restart until
  processed.
- Base configuration changes require runner restart unless explicitly
  routed through the dashboard strategy override path.
- Locked mode blocks entries but should preserve exits, cancel commands,
  flatten commands, reconciliation, and dashboard state publishing.
- Some strategy providers currently receive the broker client for market
  data access. They must not submit, cancel, or manage orders.
