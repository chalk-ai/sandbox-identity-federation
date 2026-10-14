"""Sandbox identity forwarding demo: run a fixed scenario as one agent and stream every hop.

Each agent has its own Chalk service token. The server keeps one warm sandbox per agent,
launched with that agent's token and ``chalk_identity=True``, so the sandbox's identity is
the agent's. A run execs agent_chain.py in the sandbox, which forwards that identity to
Google Cloud (STS, service-account impersonation, BigQuery) and prints one JSON line per hop.

Visitors can only pick a scenario by id; plans, credentials and targets stay server-side.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import threading
import time
import types
from pathlib import Path
from typing import Any, AsyncIterator

import chalkcompute
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

ENV_ID = os.environ["DEMO_CHALK_ENVIRONMENT_ID"]
API_SERVER = os.environ["DEMO_CHALK_API_SERVER"]
SANDBOX_API_HOST = os.environ["DEMO_SANDBOX_API_HOST"]
PROVIDER = os.environ["DEMO_WIF_PROVIDER"]
PROJECT = os.environ["DEMO_GCP_PROJECT"]
PROJECT_NUMBER = os.environ["DEMO_GCP_PROJECT_NUMBER"]
DATASET = os.environ["DEMO_BQ_DATASET"]
INDEX_HTML = Path(__file__).with_name("index.html")
CHAIN = Path(__file__).with_name("agent_chain.py")

# Runs per visitor IP per minute, and concurrent runs across all visitors.
RATE_PER_MINUTE = 8
MAX_CONCURRENT_RUNS = 3
RUN_TIMEOUT_S = 90

log = logging.getLogger("sandbox-identity")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

AGENTS: dict[str, dict[str, str]] = {
    "release-agent": {"label": "Release agent", "env": "RELEASE"},
    "support-agent": {"label": "Support agent", "env": "SUPPORT"},
    "scratch-agent": {"label": "Unvetted agent", "env": "SCRATCH"},
}

RELEASE_SA = f"sbx-demo-release@{PROJECT}.iam.gserviceaccount.com"
SUPPORT_SA = f"sbx-demo-support@{PROJECT}.iam.gserviceaccount.com"
SCENARIOS: dict[str, dict[str, Any]] = {
    "release": {
        "agent": "release-agent",
        "title": "Release agent checks last night's deploys",
        "plan": {
            "direct_tables": ["service_status"],
            "service_account": RELEASE_SA,
            "sa_tables": ["release_metrics", "customer_payments"],
        },
    },
    "support": {
        "agent": "support-agent",
        "title": "Support agent triages new tickets",
        "plan": {
            "direct_tables": ["service_status"],
            "service_account": SUPPORT_SA,
            "sa_tables": ["support_tickets", "release_metrics"],
        },
    },
    "unvetted": {
        "agent": "scratch-agent",
        "title": "Unvetted agent tries to borrow the release agent's access",
        "plan": {"direct_tables": ["service_status"], "service_account": RELEASE_SA, "sa_tables": ["release_metrics"]},
    },
    "replay": {
        "agent": "support-agent",
        "title": "A token minted for another audience is replayed",
        "plan": {"audience": "https://tools.example.invalid/mcp", "direct_tables": ["service_status"]},
    },
}


def agent_client(agent: str) -> Any:
    """Credentials object for ConnectClient, so each sandbox is launched as its own agent."""
    prefix = f"DEMO_AGENT_{AGENTS[agent]['env']}"
    return types.SimpleNamespace(
        _client_id=os.environ[f"{prefix}_CLIENT_ID"],
        _client_secret=os.environ[f"{prefix}_CLIENT_SECRET"],
        _api_server=API_SERVER,
        _primary_environment=ENV_ID,
    )


IMAGE = (
    chalkcompute.Image.debian_slim("3.12")
    .pip_install(["chalkcompute==2.13.5"])
    .add_local_file(str(CHAIN), "/opt/demo/agent_chain.py", strategy="copy")
)
NETWORK = chalkcompute.NetworkPolicy(
    allowed_hosts=[SANDBOX_API_HOST, "sts.googleapis.com", "iamcredentials.googleapis.com", "bigquery.googleapis.com"]
)


class WarmSandboxes:
    """One long-lived sandbox per agent, recreated when it has expired or stops answering."""

    def __init__(self) -> None:
        super().__init__()
        self._sandboxes: dict[str, Any] = {}
        self._locks = {a: threading.Lock() for a in AGENTS}

    def _launch(self, agent: str) -> Any:
        t0 = time.monotonic()
        sb = chalkcompute.Sandbox(
            image=IMAGE,
            chalk_identity=True,
            network_policy=NETWORK,
            lifetime="21600s",
            cpu="250m",
            memory="512Mi",
            tags={"demo": "sandbox-identity", "agent": agent},
            chalk_client=agent_client(agent),
        ).run()
        log.info("launched sandbox for %s in %.1fs", agent, time.monotonic() - t0)
        return sb

    def run(self, agent: str, plan: dict[str, Any]) -> tuple[list[str], float, bool]:
        """Returns (hop lines, sandbox launch seconds, whether the sandbox was reused)."""
        with self._locks[agent]:
            launch_s, reused = 0.0, True
            for attempt in range(2):
                sb = self._sandboxes.get(agent)
                if sb is None:
                    t0 = time.monotonic()
                    sb = self._launch(agent)
                    launch_s, reused = time.monotonic() - t0, False
                    self._sandboxes[agent] = sb
                try:
                    r = sb.exec("python", "/opt/demo/agent_chain.py", json.dumps(plan))
                except Exception:
                    log.exception("exec failed for %s (attempt %d); relaunching", agent, attempt)
                    self._sandboxes.pop(agent, None)
                    continue
                if r.stderr_text.strip():
                    log.warning("agent_chain stderr for %s: %s", agent, r.stderr_text[-2000:])
                return [ln for ln in r.stdout_text.splitlines() if ln.strip()], launch_s, reused
            raise RuntimeError("sandbox unavailable")

    def prewarm(self) -> None:
        for agent in AGENTS:
            try:
                with self._locks[agent]:
                    if agent not in self._sandboxes:
                        self._sandboxes[agent] = self._launch(agent)
            except Exception:
                log.exception("prewarm failed for %s", agent)


warm = WarmSandboxes()
run_slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
recent: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)

app = FastAPI()


@app.on_event("startup")
async def startup() -> None:
    threading.Thread(target=warm.prewarm, daemon=True).start()


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/scenarios")
async def scenarios() -> JSONResponse:
    return JSONResponse(
        [
            {"id": k, "title": v["title"], "agent": v["agent"], "agent_label": AGENTS[v["agent"]]["label"]}
            for k, v in SCENARIOS.items()
        ]
    )


def redact(line: str) -> str:
    """Keep the demo's cloud project out of the public page; the hops are otherwise verbatim."""
    return line.replace(PROJECT_NUMBER, "PROJECT_NUMBER").replace(PROJECT, "your-project")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@app.post("/api/run/{scenario_id}")
