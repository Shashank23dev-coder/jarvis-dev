"""Host-issued owner capability; model arguments and conversation text are not authority.

The authenticated HTTP perimeter issues this context. Trusted operator processes
may issue it explicitly. The private broker mailbox is a host-to-host transport,
not a public API; the broker additionally pins every operation to configured dev.
"""
from contextvars import ContextVar
from typing import Literal

from tools.schemas import StrictModel
from tools.policy import ToolDenied


class OwnerDevAuthority(StrictModel):
    principal: Literal['owner'] = 'owner'
    workspace: Literal['configured_dev'] = 'configured_dev'
    capability: Literal['engineering_development'] = 'engineering_development'


current_owner = ContextVar('engineering_owner', default=None)


def require_owner(authority):
    # Serialized fields are audit metadata, never an authentication credential.
    # Only trusted perimeter/operator code can supply the in-process capability.
    if not isinstance(authority, OwnerDevAuthority):
        raise ToolDenied('Authenticated owner development capability required')
    return authority
