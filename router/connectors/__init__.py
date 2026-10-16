"""Tool adapters: the SHAPE of each device or account the router reaches.

Each module has the same three parts:

    a Backend protocol     what the adapter needs from the outside world (a few methods)
    a real backend         a small HTTP / CLI / adb client for the common case
    register(reg, backend) adds the tools to a ToolRegistry, with writes= and tier= set

The real backends are thin and are NOT exercised against real devices in this repo. The fakes in
router/fakes.py implement every Backend protocol, so the adapters, the gate and the worker are all
tested offline. A new connector is one module in this shape.
"""
