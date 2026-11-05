"""Routing benchmark: the real gateway in front of a simulated fleet of model hosts.

The gateway under test is the production one: token verification, limits, the backend pool
and its strategies, and the streaming meter all run unmodified. Only the model hosts are
simulated, so a routing strategy is compared on what it actually decides rather than on a
re-implementation of it. See ``README.md`` for the model, its assumptions, and its limits.
"""
