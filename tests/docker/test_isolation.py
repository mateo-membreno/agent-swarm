"""A hostile agent tries to get out. Every probe must fail; the legitimate paths must work.

Runs the real Runner against real Docker with the real gateway, so these test the actual boundary, not a mock.
"""
from __future__ import annotations

import json
import textwrap

import pytest

pytestmark = pytest.mark.docker

AGENT = {"name": "hostile", "sandbox": {"cpus": 0.5, "memory_mb": 128, "timeout_per_event_s": 60}}

PROBE = textwrap.dedent('''
    import json, os, socket, urllib.request, urllib.error

    out = {}
    def tcp(host, port, t=3):
        try:
            socket.create_connection((host, port), t).close(); return "connected"
        except Exception as e:
            return "blocked:" + type(e).__name__

    out["internet_ip"] = tcp("1.1.1.1", 443)
    out["cloud_metadata"] = tcp("169.254.169.254", 80)
    for name in ("example.com", "redpanda", "host.docker.internal", "harness-redpanda-1"):
        try:
            socket.gethostbyname(name); out["dns_" + name] = "resolved"
        except Exception as e:
            out["dns_" + name] = "blocked:" + type(e).__name__
    out["broker_9092"] = tcp("redpanda", 9092)
    out["docker_sock"] = os.path.exists("/var/run/docker.sock")
    out["uid"] = os.getuid()
    caps = [l for l in open("/proc/self/status") if l.startswith("CapEff")][0].split()[1]
    out["cap_eff"] = caps
    for path in ("/etc/pwned", "/usr/pwned", "/pwned"):
        try:
            open(path, "w").write("x"); out["write_" + path] = "WRITTEN"
        except Exception as e:
            out["write_" + path] = "blocked:" + type(e).__name__
    out["env_keys"] = sorted(k for k in os.environ if any(w in k for w in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")))
    out["env_values"] = [v for k, v in os.environ.items() if k != "HARNESS_TOKEN"]
    # the admin API shares the gateway's interfaces; the sandbox must not be able to use it
    try:
        req = urllib.request.Request("http://gateway:8081/admin/health", headers={"authorization": "Bearer x"})
        r = urllib.request.urlopen(req, timeout=3)
        out["admin_api"] = r.status
    except urllib.error.HTTPError as e:
        out["admin_api"] = e.code
    except Exception as e:
        out["admin_api"] = "blocked:" + type(e).__name__
    print("PROBE=" + json.dumps(out))
''')


def _probe(runner, mint, event_id="ev_iso_1"):
    token = mint(event_id=event_id)
    res = runner.run(AGENT, "harness-base:latest", token, event_id, command=["python", "-c", PROBE])
    assert res.ok, res.logs
    line = next(x for x in res.logs.splitlines() if x.startswith("PROBE="))
    return json.loads(line[len("PROBE="):])


@pytest.fixture(scope="module")
def probe(runner, mint_module):
    return _probe(runner, mint_module)


@pytest.fixture(scope="module")
def mint_module(stack):
    import httpx
    h = {"authorization": f"Bearer {stack['admin_token']}"}
    tokens: list[str] = []

    def _m(agent="hostile", event_id="ev_iso_1", **grant):
        body = {"agent": agent, "event_id": event_id, "event": {"event_id": event_id, "trace_id": "tr_t", "payload": {}}, **grant}
        r = httpx.post(f"{stack['admin_url']}/admin/sessions", json=body, headers=h)
        r.raise_for_status()
        tokens.append(r.json()["token"])
        return tokens[-1]

    yield _m
    for t in tokens:
        httpx.post(f"{stack['admin_url']}/admin/sessions/revoke", json={"token": t}, headers=h)


def test_no_internet(probe):
    assert probe["internet_ip"].startswith("blocked"), probe
    assert probe["cloud_metadata"].startswith("blocked"), probe
    assert probe["dns_example.com"].startswith("blocked"), probe


def test_cannot_see_the_broker(probe):
    assert probe["dns_redpanda"].startswith("blocked") and probe["dns_harness-redpanda-1"].startswith("blocked"), probe
    assert probe["broker_9092"].startswith("blocked"), probe


