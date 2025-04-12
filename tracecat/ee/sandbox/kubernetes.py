"""Kubernetes pods operations.

Security hardening:
- Do not allow default namespace in kubeconfig contexts and functions
- Do not allow access to the current namespace (if running in a pod)
- Must be provided with kubeconfig as a secret (cannot be loaded from environment or default locations)
- Assumes that the pod has service account token and namespace file mounted (i.e. automountServiceAccountToken: True in the pod spec)

References
----------
- https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1PodList.md
- https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1PodSpec.md
- https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1Pod.md
- https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1Container.md
- https://martinheinz.dev/blog/73
"""

import base64
import os

from kubernetes import client, config
from kubernetes.client.models import V1Container, V1Pod, V1PodList, V1PodSpec
from kubernetes.stream import stream
from pydantic import BaseModel
from yaml import safe_load

from tracecat.logger import logger


class KubernetesResult(BaseModel):
    """Result from running a command in a Kubernetes pod.

    Parameters
    ----------
    pod: str
        Pod name that was used.
    container: str
        Container name that was used.
    namespace: str
        Namespace that the pod is in.
    command: list[str]
        Command that was executed.
    stdout: list[str]
        Standard output lines from the container.
    stderr: list[str]
        Standard error lines from the container.
    """

    pod: str
    container: str
    namespace: str
    command: list[str]
    stdout: list[str] | None = None
    stderr: list[str] | None = None


def _decode_kubeconfig(kubeconfig_base64: str) -> dict:
    """Decode base64 kubeconfig YAML file.

    Args:
        kubeconfig_base64: Base64 encoded kubeconfig YAML file.

    Returns:
        dict: Decoded kubeconfig YAML file.

    Raises:
        ValueError: If kubeconfig is invalid
    """
    # Decode base64 kubeconfig YAML file
    kubeconfig_dict = safe_load(base64.b64decode(kubeconfig_base64 + "=="))
    logger.info(
        "Loaded kubeconfig YAML into JSON with fields", fields=kubeconfig_dict.keys()
    )

    if not isinstance(kubeconfig_dict, dict):
        logger.warning("kubeconfig is not a dictionary")
        raise ValueError("kubeconfig must be a dictionary")

    if not kubeconfig_dict:
        logger.warning("Empty kubeconfig dictionary after decoding")
        raise ValueError("kubeconfig cannot be empty")

    contexts = kubeconfig_dict.get("contexts", [])
    if not contexts:
        logger.warning("Kubeconfig contains no contexts")
        raise ValueError("kubeconfig must contain at least one context")

    # Cannot contain default namespace
    for context in contexts:
        if context.get("namespace") == "default":
            logger.warning(
                "Kubeconfig contains default namespace",
                context_name=context.get("name"),
            )
            raise ValueError("kubeconfig cannot contain default namespace")
    return kubeconfig_dict


def _get_kubernetes_client(kubeconfig_base64: str) -> client.CoreV1Api:
    """Get Kubernetes client with explicit configuration.

    Args:
        kubeconfig_base64: Base64 encoded kubeconfig YAML file.

    Returns:
        CoreV1Api: Kubernetes API client

    Raises:
        ValueError: If kubeconfig is invalid
    """

    # kubeconfig must be provided
    if not kubeconfig_base64:
        logger.warning("Empty kubeconfig provided")
        raise ValueError("kubeconfig cannot be empty")

    # Load from explicit file, never from environment or default locations
    # NOTE: This is critical. We must not allow Kubernetes' default behavior of
    # using the kubeconfig from the environment.
    kubeconfig_dict = _decode_kubeconfig(kubeconfig_base64)
    config.load_kube_config_from_dict(config_dict=kubeconfig_dict)

    logger.info("Successfully validated and loaded kubeconfig")
    return client.CoreV1Api()


def _validate_access_allowed(namespace: str) -> None:
    """Validate if access to the namespace is allowed.

    Args:
        namespace: Namespace to check access for

    Raises:
        PermissionError: If access to the namespace is not allowed
    """

    logger.info("Validating namespace access permissions", namespace=namespace)
    current_namespace = None

    # Cannot be default namespace
    if namespace == "default":
        logger.warning("Attempted operation on default namespace")
        raise PermissionError(
            "Tracecat does not allow Kubernetes operations on the default namespace"
        )

    # Check if current namespace is the same as the provided namespace
    if "KUBERNETES_SERVICE_HOST" in os.environ:
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                current_namespace = f.read().strip()
        except FileNotFoundError as e:
            logger.warning("Kubernetes service account namespace file not found")
            raise FileNotFoundError(
                "Kubernetes service account namespace file not found"
            ) from e

        # Check if current namespace is the same as the provided namespace
        if current_namespace == namespace:
            logger.warning(
                "Attempted operation on current namespace",
                current_namespace=current_namespace,
            )
            raise PermissionError(
                f"Tracecat does not allow Kubernetes operations on the current namespace {current_namespace!r}"
            )

    logger.info(
        "Namespace access validated",
        namespace=namespace,
        current_namespace=current_namespace,
    )


