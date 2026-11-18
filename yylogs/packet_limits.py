"""Packet collection bounds; legacy Hook capture limits remain unchanged."""
SESSION_SECONDS = 4 * 60 * 60
# Allow decoder boundary/finalization timestamp jitter at the session limit.
MAX_BATTLE_SECONDS = SESSION_SECONDS + 5
MAX_BATTLE_MS = MAX_BATTLE_SECONDS * 1000
