"""Interactive operator bootstrap, with no password accepted in command arguments."""

import getpass

from .auth import AuthService
from .db import create_database


async def bootstrap_admin(settings, tenant, login):
    password = getpass.getpass("New administrator password, 12–256 characters: ")
    if password != getpass.getpass("Confirm password: "):
        raise ValueError("Passwords did not match")
    database = create_database(settings.runtime_database_url())
    try:
        await database.check_schema()
        actor = await AuthService(database, settings).bootstrap(tenant, login, password)
        return {"status": "administrator_created", "tenant_id": actor.tenant_id}
    finally:
        await database.close()
