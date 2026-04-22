"""silicon-system-test — unattended end-to-end fuzz testing framework.

Spawns a throwaway silicon-serve, drives N concurrent matches with
random-action agents, collects logs + replays into a timestamped
bundle for a /review-system-test skill to triage. See
~/dev/system-test-plan.md for the full design rationale.
"""
