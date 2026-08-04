from __future__ import annotations

from typing import Any

from .common import container, env_names, find_kind, nested, require


def check_gateway_render(name: str, docs: list[dict[str, Any]], errors: list[str]) -> None:
    deployments = find_kind(docs, "Deployment")
    require(errors, len(deployments) == 1, f"{name}: expected one gateway Deployment")
    if not deployments:
        return
    deployment = deployments[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    gateway = container(deployment)
    gateway_security = gateway.get("securityContext", {})
    required_env = {
        "RUNTIME_BACKEND",
        "MODEL_ID",
        "OLLAMA_BASE_URL",
        "VLLM_BASE_URL",
        "REQUEST_TIMEOUT_SECONDS",
        "ALLOWED_MODELS",
        "MAX_MESSAGES",
        "MAX_PROMPT_CHARS",
        "MAX_REQUEST_BODY_BYTES",
        "MAX_COMPLETION_TOKENS",
        "ALLOW_STREAMING",
        "PROMPT_SECRET_DETECTION_ENABLED",
        "PROMPT_SECRET_PATTERNS",
        "SANDBOX_BUDGET_ENABLED",
        "SANDBOX_BUDGET_BACKEND",
        "SANDBOX_REQUEST_BUDGET",
        "SANDBOX_PROMPT_CHAR_BUDGET",
        "SANDBOX_ESTIMATED_TOKEN_BUDGET",
        "BUDGET_ESTIMATED_CHARS_PER_TOKEN",
        "SANDBOX_BUDGET_WINDOW_SECONDS",
        "SANDBOX_BUDGET_REDIS_URL",
        "SANDBOX_BUDGET_REDIS_TIMEOUT_SECONDS",
        "SANDBOX_BUDGET_KEY_PREFIX",
        "AUDIT_LOG_ENABLED",
        "DEFAULT_SANDBOX_ID",
        "API_KEY_AUTH_ENABLED",
        "API_KEY_HEADER",
        "API_KEY_SHA256S",
        "OTEL_TRACING_ENABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
    }
    missing_env = required_env - env_names(deployment)
    env = {item.get("name"): item.get("value") for item in gateway.get("env", [])}

    require(
        errors,
        pod_spec.get("automountServiceAccountToken") is False,
        f"{name}: gateway pod must disable service account token automount",
    )
    require(
        errors, nested(pod_spec, "securityContext", "runAsNonRoot") is True, f"{name}: gateway pod must run as non-root"
    )
    require(
        errors,
        "topologySpreadConstraints" in pod_spec,
        f"{name}: gateway should render topology spread constraints for multi-replica placement",
    )
    require(errors, not missing_env, f"{name}: gateway Deployment is missing env vars: {sorted(missing_env)}")
    require(
        errors,
        env.get("MAX_REQUEST_BODY_BYTES") == "1048576",
        f"{name}: MAX_REQUEST_BODY_BYTES must render as a base-10 integer",
    )
    require(
        errors,
        env.get("BATCH_MAX_FILE_BYTES") == "104857600",
        f"{name}: BATCH_MAX_FILE_BYTES must render as a base-10 integer",
    )
    require(
        errors,
        gateway_security.get("allowPrivilegeEscalation") is False,
        f"{name}: gateway must block privilege escalation",
    )
    require(
        errors,
        gateway_security.get("readOnlyRootFilesystem") is True,
        f"{name}: gateway must use a read-only root filesystem",
    )
    require(
        errors,
        "ALL" in nested(gateway_security, "capabilities", "drop", default=[]),
        f"{name}: gateway must drop all Linux capabilities",
    )
    require(errors, bool(nested(gateway, "resources", "requests")), f"{name}: gateway must set resource requests")
    require(errors, bool(nested(gateway, "resources", "limits")), f"{name}: gateway must set resource limits")
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: gateway chart must render a ServiceAccount")
    service_accounts = find_kind(docs, "ServiceAccount")
    if service_accounts:
        require(
            errors,
            service_accounts[0].get("automountServiceAccountToken") is False,
            f"{name}: ServiceAccount must disable token automount",
        )
    require(errors, bool(find_kind(docs, "NetworkPolicy")), f"{name}: gateway chart must render a NetworkPolicy")
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), f"{name}: gateway chart must render a PodDisruptionBudget"
    )
    scaled_objects = find_kind(docs, "ScaledObject")
    if scaled_objects:
        queries = "\n".join(
            nested(trigger, "metadata", "query", default="")
            for trigger in nested(scaled_objects[0], "spec", "triggers", default=[])
        )
        for signal in (
            'route=~"/v1/.*"',
            "inference_gateway_inflight_requests",
            "inference_gateway_load_shed_total",
            "inference_gateway_request_duration_seconds_bucket",
        ):
            require(errors, signal in queries, f"{name}: KEDA scaling must include signal {signal}")


