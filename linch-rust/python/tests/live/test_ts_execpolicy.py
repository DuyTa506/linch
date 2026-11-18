"""Live toolsec: a tenant cannot choose its own execution policy (`allow_root`, image, network,
limits).

Expectations are in `expectations/toolsec.md` id 2.9. The docker daemon is not reachable by the test
user, so a docker entry is exercised at admission only.
"""

from __future__ import annotations

from typing import Any

from ts_support import (
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    serving,
    skip_dangerous,
    tenant,
    text_of,
    workspace,
)

__all__ = ["pytestmark"]

AREA = "tenants"
BOXED, DOCKED, PLAIN = "tok-boxed-ts-0001", "tok-docked-ts-0001", "tok-plain-ts-0001"


@needs_server
async def test_a_tenant_cannot_choose_its_own_execution_policy() -> None:
    model = await plan_model([("Bash", {"command": "echo inside-tenant-sandbox"})]).start()
    outcomes: dict[str, Any] = {}
    try:
        with workspace() as root:
            allow: dict[str, Any] = {
                "exec_tools": True,
                "provider_base_urls": [model.url],
                "workspace_roots": [str(root)],
                "permission_modes": ["default", "skip_dangerous"],
            }
            tenants = {
                "boxed": tenant(BOXED, **allow, execution=[{"backend": "sandbox"}]),
                "docked": tenant(DOCKED, **allow, execution=[{"backend": "docker"}]),
                "plain": tenant(PLAIN, **allow),
            }
            async with serving(tenants) as proc:

                async def attempt(token: str, namespace: str, label: str, **top: Any) -> None:
                    model.requests.clear()
                    async with proc.connect(token) as server:
                        config = fake_config(
                            model,
                            namespace=namespace,
                            workspace=str(root),
                            permissions=skip_dangerous(root),
                            **top,
                        )
                        try:
                            agent = await server.open_agent(config)
                        except Exception as error:
                            kind = getattr(error, "kind", None)
                            outcomes[label] = (type(error).__name__, kind, str(error)[:200])
                            return
                        envelopes = await finish(agent, await agent.run("s1", "go"))
                        end = ends(envelopes, "Bash")
                        outcomes[label] = (
                            "opened",
                            end[0].error_code if end else None,
                            text_of(end[0])[:200] if end else None,
                        )

                await attempt(BOXED, "boxed", "sandbox_default_first_entry")
                await attempt(
                    BOXED,
                    "boxed",
                    "sandbox_network_true",
                    execution={"backend": "sandbox", "network": True},
                )
                await attempt(
                    BOXED,
                    "boxed",
                    "sandbox_bigger_memory",
                    execution={"backend": "sandbox", "memory_bytes": 1 << 40},
                )
                await attempt(BOXED, "boxed", "local_instead", execution={"backend": "local"})
                await attempt(
                    DOCKED,
                    "docked",
                    "docker_allow_root",
                    execution={"backend": "docker", "allow_root": True},
                )
                await attempt(
                    DOCKED,
                    "docked",
                    "docker_image",
                    execution={"backend": "docker", "image": "evil:latest"},
                )
                await attempt(
                    DOCKED,
                    "docked",
                    "docker_network_host",
                    execution={"backend": "docker", "network": "host"},
                )
                await attempt(
                    DOCKED, "docked", "docker_exact_entry", execution={"backend": "docker"}
                )
                await attempt(PLAIN, "plain", "plain_sandbox", execution={"backend": "sandbox"})
                await attempt(PLAIN, "plain", "plain_local", execution={"backend": "local"})
    finally:
        await model.stop()
    record(AREA, "2.9-execution-policy", **outcomes)
    assert outcomes["sandbox_default_first_entry"][0] == "opened"
    assert "inside-tenant-sandbox" in outcomes["sandbox_default_first_entry"][2]
    for label in (
        "sandbox_network_true",
        "sandbox_bigger_memory",
        "local_instead",
        "docker_allow_root",
        "docker_image",
        "docker_network_host",
        "plain_sandbox",
    ):
        assert outcomes[label][:2] == ("NotAllowed", "execution"), (label, outcomes[label])
    assert outcomes["plain_local"][0] == "opened"
