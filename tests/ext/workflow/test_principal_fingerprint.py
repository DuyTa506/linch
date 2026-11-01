"""Principal roles use canonical JSON in durable workflow call identities."""

import json
from collections.abc import Iterable
from typing import Any


def test_principal_roles_are_canonical_in_workflow_call_fingerprint() -> None:
    from linch import Principal, RunOptions
    from linch.ext.workflow.context import _run_options_fingerprint

    options = RunOptions(principal=Principal("tenant", "user", frozenset({"reader", "editor"})))

    payload = json.loads(_run_options_fingerprint(options))

    assert payload["principal"] == {
        "tenant": "tenant",
        "user": "user",
        "roles": ["editor", "reader"],
    }


def test_profile_contract_is_canonical_in_workflow_call_fingerprint() -> None:
    from linch import Profile, RunOptions
    from linch.ext.workflow.context import _run_options_fingerprint

    class Plugin:
        name = "search"
        version = "1"
        provides: Iterable[str] = ()
        requires: Iterable[str] = ()

        def setup(self, ctx: Any, config: Any = None) -> Any:
            from linch.kernel import Disposable

            return Disposable()

    profile = Profile(
        "support",
        "1",
        plugins={"search": (Plugin(), {"endpoint": "tenant-search"})},
    )

    payload = json.loads(_run_options_fingerprint(RunOptions(profile=profile)))

    assert payload["profile"] == profile._contract()
