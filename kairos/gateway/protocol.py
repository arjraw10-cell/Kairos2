"""Message type constants for the gateway WebSocket protocol.

Every message is JSON with a 'type' field matching one of these constants.
"""


class ClientMsg:
    """Messages sent from client → server."""
    CONNECT = "connect"                # {type, session_id?}
    NEW_SESSION = "new_session"        # {type, workspace}
    LOAD_SESSION = "load_session"      # {type, session_id}
    UNLOAD_SESSION = "unload"          # {type}
    LIST_SESSIONS = "list_sessions"    # {type}
    LIST_WORKSPACES = "list_workspaces"  # {type}
    MESSAGE = "message"                # {type, session_id, content, image_url?}
    INTERRUPT = "interrupt"            # {type, session_id}
    STOP = "stop"                      # {type, session_id}
    COMPACT = "compact"                # {type, session_id}
    PING = "ping"                      # {type}


class ServerMsg:
    """Messages sent from server → client."""
    CONNECTED = "connected"                        # {type, session_id, workspace, history?, workspaces}
    NEW_SESSION_CREATED = "new_session_created"    # {type, session_id, workspace}
    SESSIONS_LIST = "sessions_list"                # {type, sessions: [{id, timestamp, workspace, preview}]}
    WORKSPACES_LIST = "workspaces_list"            # {type, workspaces: [str]}
    STREAM_START = "stream_start"                  # {type, session_id}
    STREAM_TOKEN = "stream_token"                  # {type, session_id, content}
    TOOL_CALL = "tool_call"                        # {type, session_id, name, args, summary}
    STREAM_END = "stream_end"                      # {type, session_id, content, has_tool_calls}
    DONE = "done"                                  # {type, session_id, response}
    TOKEN_UPDATE = "token_update"                  # {type, session_id, session_input, session_output, context_pct, turn_input, turn_output}
    COMPACTED = "compacted"                        # {type, session_id, message}
    UNLOADED = "unloaded"                          # {type, session_id}
    ERROR = "error"                                # {type, message}
    PONG = "pong"                                  # {type}
    EXIT = "exit"                                  # {type}