def check_budget_redis_render(docs: list[dict[str, Any]], errors: list[str]) -> None:
    deployments = find_kind(docs, "Deployment")
    require(errors, len(deployments) == 1, "budget-redis: expected one Deployment")
    if deployments:
        deployment = deployments[0]
        pod_spec = deployment["spec"]["template"]["spec"]
        redis = container(deployment)
        security = redis.get("securityContext", {})
        image = redis.get("image", "")
        require(errors, ":latest" not in image, "budget-redis: image tag must be pinned")
        require(
            errors,
            pod_spec.get("automountServiceAccountToken") is False,
            "budget-redis: pod must disable service account token automount",
        )
        require(
            errors,
            nested(pod_spec, "securityContext", "runAsNonRoot") is True,
            "budget-redis: pod must run as non-root",
        )
        require(
            errors, security.get("allowPrivilegeEscalation") is False, "budget-redis: must block privilege escalation"
        )
        require(
            errors, security.get("readOnlyRootFilesystem") is True, "budget-redis: must use a read-only root filesystem"
        )
        require(
            errors,
            "ALL" in nested(security, "capabilities", "drop", default=[]),
            "budget-redis: must drop all Linux capabilities",
        )
        require(errors, bool(nested(redis, "resources", "requests")), "budget-redis: must set resource requests")
        require(errors, bool(nested(redis, "resources", "limits")), "budget-redis: must set resource limits")
    require(errors, bool(find_kind(docs, "Service")), "budget-redis: chart must render a Service")
    require(errors, bool(find_kind(docs, "ServiceAccount")), "budget-redis: chart must render a ServiceAccount")
    require(
        errors,
        len(find_kind(docs, "NetworkPolicy")) >= 2,
        "budget-redis: chart must render default-deny and gateway allow NetworkPolicies",
    )
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), "budget-redis: chart must render a PodDisruptionBudget"
    )


def check_runtime_egress(
    name: str,
    docs: list[dict[str, Any]],
    errors: list[str],
    *,
    allow_local_exception: bool = False,
) -> None:
    """Require every runtime egress rule to name a destination.

    The only broad CIDR is the labeled local Ollama bootstrap exception; customer
    and default renders stay closed or use explicit non-broad CIDRs.
    """
    broad_cidrs = {"0.0.0.0/0", "::/0"}
    for policy in find_kind(docs, "NetworkPolicy"):
        metadata = policy.get("metadata", {})
        annotations = metadata.get("annotations", {})
        labels = metadata.get("labels", {})
        for rule in policy.get("spec", {}).get("egress") or []:
            destinations = rule.get("to")
            require(errors, bool(destinations), f"{name}: every runtime egress rule must declare `to`")
            for destination in destinations or []:
                cidr = nested(destination, "ipBlock", "cidr")
                if cidr not in broad_cidrs:
                    continue
                exception_is_valid = (
                    allow_local_exception
                    and annotations.get("platform.ai/local-model-pull-egress") == "true"
                    and labels.get("platform.ai/environment") == "local"
                )
                require(
                    errors,
                    exception_is_valid,
                    f"{name}: broad model-pull egress is allowed only for the labeled local bootstrap exception",
                )


