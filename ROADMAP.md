# LLM-Portal Roadmap

This roadmap distills the project's historical planning into a public, outcome-oriented view. It is
not a release commitment: implementation scope and scheduling are decided in GitHub issues and pull
requests.

## Current foundation

- OpenAI Chat Completions and Anthropic Messages gateways, including streaming and tool calls
- Multi-site WireGuard enrollment and model routing with virtual-key group isolation
- Web administration for sites, groups, models, keys, usage, and external MCP servers
- Group-aware MCP tool discovery and invocation
- Three edge modes: standalone TLS, integration with an existing nginx, and trusted-LAN TLS offload
- Commit-bound release packages, checksums, deployment smoke tests, and rollback tooling

## Now: stabilize the Alpha release

- Keep installation, upgrade, and rollback workflows reproducible across all edge modes
- Expand browser and deployment regression coverage around administrator and site-management flows
- Improve diagnostics for routing failures, tunnel health, and external MCP availability
- Turn accepted roadmap work into narrowly scoped public issues before implementation

## Next: routing and policy controls

- Explicit active/standby deployment order with observable failover events
- Per-key and per-model system-prompt injection or replacement policies
- Managed passthrough for OpenAI Responses-compatible upstreams
- Cache governance: policy visibility, invalidation controls, and clearer hit-rate diagnostics
- Broader validation of upstream providers beyond private OpenAI-compatible model servers

## Later: data and availability

- Optional, versioned content-optimization pipelines with auditable transformations
- Opt-in conversation retention and a paginated export/query API
- High-availability management services and alternatives to single-host SQLite state
- Stronger isolation for deployments that require multiple administrative trust domains

## Current limitations

- Routing uses least-busy selection and failure cooldown, without an explicit active/standby sequence.
- The gateway does not currently apply configurable system-prompt policies.
- Conversation bodies are not retained; usage logs contain request metadata only.
- The management plane is designed for a single gateway host and is not highly available.
- Private model servers normally rely on the WireGuard and site-LAN trust boundary rather than their
  own application authentication.

## Non-goals for the current release line

- A hosted multi-tenant SaaS control plane
- Physical tenant isolation on a shared gateway host
- Cross-protocol conversion for OpenAI Responses server-side conversation semantics
- Support for undocumented or arbitrary invalid message roles
