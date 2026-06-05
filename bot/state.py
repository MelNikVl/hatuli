"""Mutable runtime state shared between scheduler and admin web panel.

All coroutines run in the same asyncio event loop (via asyncio.gather in
main.py), so module-level variables here are safely shared without locks.
"""

# Whether the parser loop is allowed to fire.  Set to False via admin panel
# to pause scraping without killing the process.
parser_enabled: bool = True

# Random delay range between parser cycles (seconds).  Admin panel can
# update these in real-time; the scheduler reads them at the start of each
# sleep, so changes take effect on the next cycle.
parse_interval_min: int = 60
parse_interval_max: int = 300
