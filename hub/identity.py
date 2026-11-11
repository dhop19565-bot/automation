"""Demo identity.

THIS IS NOT AUTHENTICATION. The caller names a user in the ``X-Hub-User``
header and is believed. It exists so the maker-checker controls have two
different people to tell apart in a demo. Before real data goes anywhere near
this, replace ``resolve_user`` with verified sign-in (for example Entra ID) and
take roles from the identity provider.
"""
from __future__ import annotations

from dataclasses import dataclass

ROLE_PREPARER = "preparer"
ROLE_APPROVER = "approver"


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    roles: frozenset[str]

    def has(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> dict:
        return {"id": self.user_id, "name": self.name, "roles": sorted(self.roles)}


DEMO_USERS: dict[str, User] = {
    u.user_id: u
    for u in (
        User("alex.preparer", "Alex Morgan (Financial Accountant)", frozenset({ROLE_PREPARER})),
        User("sam.controller", "Sam Okafor (Financial Controller)", frozenset({ROLE_PREPARER, ROLE_APPROVER})),
        User("jo.reviewer", "Jo Lindqvist (Group Reporting Manager)", frozenset({ROLE_APPROVER})),
    )
}


def resolve_user(user_id: str | None) -> User | None:
    return DEMO_USERS.get(user_id or "")
