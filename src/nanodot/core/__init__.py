"""Core — domain logic: task store, state machine, memory, activity, permissions.

Core is pure with respect to infrastructure: it talks only to the protocols
in ``nanodot.ports`` and the standard library, never to ``nanodot.native``
or any SDK. Adapters execute; core decides.
"""
