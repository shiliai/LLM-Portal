# LLM-portal

**English** | [Chinese](README.md)

A self-hosted unified AI gateway for small and medium-sized organizations. It aggregates private
inference models spread across multiple internal sites behind one public gateway, exposes
OpenAI- and Anthropic-compatible APIs, and provides virtual keys, group-based routing, usage
metering, and a web administration console.

> **Status: Alpha.** The current version is used in real deployments, but APIs, configuration, and
> upgrade procedures may still change before the first stable release. See [`ROADMAP.md`](ROADMAP.md)
> for known limitations and planned work.

## Implemented MVP features

- **Dual-protocol API:** OpenAI `/v1/chat/completions` and Anthropic `/v1/messages`, including
  `count_tokens`, SSE streaming, and tool calls. The `compat` proxy handles known Anthropic tool-chain
  compatibility cases such as inline system-message normalization, forced tool-choice rewriting,
  and OpenAI streaming `finish_reason` correction.
- **Multi-site connectivity:** private-site machines establish outbound WireGuard tunnels. No model
  port needs to be exposed publicly. `site-add` issues a one-line enrollment command.
- **Model routing:** LiteLLM maps public model names to deployments, performs least-busy routing and
  failure cooldown, and supports aliases. Site tags and key groups restrict keys to selected sites.
- **Virtual keys:** create, block, delete, restrict by model, and bind to a group. Plaintext is shown
  when issued and can later be recovered from the Fernet-encrypted key vault by an administrator.
- **Usage metering:** per-key and per-model token usage, cache hits, time to first token, reasoning
  effort, and request-level details.
- **Web console:** administrator login with email, password, and optional TOTP; graphical management
  for sites, groups, models, keys, usage, and external MCP servers; user self-service usage view.
- **MCP gateway:** built-in image analysis plus registered external MCP servers. Upstream MCP
  credentials remain on the gateway. External tools can be assigned to groups, and a virtual key's
  `metadata.group` restricts both `tools/list` and `tools/call`.
- **Public-surface allowlist:** nginx exposes only the required application routes. LiteLLM's UI and
  management APIs return 404 through the public gateway.

## Architecture

```text
Clients (OpenAI SDK / Claude Code / pi / MCP)
                         |
                         v
+------------------------------- gateway host -------------------------------+
| edge nginx allowlist                                                      |
|   /v1/messages, /v1/chat -> compat:8400 -> litellm:4000 -> postgres       |
|   /console                -> console:8300                                  |
|   /mcp                    -> mcp-hub:8200                                  |
|   /onboard/*              -> onboardd:8100                                 |
|                                      |                                     |
|                               WireGuard wg0                                |
+--------------------------------------|--------------------------------------+
                                       |
                    outbound public-key tunnels
                                       |
                    private sites running vLLM, llama.cpp, etc.
```

The core Compose stack has seven services: `litellm`, `compat`, `postgres`, `mcp-hub`, `onboardd`,
`console`, and the `wireguard` sidecar. Standalone mode adds `edge-nginx` and `edge-certbot` profiles.
External mode reuses an existing nginx deployment, while offload mode starts only the HTTP edge and
expects a LAN upstream device to terminate TLS.

## Quick start

Prerequisites: a Linux gateway host with Docker Compose, a user allowed to access Docker, and the
required firewall rules for the selected edge mode. Standalone public deployment normally needs DNS,
80/443 TCP, and 51820 UDP.

```bash
git clone https://github.com/shiliai/LLM-Portal.git
cd LLM-Portal/vps
cp .env.example .env
vi .env
./deploy.sh
```

Replace every `REPLACE_ME` value and configure at least `DOMAIN`, `ADMIN_EMAIL`, and the generated
credentials. `deploy.sh` is idempotent and runs the Compose deployment, edge setup, smoke tests, and
public-surface checks. Daily runs do not require root; firewall, BBR, and migration from an old systemd
deployment may require one-time `sudo` commands.

Issue a site enrollment command on the gateway host, then run its output on the private-site machine:

```bash
site-add my-site --model my-model:8000 --group default
# curl -fsSL "https://<domain>/onboard/install?token=..." | sudo bash
```

Call the OpenAI-compatible endpoint with a virtual key:

```bash
curl https://llm-portal.example.com/v1/chat/completions \
  -H "Authorization: Bearer $LLM_PORTAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hi"}]}'
```

See the [operations runbook](docs/runbook.md) for detailed deployment,
site enrollment, acceptance records, maintenance, and recovery procedures.

## Edge modes and configuration

All supported variables and generation instructions are in
[`vps/.env.example`](vps/.env.example).

- `EDGE_MODE=standalone` (default): the stack publishes ports 80/443 with `edge-nginx` and
  `edge-certbot`; no existing reverse proxy is required.
- `EDGE_MODE=external`: inject the gateway server block into an existing nginx deployment. Configure
  `EDGE_NGINX_CONTAINER`, `NGINX_CONF_DIR`, and `CERTBOT_DIR`.
- `EDGE_MODE=offload`: a trusted LAN device terminates TLS and proxies to the HTTP-only edge on port
  80. Set `PUBLIC_BASE` to the external HTTPS URL and `WG_ENDPOINT_HOST` to the WireGuard-reachable
  gateway address. This mode is intended for a trusted LAN and does not add authentication between
  the upstream TLS terminator and the edge.

WireGuard settings include `WG_PORT`, `WG_SUBNET`, and `WG_VPS_IP`. `WG_SUBNET` must be an IPv4 `/24`.

## Testing and development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test
make lint
make compose-validate
```

Browser E2E tests use Playwright and a mock LiteLLM server; see
[`console/e2e/README.md`](console/e2e/README.md). CI also runs a full-history
Gitleaks scan and dependency audits in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Repository layout

| Path | Purpose |
|------|---------|
| `vps/` | Compose, deployment/release/rollback scripts, nginx, LiteLLM, and WireGuard configuration |
| `console/`, `onboardd/`, `mcp-hub/`, `compat/` | Runtime services, dependencies, and tests |
| `site-tools/` | Site enrollment, inspection, and revocation commands |
| `docs/` | Operator-facing documentation |
| `tools/` | Compatibility experiments and diagnostics, excluded from runtime containers |

## Security boundary

- `console` and `onboardd` mount Docker's socket and therefore have host-equivalent administrative
  power. This is an explicit trade-off; see [`SECURITY.md`](SECURITY.md).
- Configure `ADMIN_EMAIL` and `ADMIN_PASSWORD` in production. If `ADMIN_EMAIL` is empty, the console
  retains a legacy compatibility path that accepts the LiteLLM master key for web login. Once the
  administrator account is configured, the master key is used only by server-side and loopback flows.
- User virtual-key plaintext is encrypted at rest in a separate key vault; session storage keeps only
  hashes and last-four metadata. Compromise of the whole gateway host still compromises the vault.
- Site model servers are normally unauthenticated behind WireGuard. The tunnel and site LAN are the
  trust boundary; do not expose model ports outside that boundary.
- Report vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md), not in public issues.

## Roadmap

Known limitations, near-term priorities, and longer-term directions are maintained in
[`ROADMAP.md`](ROADMAP.md). Roadmap items describe intent; scope and scheduling are tracked in linked
issues.

## Contributing and license

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a change. Never submit real credentials,
internal topology, or identifying infrastructure data. The project is licensed under the
[Apache License 2.0](LICENSE).