def check_ollama_render(name: str, docs: list[dict[str, Any]], errors: list[str]) -> None:
    statefulsets = find_kind(docs, "StatefulSet")
    require(errors, len(statefulsets) == 1, f"{name}: expected one Ollama StatefulSet")
    if not statefulsets:
        return
    statefulset = statefulsets[0]
    pod_spec = statefulset["spec"]["template"]["spec"]
    ollama = container(statefulset)
    security = ollama.get("securityContext", {})
    mounts = {mount.get("name"): mount for mount in ollama.get("volumeMounts", [])}
    volumes = {volume.get("name") for volume in pod_spec.get("volumes", [])}
    check_runtime_egress(name, docs, errors, allow_local_exception=name == "local-ollama")

    require(
        errors,
        pod_spec.get("automountServiceAccountToken") is False,
        f"{name}: Ollama pod must disable service account token automount",
    )
    require(
        errors, nested(pod_spec, "securityContext", "runAsNonRoot") is True, f"{name}: Ollama pod must run as non-root"
    )
    require(
        errors, "topologySpreadConstraints" in pod_spec, f"{name}: Ollama should render topology spread constraints"
    )
    require(
        errors, security.get("allowPrivilegeEscalation") is False, f"{name}: Ollama must block privilege escalation"
    )
    require(
        errors, security.get("readOnlyRootFilesystem") is True, f"{name}: Ollama must use a read-only root filesystem"
    )
    require(
        errors,
        "ALL" in nested(security, "capabilities", "drop", default=[]),
        f"{name}: Ollama must drop all Linux capabilities",
    )
    require(
        errors,
        "data" in mounts and mounts["data"].get("mountPath") == "/models",
        f"{name}: Ollama must mount writable model storage at /models",
    )
    require(
        errors,
        "tmp" in mounts and mounts["tmp"].get("mountPath") == "/tmp",
        f"{name}: Ollama must mount writable tmp storage",
    )
    require(errors, "tmp" in volumes, f"{name}: Ollama must define a tmp emptyDir volume")
    for init in pod_spec.get("initContainers", []):
        init_security = init.get("securityContext", {})
        init_mounts = {mount.get("name"): mount for mount in init.get("volumeMounts", [])}
        require(
            errors,
            init_security.get("allowPrivilegeEscalation") is False,
            f"{name}: Ollama init container must block privilege escalation",
        )
        require(
            errors,
            init_security.get("readOnlyRootFilesystem") is True,
            f"{name}: Ollama init container must use a read-only root filesystem",
        )
        require(
            errors,
            "ALL" in nested(init_security, "capabilities", "drop", default=[]),
            f"{name}: Ollama init container must drop all Linux capabilities",
        )
        require(
            errors,
            "data" in init_mounts and "tmp" in init_mounts,
            f"{name}: Ollama init container must mount model and tmp storage",
        )
    require(errors, bool(find_kind(docs, "Service")), f"{name}: Ollama chart must render a Service")
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: Ollama chart must render a ServiceAccount")
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), f"{name}: Ollama chart must render a PodDisruptionBudget"
    )


def check_qdrant_render(name: str, docs: list[dict[str, Any]], expect_pvc: bool, errors: list[str]) -> None:
    deployments = find_kind(docs, "Deployment")
    require(errors, len(deployments) == 1, f"{name}: expected one Qdrant Deployment")
    if deployments:
        deployment = deployments[0]
        pod_spec = deployment["spec"]["template"]["spec"]
        qdrant = container(deployment)
        security = qdrant.get("securityContext", {})
        image = qdrant.get("image", "")
        require(errors, ":latest" not in image, f"{name}: Qdrant image tag must be pinned")
        require(
            errors,
            pod_spec.get("automountServiceAccountToken") is False,
            f"{name}: Qdrant pod must disable service account token automount",
        )
        require(
            errors,
            nested(pod_spec, "securityContext", "runAsNonRoot") is True,
            f"{name}: Qdrant pod must run as non-root",
        )
        require(
            errors, security.get("allowPrivilegeEscalation") is False, f"{name}: Qdrant must block privilege escalation"
        )
        require(
            errors,
            security.get("readOnlyRootFilesystem") is True,
            f"{name}: Qdrant must use a read-only root filesystem",
        )
        require(
            errors,
            "ALL" in nested(security, "capabilities", "drop", default=[]),
            f"{name}: Qdrant must drop all Linux capabilities",
        )
        require(errors, bool(nested(qdrant, "resources", "requests")), f"{name}: Qdrant must set resource requests")
        require(errors, bool(nested(qdrant, "resources", "limits")), f"{name}: Qdrant must set resource limits")
        mounts = {mount.get("name"): mount for mount in qdrant.get("volumeMounts", [])}
        require(errors, "storage" in mounts, f"{name}: Qdrant must mount writable storage")
        require(errors, "snapshots" in mounts, f"{name}: Qdrant must mount writable snapshots storage")
        require(
            errors,
            "tmp" in mounts and mounts["tmp"].get("mountPath") == "/tmp",
            f"{name}: Qdrant must mount writable tmp storage",
        )
        ports = {port.get("name") for port in qdrant.get("ports", [])}
        require(errors, {"http", "grpc"} <= ports, f"{name}: Qdrant must expose HTTP and gRPC ports")
    require(errors, bool(find_kind(docs, "Service")), f"{name}: Qdrant chart must render a Service")
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: Qdrant chart must render a ServiceAccount")
    service_accounts = find_kind(docs, "ServiceAccount")
    if service_accounts:
        require(
            errors,
            service_accounts[0].get("automountServiceAccountToken") is False,
            f"{name}: Qdrant ServiceAccount must disable token automount",
        )
    require(errors, bool(find_kind(docs, "NetworkPolicy")), f"{name}: Qdrant chart must render a NetworkPolicy")
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), f"{name}: Qdrant chart must render a PodDisruptionBudget"
    )
    deployments = find_kind(docs, "Deployment")
    if deployments:
        volumes = {volume.get("name") for volume in deployments[0]["spec"]["template"]["spec"].get("volumes", [])}
        require(
            errors,
            {"storage", "snapshots", "tmp"} <= volumes,
            f"{name}: Qdrant must define storage, snapshots, and tmp volumes",
        )
    if expect_pvc:
        require(
            errors,
            bool(find_kind(docs, "PersistentVolumeClaim")),
            f"{name}: customer Qdrant profile must render a PersistentVolumeClaim",
        )


