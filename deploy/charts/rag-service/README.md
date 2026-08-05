# RAG Service Chart

The RAG service chart deploys a private retrieval API with local lexical retrieval and optional Qdrant-backed vector retrieval.

## Important Values

| Value | Purpose |
| --- | --- |
| `retrieval.backend` | Selects `lexical` or `qdrant`. |
| `retrieval.vectorStore.url` | Qdrant base URL when vector retrieval is enabled. |
| `retrieval.vectorStore.dimensions` | Vector dimension count expected by the collection. |
| `knowledge.documents` | Local knowledge documents mounted into the service for labs. |
| `auth.enabled` | Requires API-key or bearer-token authentication for business endpoints. |
| `networkPolicy.vectorStoreEgress` | Allows controlled egress to the vector-store namespace. |

## Profiles

- Minimal: `deploy/charts/rag-service/values.yaml`
- Local: `deploy/clusters/local/values/rag-service.yaml`
- Customer: `deploy/clusters/customer/values/rag-service.yaml`

Run `make config-contract` after changing settings, env vars, Helm values, or chart defaults.

<!-- chart-docs:start -->
## Values

| Value | Default |
| --- | --- |
| `auth.apiKeyHashes` | `[]` |
| `auth.apiKeyHeader` | `X-API-Key` |
| `auth.enabled` | `false` |
| `auth.existingSecret.key` | `api-key-sha256s` |
| `auth.existingSecret.name` | `""` |
| `auth.jwt.audience` | `""` |
| `auth.jwt.cacheSeconds` | `300` |
| `auth.jwt.enabled` | `false` |
| `auth.jwt.issuer` | `""` |
| `auth.jwt.jwksUrl` | `""` |
| `auth.jwt.requestTimeoutSeconds` | `5.0` |
| `auth.jwt.required` | `false` |
| `auth.jwt.tenantClaim` | `sandbox_id` |
| `autoscaling.enabled` | `false` |
| `autoscaling.maxReplicas` | `5` |
| `autoscaling.minReplicas` | `1` |
| `autoscaling.targetCPUUtilizationPercentage` | `70` |
| `image.pullPolicy` | `IfNotPresent` |
| `image.repository` | `ghcr.io/ramazankara/private-ai-platform-kit/rag-service` |
| `image.tag` | `v0.28.1` |
| `ingestion.annotations` | `{}` |
| `ingestion.backoffLimit` | `1` |
| `ingestion.chunkChars` | `1200` |
| `ingestion.enabled` | `false` |
| `ingestion.overlapChars` | `120` |
| `ingestion.restartPolicy` | `Never` |
| `knowledge.documents.accelerators.md` | `# Accelerator Support<br><br>Ollama is the default local runtime. vLLM is the production-style runtime<br>for customer cluster...` |
| `knowledge.documents.coding-agents.md` | `# Coding Agents<br><br>Coding agents should call the inference gateway with X-Request-ID,<br>X-Sandbox-ID, and traceparent whe...` |
| `knowledge.documents.controls.md` | `# Platform Controls<br><br>The platform includes model catalog governance, gateway admission controls,<br>Redis-backed sandbox...` |
| `knowledge.documents.platform-overview.md` | `# Private AI Platform Kit<br><br>Private AI Platform Kit is a local-first, provider-neutral Kubernetes platform<br>for private...` |
| `knowledge.mountPath` | `/knowledge` |
| `namespace.create` | `true` |
| `namespace.name` | `""` |
| `networkPolicy.allowedIngressNamespaceLabels` | `[{"platform.ai/traceable-sandbox": "true"}]` |
| `networkPolicy.allowedIngressNamespaces` | `["ai-agents", "ai-sandbox", "monitoring"]` |
| `networkPolicy.dnsEgress.namespace` | `kube-system` |
| `networkPolicy.dnsEgress.port` | `53` |
| `networkPolicy.embeddingEgress.enabled` | `false` |
| `networkPolicy.embeddingEgress.namespace` | `""` |
| `networkPolicy.embeddingEgress.port` | `8080` |
| `networkPolicy.enabled` | `true` |
| `networkPolicy.vectorStoreEgress.enabled` | `true` |
| `networkPolicy.vectorStoreEgress.namespace` | `vector` |
| `networkPolicy.vectorStoreEgress.port` | `6333` |
| `observability.tracing.enabled` | `false` |
| `observability.tracing.otlpEndpoint` | `""` |
| `observability.tracing.serviceName` | `rag-service` |
| `podDisruptionBudget.enabled` | `true` |
| `podDisruptionBudget.minAvailable` | `1` |
| `podLabels.platform.ai/cost-center` | `platform` |
| `podLabels.platform.ai/environment` | `local` |
| `podLabels.platform.ai/owner` | `platform-team` |
| `podLabels.platform.ai/sandbox-id` | `local-lab` |
| `replicaCount` | `1` |
| `resources.limits.cpu` | `300m` |
| `resources.limits.memory` | `256Mi` |
| `resources.requests.cpu` | `50m` |
| `resources.requests.memory` | `96Mi` |
| `retrieval.allowedClassifications` | `[]` |
| `retrieval.backend` | `lexical` |
| `retrieval.candidateMultiplier` | `4` |
| `retrieval.defaultTopK` | `3` |
| `retrieval.embedding.baseUrl` | `""` |
| `retrieval.embedding.model` | `hash-text-v1` |
| `retrieval.embedding.provider` | `hash` |
| `retrieval.lexicalWeight` | `0.5` |
| `retrieval.maxContextChars` | `6000` |
| `retrieval.maxQueryChars` | `2048` |
| `retrieval.maxRequestBodyBytes` | `1048576` |
| `retrieval.maxTopK` | `8` |
| `retrieval.reranker.baseUrl` | `""` |
| `retrieval.reranker.model` | `""` |
| `retrieval.reranker.provider` | `none` |
| `retrieval.reranker.timeoutSeconds` | `2.0` |
| `retrieval.tenantIsolation.enabled` | `false` |
| `retrieval.tenantIsolation.field` | `owner` |
| `retrieval.vectorStore.bootstrapFromKnowledge` | `true` |
| `retrieval.vectorStore.collection` | `private-ai-platform-kit` |
| `retrieval.vectorStore.collectionVersion` | `v1` |
| `retrieval.vectorStore.dimensions` | `384` |
| `retrieval.vectorStore.timeoutSeconds` | `1.0` |
| `retrieval.vectorStore.url` | `""` |
| `service.port` | `8080` |
| `serviceAccount.automountServiceAccountToken` | `false` |
| `serviceAccount.create` | `true` |
| `serviceAccount.name` | `""` |
| `serviceMonitor.enabled` | `true` |
| `serviceMonitor.interval` | `30s` |
| `sourceManifest.enabled` | `false` |
| `sourceManifest.manifest.apiVersion` | `platform.ai/v1alpha1` |
| `sourceManifest.manifest.kind` | `RagSourceManifest` |
| `sourceManifest.manifest.metadata.name` | `platform-knowledge` |
| `sourceManifest.manifest.spec.sources` | `[]` |
| `sourceManifest.mountPath` | `/rag-sources/source-manifest.yaml` |
| `sourceManifest.path` | `""` |
| `tests.enabled` | `true` |
| `tests.image.digest` | `sha256:9532d8c39891ca2ecde4d30d7710e01fb739c87a8b9299685c63704296b16028` |
| `tests.image.repository` | `busybox` |
| `tests.image.tag` | `1.37.0` |
| `topologySpread.enabled` | `true` |
| `topologySpread.maxSkew` | `1` |
| `topologySpread.topologyKey` | `kubernetes.io/hostname` |
| `topologySpread.whenUnsatisfiable` | `ScheduleAnyway` |
| `traceability.auditChainStore.backend` | `memory` |
| `traceability.auditChainStore.key` | `rag-service:audit-chain-head` |
| `traceability.auditChainStore.path` | `/var/lib/rag-service/audit-chain-head.json` |
| `traceability.auditChainStore.persistIntervalSeconds` | `5` |
| `traceability.auditChainStore.redisUrl` | `redis://budget-redis.budget.svc.cluster.local:6379/0` |
| `traceability.auditChainStore.timeoutSeconds` | `0.5` |
| `traceability.auditLogEnabled` | `true` |
| `traceability.defaultSandboxId` | `local-lab` |
<!-- chart-docs:end -->
## Install profiles

| Profile | Command |
| --- | --- |
| Minimal (chart defaults) | `helm install rag-service deploy/charts/rag-service` |
| Local kind lab | `helm install rag-service deploy/charts/rag-service -f deploy/clusters/local/values/rag-service.yaml` |
| Customer cluster | `helm install rag-service deploy/charts/rag-service -f deploy/clusters/customer/values/rag-service.yaml` |

In GitOps installs these value files are applied by the matching Argo CD Application in `deploy/clusters/<env>/apps.yaml`; the commands above are for direct `helm` workstation checks.
