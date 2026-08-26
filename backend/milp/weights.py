"""
GridShield -- Feeder dependency configuration for MILP.
Maps a dependent feeder to the feeder that must be energized first
(e.g. a downstream feeder that only makes sense once its upstream
switch/feeder is live). Edit this dict as the topology grows.

Note: priority weights, criticality tiers, and min-safe-fractions are
computed once in state/schema.py from PostGIS data, and are attached
directly to each feeder dict. They are not duplicated here to avoid
two sources of truth drifting apart.
"""

DEPENDENCIES = {
    # "dependent_feeder_id": "provider_feeder_id",
}