def list_kubernetes_pods(namespace: str, kubeconfig_base64: str) -> list[str]:
    """List all pods in the given namespace.

    Args:
        namespace : str
            The namespace to list pods from. Must not be the current namespace.
        kubeconfig_base64 : str
            Base64 encoded kubeconfig YAML file. Required for security isolation.

    Returns:
        list[str]: List of pod names in the namespace.

    Raises:
        PermissionError: If trying to access current namespace
        ValueError: If no pods found or invalid arguments
    """
    logger.info("Listing kubernetes pods", namespace=namespace)
    _validate_access_allowed(namespace)
    client = _get_kubernetes_client(kubeconfig_base64)

    pods: V1PodList = client.list_namespaced_pod(namespace=namespace)
    if pods.items is None:
        logger.warning("No pods found in namespace", namespace=namespace)
        raise ValueError(f"No pods found in namespace {namespace}")
    items: list[V1Pod] = pods.items
    pod_names = [pod.metadata.name for pod in items]  # type: ignore

    logger.info(
        "Successfully listed pods", namespace=namespace, pod_count=len(pod_names)
    )
    return pod_names


def list_kubernetes_containers(
    pod: str, namespace: str, kubeconfig_base64: str
) -> list[str]:
    """List all containers in a given pod.

    Args:
        pod : str
            Name of the pod to list containers from.
        namespace : str
            Namespace where the pod is located. Must not be the current namespace.
        kubeconfig_base64 : str
            Base64 encoded kubeconfig YAML file. Required for security isolation.

    Returns:
        list[str]: List of container names in the pod.

    Raises:
        PermissionError: If trying to access current namespace
        ValueError: If invalid pod or no containers found
    """
    logger.info("Listing kubernetes containers", pod=pod, namespace=namespace)
    _validate_access_allowed(namespace)
    client = _get_kubernetes_client(kubeconfig_base64)

    pod_info: V1Pod = client.read_namespaced_pod(
        name=pod,
        namespace=namespace,
    )  # type: ignore
    if pod_info.spec is None:
        logger.warning("Pod has no spec", pod=pod, namespace=namespace)
        raise ValueError(f"Pod {pod} in namespace {namespace} has no spec")

    spec: V1PodSpec = pod_info.spec
    if spec.containers is None:
        logger.warning("Pod has no containers", pod=pod, namespace=namespace)
        raise ValueError(f"Pod {pod} in namespace {namespace} has no containers")

    containers: list[V1Container] = spec.containers
    container_names = [
        str(container.name) for container in containers if container.name is not None
    ]

    logger.info("Successfully listed containers", pod=pod, namespace=namespace)
    return container_names


def exec_kubernetes_pod(
    pod: str,
    command: str | list[str],
    namespace: str,
    kubeconfig_base64: str,
    container: str | None = None,
    timeout: int = 60,
) -> KubernetesResult:
    """Execute a command in a Kubernetes pod.

    Args:
        pod : str
            Name of the pod to execute command in.
        command : str | list[str]
            Command to execute in the pod.
        namespace : str
            Namespace where the pod is located. Must not be the current namespace.
        kubeconfig_base64 : str
            Base64 encoded kubeconfig YAML file. Required for security isolation.
        container : str | None, default=None
            Name of the container to execute command in. If None, uses the first container.
        timeout : int, default=60
            Timeout in seconds for the command execution.

    Returns:
        KubernetesResult: Object containing stdout and stderr from the command.

    Raises:
        PermissionError: If trying to access current namespace
        RuntimeError: If the command execution fails
        ValueError: If invalid arguments provided
    """
    cmd = command if isinstance(command, list) else [command]
    logger.info(
        "Executing command in kubernetes pod", pod=pod, namespace=namespace, command=cmd
    )

    _validate_access_allowed(namespace)

    # Convert string command to list
    if isinstance(command, str):
        command = [command]

    client = _get_kubernetes_client(kubeconfig_base64)

    if container is None:
        containers = list_kubernetes_containers(pod, namespace, kubeconfig_base64)
        container = containers[0]
        logger.info(
            "Using first container", pod=pod, namespace=namespace, container=container
        )

    try:
        resp = stream(
            client.connect_get_namespaced_pod_exec(
                name=pod,
                namespace=namespace,
                command=command,
                container=container,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
                _request_timeout=timeout,
            )
        )
        # Split output into lines
        stdout = resp.read_stdout().splitlines() if resp.peek_stdout() else []
        stderr = resp.read_stderr().splitlines() if resp.peek_stderr() else []

        if stderr:
            logger.warning(
                "Unexpected stderr output from Kubernetes pod exec",
                pod=pod,
                container=container,
                namespace=namespace,
                command=command,
                stderr=stderr,
            )
            raise RuntimeError(f"Got stderr from Kubernetes pod exec: {stderr!r}")

        logger.info(
            "Successfully executed command",
            pod=pod,
            namespace=namespace,
            container=container,
            stdout_lines=len(stdout),
        )

        return KubernetesResult(
            pod=pod,
            container=container,
            namespace=namespace,
            command=command,
            stdout=stdout,
            stderr=stderr,
        )

    except Exception as e:
        logger.warning(
            "Unexpected error executing Kubernetes command",
            pod=pod,
            container=container,
            namespace=namespace,
            command=command,
            error=str(e),
        )
        raise e