def check_rag_render(name: str, docs: list[dict[str, Any]], expect_hpa: bool, errors: list[str]) -> None:
    deployments = find_kind(docs, "Deployment")
    require(errors, len(deployments) == 1, f"{name}: expected one RAG Deployment")
    if deployments:
        deployment = deployments[0]
        pod_spec = deployment["spec"]["template"]["spec"]
        rag = container(deployment)
        security = rag.get("securityContext", {})
        image = rag.get("image", "")
        required_env = {
            "RAG_DOCUMENT_DIR",
            "RAG_RETRIEVAL_BACKEND",
            "DEFAULT_SANDBOX_ID",
            "AUDIT_LOG_ENABLED",
            "MAX_QUERY_CHARS",
            "MAX_REQUEST_BODY_BYTES",
            "DEFAULT_TOP_K",
            "MAX_TOP_K",
            "MAX_CONTEXT_CHARS",
            "QDRANT_URL",
            "QDRANT_COLLECTION",
            "QDRANT_TIMEOUT_SECONDS",
            "QDRANT_VECTOR_DIMENSIONS",
            "QDRANT_BOOTSTRAP_FROM_KNOWLEDGE",
            "API_KEY_AUTH_ENABLED",
            "API_KEY_HEADER",
            "API_KEY_SHA256S",
            "OTEL_TRACING_ENABLED",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_SERVICE_NAME",
        }
        missing_env = required_env - env_names(deployment)
        env = {item.get("name"): item.get("value") for item in rag.get("env", [])}
        require(errors, ":latest" not in image, f"{name}: RAG image tag must be pinned")
        require(
            errors,
            pod_spec.get("automountServiceAccountToken") is False,
            f"{name}: RAG pod must disable service account token automount",
        )
        require(
            errors, nested(pod_spec, "securityContext", "runAsNonRoot") is True, f"{name}: RAG pod must run as non-root"
        )
        require(
            errors, "topologySpreadConstraints" in pod_spec, f"{name}: RAG should render topology spread constraints"
        )
        require(errors, not missing_env, f"{name}: RAG Deployment is missing env vars: {sorted(missing_env)}")
        require(
            errors,
            env.get("MAX_REQUEST_BODY_BYTES") == "1048576",
            f"{name}: MAX_REQUEST_BODY_BYTES must render as a base-10 integer",
        )
        require(
            errors, security.get("allowPrivilegeEscalation") is False, f"{name}: RAG must block privilege escalation"
        )
        require(
            errors, security.get("readOnlyRootFilesystem") is True, f"{name}: RAG must use a read-only root filesystem"
        )
        require(
            errors,
            "ALL" in nested(security, "capabilities", "drop", default=[]),
            f"{name}: RAG must drop all Linux capabilities",
        )
        require(errors, bool(nested(rag, "resources", "requests")), f"{name}: RAG must set resource requests")
        require(errors, bool(nested(rag, "resources", "limits")), f"{name}: RAG must set resource limits")
        mounts = rag.get("volumeMounts", [])
        require(
            errors,
            any(mount.get("readOnly") is True for mount in mounts),
            f"{name}: RAG knowledge mount must be read-only",
        )
    require(errors, bool(find_kind(docs, "ConfigMap")), f"{name}: RAG chart must render a knowledge ConfigMap")
    require(errors, bool(find_kind(docs, "Service")), f"{name}: RAG chart must render a Service")
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: RAG chart must render a ServiceAccount")
    service_accounts = find_kind(docs, "ServiceAccount")
    if service_accounts:
        require(
            errors,
            service_accounts[0].get("automountServiceAccountToken") is False,
            f"{name}: RAG ServiceAccount must disable token automount",
        )
    require(errors, bool(find_kind(docs, "NetworkPolicy")), f"{name}: RAG chart must render a NetworkPolicy")
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), f"{name}: RAG chart must render a PodDisruptionBudget"
    )
    if expect_hpa:
        require(
            errors, bool(find_kind(docs, "HorizontalPodAutoscaler")), f"{name}: customer RAG profile must render an HPA"
        )


