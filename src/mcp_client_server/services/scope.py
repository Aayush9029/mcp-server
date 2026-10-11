from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Principal
from mcp_client_server.store import AccountTasks, KeyTasks, TaskScope


def own_scope(principal: Principal) -> TaskScope:
    """What a credential can read: the data of its key, or of its full account."""
    match principal:
        case AgentPrincipal():
            return KeyTasks(principal.key_id)
        case AccountPrincipal():
            return AccountTasks(principal.account_id)
