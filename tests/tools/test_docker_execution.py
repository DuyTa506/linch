from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def docker(tmp_path):
    """Run real CLI processes without a Docker daemon or image downloads."""
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / "calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "run":
    (root / "container").write_text(args[args.index("--name") + 1])
    (root / "pid").write_text(str(os.getpid()))
    os.execv("/bin/sh", ["sh", "-c", args[-1]])
if args[0] == "rm":
    try:
        os.kill(int((root / "pid").read_text()), 0)
        (root / "removed-before-cli-exit").touch()
    except ProcessLookupError:
        pass
    (root / "rm-started").touch()
    if (root / "rm-delay").exists():
        time.sleep(0.2)
    (root / "container").unlink(missing_ok=True)
"""
    )
    executable.chmod(0o700)
    return str(executable)


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]


async def wait_for_file(path):
    async with asyncio.timeout(2):
        while not path.exists():
            await asyncio.sleep(0.005)


async def test_default_limits_reach_docker_cli(docker, tmp_path):
    # Missing limits at the CLI boundary would silently weaken confinement.
    from linch.tools.execution import DockerBackend

    result = await DockerBackend(docker_path=docker).run(
        "echo docker-out", cwd=str(tmp_path), timeout_s=2
    )
    assert result.stdout == "docker-out\n" and result.returncode == 0
    args = calls(tmp_path)[0]
    assert args[:3] == ["run", "--rm", "--name"]
    assert args[3].startswith("linch-")
    assert args[4:] == [
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "512",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-w",
        str(tmp_path),
        "-v",
        f"{tmp_path}:{tmp_path}",
        "python:3.12-slim",
        "sh",
        "-c",
        "echo docker-out",
    ]


async def test_none_disables_limits_and_existing_policy_args_survive(docker, tmp_path, monkeypatch):
    from linch.tools.execution import DockerBackend

    monkeypatch.setenv("FORWARDED_TOKEN", "host-secret")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    backend = DockerBackend(
        docker_path=docker,
        image="alpine:3.20",
        network="none",
        workspace_mount="ro",
        read_only_root=True,
        tmpfs=("/tmp:size=64m",),
        env={"APP_MODE": "test"},
        forward_env=("FORWARDED_TOKEN", "MISSING_TOKEN"),
        user="1000:1000",
        memory=None,
        cpus=None,
        pids_limit=None,
        cap_drop=None,
        no_new_privileges=None,
        resume_fingerprint_key=b"k" * 16,
    )
    for option in ("memory", "cpus", "pids_limit", "cap_drop", "no_new_privileges"):
        assert backend.resume_policy_config[option] is None
    await backend.run("id", cwd=str(tmp_path), timeout_s=2)
    assert calls(tmp_path)[0][4:] == [
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:size=64m",
        "--env",
        "APP_MODE",
        "--env",
        "FORWARDED_TOKEN",
        "--user",
        "1000:1000",
        "-w",
        str(tmp_path),
        "-v",
        f"{tmp_path}:{tmp_path}:ro",
        "alpine:3.20",
        "sh",
        "-c",
        "id",
    ]


@pytest.mark.parametrize(
    ("option", "value", "cli"),
    [
        ("memory", "512m", ["--memory", "512m"]),
        ("cpus", 0.5, ["--cpus", "0.5"]),
        ("pids_limit", 12, ["--pids-limit", "12"]),
        (
            "cap_drop",
            ("NET_RAW", "SYS_ADMIN"),
            ["--cap-drop", "NET_RAW", "--cap-drop", "SYS_ADMIN"],
        ),
        ("no_new_privileges", False, []),
    ],
)
async def test_custom_limits_affect_cli_and_resume_policy(docker, tmp_path, option, value, cli):
    from linch.tools.execution import DockerBackend

    default = DockerBackend(docker_path=docker)
    backend = DockerBackend(docker_path=docker, **{option: value})
    assert backend.resume_policy_version == "2"
    assert default.resume_policy_config[option] != backend.resume_policy_config[option]
    assert backend.resume_policy_config[option] == value
    await backend.run("true", cwd=str(tmp_path), timeout_s=2)
    args = calls(tmp_path)[0]
    if cli:
        assert args[args.index(cli[0]) : args.index(cli[0]) + len(cli)] == cli
    else:
        assert "--security-opt" not in args


async def test_start_streams_bounded_output_with_closed_stdin(docker, tmp_path):
    from linch import ShellProcess, ShellStarter
    from linch.tools.execution import DockerBackend

    backend = DockerBackend(docker_path=docker)
    assert isinstance(backend, ShellStarter)
    handle = await backend.start(
        "cat; echo ready; sleep .2; head -c 40000 /dev/zero", cwd=str(tmp_path)
    )
    assert isinstance(handle, ShellProcess)
    assert await handle.wait(0.12) is None
    assert handle.read().stdout == "ready\n"
    result = await handle.wait(2)
    assert result is not None and not result.timed_out and result.truncated
    assert len(result.stdout) < 30100
    assert Path(result.stdout_path).stat().st_size == 40006


@pytest.mark.parametrize("cause", ["timeout", "abort", "cancel"])
async def test_interruption_preserves_output_and_removes_container(docker, tmp_path, cause):
    from linch.abort import AbortContext
    from linch.tools.execution import DockerBackend

    signal = AbortContext()
    backend = DockerBackend(docker_path=docker)
    task = asyncio.create_task(
        backend.run(
            "echo earlier; exec sleep 10",
            cwd=str(tmp_path),
            timeout_s=0.2 if cause == "timeout" else 5,
            signal=signal,
        )
    )
    await wait_for_file(tmp_path / "pid")
    await asyncio.sleep(0.03)
    if cause == "abort":
        signal.abort()
    elif cause == "cancel":
        task.cancel()
    if cause == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.stdout == "earlier\n"
        assert result.timed_out is (cause == "timeout")
        assert result.aborted is (cause == "abort")
        assert result.signal == 15
    args = calls(tmp_path)
    assert args[1] == ["rm", "-f", args[0][3]]
    assert not (tmp_path / "container").exists()
    assert not (tmp_path / "removed-before-cli-exit").exists()


async def test_kill_after_cli_exit_still_removes_container(docker, tmp_path):
    from linch.tools.execution import DockerBackend

    handle = await DockerBackend(docker_path=docker).start("echo done", cwd=str(tmp_path))
    assert (await handle.wait(2)).stdout == "done\n"
    await handle.kill()
    assert not (tmp_path / "container").exists()
    assert calls(tmp_path)[1] == ["rm", "-f", calls(tmp_path)[0][3]]


@pytest.mark.parametrize("first_cause", ["timeout", "cancel"])
async def test_repeated_cancellation_finishes_container_removal(docker, tmp_path, first_cause):
    from linch.tools.execution import DockerBackend

    (tmp_path / "rm-delay").touch()
    backend = DockerBackend(docker_path=docker)
    task = asyncio.create_task(
        backend.run(
            "echo partial; exec sleep 10",
            cwd=str(tmp_path),
            timeout_s=0.15 if first_cause == "timeout" else 5,
        )
    )
    await wait_for_file(tmp_path / "pid")
    if first_cause == "cancel":
        task.cancel()
    await wait_for_file(tmp_path / "rm-started")
    task.cancel()
    await asyncio.sleep(0.03)
    task.cancel()
    if first_cause == "timeout":
        result = await task
        assert result.timed_out and result.stdout == "partial\n"
    else:
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not (tmp_path / "container").exists()
    assert not (tmp_path / "removed-before-cli-exit").exists()


async def test_env_values_stay_out_of_argv_and_reach_spawn_env(docker, tmp_path, monkeypatch):
    from linch.tools import execution
    from linch.tools.execution import DockerBackend

    monkeypatch.setenv("FORWARDED_TOKEN", "host-secret-value")
    spawned: list[tuple[tuple[str, ...], dict]] = []
    real_spawn = asyncio.create_subprocess_exec

    async def fake_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", fake_spawn)
    backend = DockerBackend(
        docker_path=docker,
        image="alpine:3.20",
        env={"APP_KEY": "app-secret-value"},
        forward_env=("FORWARDED_TOKEN",),
        resume_fingerprint_key=b"k" * 16,
    )
    await backend.run("true", cwd=str(tmp_path), timeout_s=2)

    args, kwargs = next((a, k) for a, k in spawned if a[1] == "run")
    assert "app-secret-value" not in " ".join(args)
    assert "host-secret-value" not in " ".join(args)
    flagged = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert flagged == ["APP_KEY", "FORWARDED_TOKEN"]
    assert kwargs["env"]["APP_KEY"] == "app-secret-value"
    assert kwargs["env"]["FORWARDED_TOKEN"] == "host-secret-value"