async def run(scenario_id: str, request: Request) -> StreamingResponse:
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise HTTPException(404, "unknown scenario")
    ip = client_ip(request)
    now = time.monotonic()
    window = recent[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_PER_MINUTE:
        raise HTTPException(429, "Too many runs from your address; try again in a minute.")
    window.append(now)

    agent = scenario["agent"]
    plan = {"project": PROJECT, "dataset": DATASET, "audience": PROVIDER, "sts_audience": PROVIDER, **scenario["plan"]}

    async def stream() -> AsyncIterator[bytes]:
        if run_slots.locked():
            yield (json.dumps({"hop": "queued"}) + "\n").encode()
        async with run_slots:
            yield (
                json.dumps(
                    {
                        "hop": "launch",
                        "agent": agent,
                        "agent_label": AGENTS[agent]["label"],
                        "scenario": scenario_id,
                        "title": scenario["title"],
                    }
                )
                + "\n"
            ).encode()
            t0 = time.monotonic()
            try:
                lines, launch_s, reused = await asyncio.wait_for(
                    asyncio.to_thread(warm.run, agent, plan), timeout=RUN_TIMEOUT_S
                )
            except Exception as exc:
                log.exception("run %s failed", scenario_id)
                yield (json.dumps({"hop": "error", "detail": type(exc).__name__}) + "\n").encode()
                return
            for i, line in enumerate(lines):
                hop = json.loads(redact(line))
                if i == 0:
                    hop["sandbox_reused"] = reused
                    hop["launch_s"] = round(launch_s, 1)
                yield (json.dumps(hop) + "\n").encode()
            yield (json.dumps({"hop": "done", "total_ms": round((time.monotonic() - t0) * 1000)}) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
