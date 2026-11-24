"""Runs one agent invocation in a hardened, single-use sandbox (plan.md §7, §8).

Per event we create a private *internal* Docker network (no route out), attach only the gateway to it
(alias `gateway`), and start the agent container on it. There are no bind mounts, no published ports,
no capabilities, a read-only root filesystem, and resource limits. Everything is torn down afterwards.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool

SANDBOX_USER = "1000:1000"
LABEL = "harness.sandbox"


@dataclass
class RunResult:
    exit_code: int | None
    timed_out: bool
    logs: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SubnetPool:
    """Hands out distinct /28s so concurrent per-event networks never collide or exhaust Docker's default pool."""

    def __init__(self, cidr: str = "10.200.0.0/16"):
        self._free = list(ipaddress.ip_network(cidr).subnets(new_prefix=28))
        self._lock = threading.Lock()

    def take(self) -> str:
        with self._lock:
            if not self._free:
                raise RuntimeError("sandbox subnet pool exhausted")
            return str(self._free.pop(0))

    def give(self, subnet: str) -> None:
        with self._lock:
            self._free.append(ipaddress.ip_network(subnet))


def hardening(agent: dict, image: str, env: dict, network: str, name: str, runtime: str | None, command: list[str] | None = None) -> dict:
    """The container create-arguments. Pure function so tests can assert the security posture without Docker."""
    sb = agent.get("sandbox") or {}
    kw = dict(
        image=image, name=name, detach=True, user=SANDBOX_USER, environment=env, network=network,
        read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        pids_limit=128, mem_limit=f"{int(sb.get('memory_mb', 256))}m", memswap_limit=f"{int(sb.get('memory_mb', 256))}m",
        nano_cpus=int(float(sb.get("cpus", 0.5)) * 1e9),
        tmpfs={"/tmp": "rw,noexec,nosuid,size=64m", "/workspace/scratch": "rw,noexec,nosuid,size=128m,uid=1000,gid=1000"},
        init=True, labels={LABEL: "1", "harness.agent": agent["name"]},
        ulimits=[docker.types.Ulimit(name="nofile", soft=256, hard=256), docker.types.Ulimit(name="core", soft=0, hard=0)],
        log_config=docker.types.LogConfig(type="json-file", config={"max-size": "256k", "max-file": "1"}),
        privileged=False,
    )
    if runtime:
        kw["runtime"] = runtime
    if command:
        kw["command"] = command
    return kw


class Runner:
    def __init__(self, client: docker.DockerClient | None = None, gateway_container: str = "harness-gateway-1",
                 gateway_url: str = "http://gateway:8080"):
        self.d = client or docker.from_env()
        self.gateway_container = gateway_container
        self.gateway_url = gateway_url
        self.subnets = SubnetPool()
        self.runtime = "runsc" if "runsc" in (self.d.info().get("Runtimes") or {}) else None

    def run(self, agent: dict, image: str, token: str, event_id: str, command: list[str] | None = None) -> RunResult:
        suffix = f"{event_id[-12:]}-{int(time.time() * 1000) % 100000}"
        net_name, c_name = f"sess_{suffix}", f"sbx_{suffix}"
        subnet = self.subnets.take()
        net = container = None
        t0 = time.time()
        try:
            net = self.d.networks.create(
                net_name, driver="bridge", internal=True, labels={LABEL: "1"},
                ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)]),
            )
            net.connect(self.d.containers.get(self.gateway_container), aliases=["gateway"])
            env = {"HARNESS_GATEWAY": self.gateway_url, "HARNESS_TOKEN": token}
            if agent.get("instructions"):
                env["HARNESS_INSTRUCTIONS"] = agent["instructions"]
            if (agent.get("_output") or {}).get("schema"):
                env["HARNESS_OUTPUT_SCHEMA"] = json.dumps(agent["_output"]["schema"])
            container = self.d.containers.run(**hardening(agent, image, env, net_name, c_name, self.runtime, command))
            timeout = int((agent.get("sandbox") or {}).get("timeout_per_event_s", 120))
            timed_out, code = False, None
            try:
                code = container.wait(timeout=timeout)["StatusCode"]
            except Exception:  # requests' ReadTimeout/ConnectionError on wait: treat as timeout and kill
                timed_out = True
                try:
                    container.kill()
                except APIError:
                    pass
            logs = container.logs(tail=60).decode(errors="replace")[-4000:]
            return RunResult(code, timed_out, logs, time.time() - t0)
        finally:
            self._cleanup(container, net)
            self.subnets.give(subnet)

    def _cleanup(self, container, net) -> None:
        if container is not None:
            try:
                container.remove(force=True)
            except (APIError, NotFound):
                pass
        if net is not None:
            try:
                net.disconnect(self.gateway_container, force=True)
            except (APIError, NotFound):
                pass
            try:
                net.remove()
            except (APIError, NotFound):
                pass

    def sweep_orphans(self) -> int:
        """Remove sandboxes and networks left over from a crashed controller."""
        n = 0
        for c in self.d.containers.list(all=True, filters={"label": f"{LABEL}=1"}):
            c.remove(force=True)
            n += 1
        for net in self.d.networks.list(filters={"label": f"{LABEL}=1"}):
            try:
                net.disconnect(self.gateway_container, force=True)
            except (APIError, NotFound):
                pass
            try:
                net.remove()
                n += 1
            except APIError:
                pass
        return n

    # -- images
    def ensure_image(self, agent: dict, root: str = ".") -> str:
        """Return an image id to run. `build` agents are built locally and pinned by id; `image` agents must already be pinned."""
        if agent.get("build"):
            img, _ = self.d.images.build(path=str(Path(root) / agent["build"]), tag=f"harness-agent-{agent['name']}:latest",
                                         rm=True, forcerm=True)
            return img.id
        try:
            return self.d.images.get(agent["image"]).id
        except ImageNotFound:
            return self.d.images.pull(agent["image"]).id
