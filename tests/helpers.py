"""A stand-in for the claude_agent_sdk module: records what it is given, runs nothing."""
import types


class _Options:
    def __init__(self, **kw):
        self.kw = kw


class _Allow:
    def __init__(self, updated_input=None):
        self.updated_input = updated_input


class _Deny:
    def __init__(self, message=''):
        self.message = message


def stub_sdk():
    sdk = types.SimpleNamespace(ClaudeAgentOptions=_Options, PermissionResultAllow=_Allow, PermissionResultDeny=_Deny)

    def tool(name, description, schema):
        def deco(fn):
            fn.tool_name = name
            return fn
        return deco

    sdk.tool = tool
    sdk.create_sdk_mcp_server = lambda name, tools=(): types.SimpleNamespace(name=name, tools=list(tools))
    return sdk