def test_no_host_reachability(probe):
    assert probe["dns_host.docker.internal"].startswith("blocked"), probe


def test_no_docker_socket(probe):
    assert probe["docker_sock"] is False


def test_unprivileged(probe):
    assert probe["uid"] == 1000
    assert int(probe["cap_eff"], 16) == 0, "sandbox must hold no Linux capabilities"


def test_root_filesystem_is_read_only(probe):
    assert all(v.startswith("blocked") for k, v in probe.items() if k.startswith("write_")), probe


def test_only_the_session_token_is_in_the_environment(probe, stack):
    assert probe["env_keys"] == ["HARNESS_TOKEN"], probe
    assert not any(stack["admin_token"] in v for v in probe["env_values"]), "admin token leaked into the sandbox"


def test_admin_api_is_unreachable_from_a_sandbox(probe):
    assert probe["admin_api"] == 403 or str(probe["admin_api"]).startswith("blocked"), probe


def test_no_state_survives_between_events(runner, mint_module):
    write = ["python", "-c", "open('/tmp/leak','w').write('secret'); open('/workspace/scratch/leak','w').write('secret'); print('WROTE')"]
    read = ["python", "-c",
            "import os; print('LEAK' if os.path.exists('/tmp/leak') or os.path.exists('/workspace/scratch/leak') else 'CLEAN')"]
    a = runner.run(AGENT, "harness-base:latest", mint_module(event_id="ev_iso_a"), "ev_iso_a", command=write)
    b = runner.run(AGENT, "harness-base:latest", mint_module(event_id="ev_iso_b"), "ev_iso_b", command=read)
    assert "WROTE" in a.logs and a.ok, a.logs
    assert "CLEAN" in b.logs and "LEAK" not in b.logs, b.logs


def test_infinite_loop_is_killed_at_the_timeout(runner, mint_module):
    agent = {**AGENT, "sandbox": {**AGENT["sandbox"], "timeout_per_event_s": 3}}
    res = runner.run(agent, "harness-base:latest", mint_module(event_id="ev_iso_loop"), "ev_iso_loop",
                     command=["python", "-c", "while True: pass"])
    assert res.timed_out and not res.ok and res.seconds < 30


def test_memory_hog_is_killed(runner, mint_module):
    res = runner.run(AGENT, "harness-base:latest", mint_module(event_id="ev_iso_mem"), "ev_iso_mem",
                     command=["python", "-c", "x = bytearray(900 * 1024 * 1024); print('ALLOCATED')"])
    assert not res.ok and "ALLOCATED" not in res.logs


FORK = textwrap.dedent("""
    import os, time
    n = 0
    for _ in range(2000):
        try:
            pid = os.fork()
        except OSError:
            break
        if pid == 0:
            time.sleep(4)
            os._exit(0)
        n += 1
    print("FORKED", n)
""")


def test_fork_bomb_is_contained(runner, mint_module):
    res = runner.run(AGENT, "harness-base:latest", mint_module(event_id="ev_iso_fork"), "ev_iso_fork", command=["python", "-c", FORK])
    forked = [int(x.split()[1]) for x in res.logs.splitlines() if x.startswith("FORKED ")]
    assert forked and forked[0] < 128, f"pids_limit did not stop the fork bomb: {res.logs}"


def test_cleanup_leaves_nothing_behind(runner):
    import docker
    d = docker.from_env()
    assert d.containers.list(all=True, filters={"label": "harness.sandbox=1"}) == []
    assert d.networks.list(filters={"label": "harness.sandbox=1"}) == []


def test_legitimate_gateway_path_works_from_inside(runner, mint_module):
    token = mint_module(event_id="ev_iso_ok", agent="hostile")
    code = "import harness; print('CAPS', harness.capabilities()['agent']); print('EVENT', harness.event()['event_id'])"
    res = runner.run(AGENT, "harness-base:latest", token, "ev_iso_ok", command=["python", "-c", code])
    assert res.ok, res.logs
    assert "CAPS hostile" in res.logs and "EVENT ev_iso_ok" in res.logs, res.logs