def check_agent_workspace_render(name: str, docs: list[dict[str, Any]], errors: list[str]) -> None:
    namespaces = find_kind(docs, "Namespace")
    require(errors, len(namespaces) == 1, f"{name}: agent workspace chart must render a Namespace")
    if namespaces:
        labels = namespaces[0].get("metadata", {}).get("labels", {})
        require(
            errors, labels.get("platform.ai/traceable-sandbox") == "true", f"{name}: agent Namespace must be traceable"
        )
        require(
            errors,
            labels.get("platform.ai/workload-kind") == "coding-agent",
            f"{name}: agent Namespace must identify coding-agent workload kind",
        )
        require(
            errors,
            labels.get("pod-security.kubernetes.io/enforce") == "restricted",
            f"{name}: agent Namespace must enforce restricted pod security",
        )
    require(errors, bool(find_kind(docs, "ResourceQuota")), f"{name}: agent workspace must render a ResourceQuota")
    require(errors, bool(find_kind(docs, "LimitRange")), f"{name}: agent workspace must render a LimitRange")
    require(
        errors, bool(find_kind(docs, "PersistentVolumeClaim")), f"{name}: agent workspace must render a workspace PVC"
    )
    require(
        errors, bool(find_kind(docs, "ConfigMap")), f"{name}: agent workspace must render a platform contract ConfigMap"
    )
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: agent workspace must render a ServiceAccount")
    service_accounts = find_kind(docs, "ServiceAccount")
    # Every ServiceAccount in the workspace namespace, not just whichever one renders
    # first: the guarantee is that no pod here gets a credential by default, and a
    # positional check would silently stop covering the agent as soon as the chart grew
    # another ServiceAccount. A workload that genuinely needs a token opts in per pod.
    for service_account in service_accounts:
        require(
            errors,
            service_account.get("automountServiceAccountToken") is False,
            f"{name}: ServiceAccount {nested(service_account, 'metadata', 'name', default='?')} "
            "must disable token automount",
        )
    require(errors, bool(find_kind(docs, "Role")), f"{name}: agent workspace must render namespace-scoped RBAC")
    require(errors, bool(find_kind(docs, "RoleBinding")), f"{name}: agent workspace must render a RoleBinding")
    policies = find_kind(docs, "NetworkPolicy")
    require(
        errors,
        len(policies) >= 2,
        f"{name}: agent workspace must render default-deny and approved-egress NetworkPolicies",
    )
    default_deny = next(
        (policy for policy in policies if policy.get("metadata", {}).get("name") == "agent-workspace-default-deny"),
        None,
    )
    require(errors, default_deny is not None, f"{name}: agent workspace missing default-deny NetworkPolicy")
    if default_deny:
        require(
            errors,
            "Ingress" in default_deny.get("spec", {}).get("policyTypes", []),
            f"{name}: agent default-deny must include Ingress",
        )
        require(
            errors,
            "Egress" in default_deny.get("spec", {}).get("policyTypes", []),
            f"{name}: agent default-deny must include Egress",
        )
        require(
            errors,
            "ingress" not in default_deny.get("spec", {}),
            f"{name}: agent default-deny must not define ingress allows",
        )
        require(
            errors,
            "egress" not in default_deny.get("spec", {}),
            f"{name}: agent default-deny must not define egress allows",
        )
    configmaps = find_kind(docs, "ConfigMap")
    if configmaps:
        data = configmaps[0].get("data", {})
        require(errors, "gateway-url" in data, f"{name}: agent platform contract must publish gateway-url")
        require(errors, "rag-url" in data, f"{name}: agent platform contract must publish rag-url")
        require(
            errors, "compliance-profile" in data, f"{name}: agent platform contract must publish compliance-profile"
        )
        require(
            errors, "data-classification" in data, f"{name}: agent platform contract must publish data-classification"
        )


def check_vllm_render(name: str, docs: list[dict[str, Any]], resource_name: str, errors: list[str]) -> None:
    deployments = find_kind(docs, "Deployment")
    require(errors, len(deployments) == 1, f"{name}: expected one vLLM Deployment")
    if not deployments:
        return
    deployment = deployments[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    vllm = container(deployment)
    vllm_security = vllm.get("securityContext", {})
    resources = vllm.get("resources", {})
    requests = resources.get("requests", {})
    limits = resources.get("limits", {})
    check_runtime_egress(name, docs, errors)
    args = vllm.get("args", [])
    require(errors, "--task" in args, f"{name}: vLLM must declare its serving task explicitly")
    if "embeddings" in name and "--task" in args:
        task_index = args.index("--task")
        require(
            errors,
            task_index + 1 < len(args) and args[task_index + 1] == "embed",
            f"{name}: embedding runtime must use --task embed",
        )

    require(
        errors,
        pod_spec.get("automountServiceAccountToken") is False,
        f"{name}: vLLM pod must disable service account token automount",
    )
    require(
        errors, nested(pod_spec, "securityContext", "runAsNonRoot") is True, f"{name}: vLLM pod must run as non-root"
    )
    require(
        errors,
        "topologySpreadConstraints" in pod_spec,
        f"{name}: vLLM should render topology spread constraints for multi-replica placement",
    )
    require(
        errors, vllm_security.get("allowPrivilegeEscalation") is False, f"{name}: vLLM must block privilege escalation"
    )
    require(
        errors,
        vllm_security.get("readOnlyRootFilesystem") is True,
        f"{name}: vLLM must use a read-only root filesystem",
    )
    require(
        errors,
        "ALL" in nested(vllm_security, "capabilities", "drop", default=[]),
        f"{name}: vLLM must drop all Linux capabilities",
    )
    require(errors, resource_name in requests, f"{name}: vLLM requests must include {resource_name}")
    require(errors, resource_name in limits, f"{name}: vLLM limits must include {resource_name}")
    env = {item.get("name"): item.get("value") for item in vllm.get("env", [])}
    require(errors, env.get("HF_HOME") == "/models", f"{name}: vLLM must redirect HF_HOME to writable model cache")
    require(
        errors,
        env.get("TRANSFORMERS_CACHE") == "/models",
        f"{name}: vLLM must redirect TRANSFORMERS_CACHE to writable model cache",
    )
    require(
        errors,
        env.get("VLLM_CACHE_ROOT") == "/models",
        f"{name}: vLLM must redirect VLLM_CACHE_ROOT to writable model cache",
    )
    mounts = {mount.get("name"): mount for mount in vllm.get("volumeMounts", [])}
    require(
        errors,
        "model-cache" in mounts and mounts["model-cache"].get("mountPath") == "/models",
        f"{name}: vLLM must mount writable model cache",
    )
    require(
        errors,
        "tmp" in mounts and mounts["tmp"].get("mountPath") == "/tmp",
        f"{name}: vLLM must mount writable tmp storage",
    )
    volumes = {volume.get("name") for volume in pod_spec.get("volumes", [])}
    require(errors, {"model-cache", "tmp"} <= volumes, f"{name}: vLLM must define model-cache and tmp volumes")
    require(errors, bool(find_kind(docs, "ServiceAccount")), f"{name}: vLLM chart must render a ServiceAccount")
    require(
        errors, bool(find_kind(docs, "PodDisruptionBudget")), f"{name}: vLLM chart must render a PodDisruptionBudget"
    )
    require(
        errors,
        bool(find_kind(docs, "HorizontalPodAutoscaler")) or bool(find_kind(docs, "ScaledObject")),
        f"{name}: vLLM profile must render autoscaling (HPA or a KEDA ScaledObject on a GPU/queue signal)",
    )
    # A GPU server must not scale on CPU utilization (the wrong signal: CPU% never
    # reflects GPU saturation, so the HPA never scales when it should). Use KEDA's
    # request-queue ScaledObject instead.
    for hpa in find_kind(docs, "HorizontalPodAutoscaler"):
        spec = hpa.get("spec", {})
        cpu_targeted = spec.get("targetCPUUtilizationPercentage") is not None or any(
            nested(metric, "resource", "name") == "cpu" for metric in spec.get("metrics", [])
        )
        require(
            errors,
            not cpu_targeted,
            f"{name}: vLLM must not autoscale on CPU (wrong signal for a GPU server); use the KEDA queue ScaledObject",
        )
