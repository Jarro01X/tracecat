"""Use this in worker to execute actions."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Any, NoReturn

import httpx
import orjson
from fastapi import status

from tracecat import config
from tracecat.clients import AuthenticatedServiceClient
from tracecat.contexts import ctx_role
from tracecat.dsl.models import RunActionInput
from tracecat.executor.models import ExecutorActionErrorInfo
from tracecat.logger import logger
from tracecat.registry.actions.models import (
    RegistryActionValidateResponse,
)
from tracecat.types.auth import Role
from tracecat.types.exceptions import (
    ExecutorClientError,
    RateLimitExceeded,
    RegistryError,
)


class ExecutorHTTPClient(AuthenticatedServiceClient):
    """Async httpx client for the executor service."""

    def __init__(self, role: Role | None = None, *args: Any, **kwargs: Any) -> None:
        self._executor_base_url = config.TRACECAT__EXECUTOR_URL
        super().__init__(role, *args, base_url=self._executor_base_url, **kwargs)
        self.params = self.params.add(
            "workspace_id", str(self.role.workspace_id) if self.role else None
        )


class ExecutorClient:
    """Use this to interact with the remote executor service."""

    _timeout: float = config.TRACECAT__EXECUTOR_CLIENT_TIMEOUT

    def __init__(self, role: Role | None = None):
        self.role = role or ctx_role.get()
        self.logger = logger.bind(service="executor-client", role=self.role)

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[ExecutorHTTPClient]:
        async with ExecutorHTTPClient(self.role) as client:
            yield client

    # === Execution ===

    async def run_action_memory_backend(self, input: RunActionInput) -> Any:
        action_type = input.task.action
        content = input.model_dump_json()
        logger.trace(
            f"Calling action {action_type!r} with content",
            content=content,
            role=self.role,
            timeout=self._timeout,
        )
        try:
            async with self._client() as client:
                # No need to include role headers here because it's already
                # added in AuthenticatedServiceClient
                response = await client.post(
                    f"/run/{action_type}",
                    headers={"Content-Type": "application/json"},
                    content=content,
                    timeout=self._timeout,
                )
            response.raise_for_status()
            return orjson.loads(response.content)
        except httpx.HTTPStatusError as e:
            self._handle_http_status_error(e, action_type)
        except httpx.ReadTimeout as e:
            raise ExecutorClientError(
                f"Timeout calling action {action_type!r} in executor: {e}"
            ) from e
        except orjson.JSONDecodeError as e:
            raise ExecutorClientError(
                f"Error decoding JSON response for action {action_type!r}: {e}"
            ) from e
        except Exception as e:
            raise ExecutorClientError(
                f"Unexpected error calling action {action_type!r} in executor: {e}"
            ) from e

    # === Validation ===

    async def validate_action(
        self, *, action_name: str, args: Mapping[str, Any]
    ) -> RegistryActionValidateResponse:
        """Validate an action."""
        try:
            logger.warning("Validating action")
            async with self._client() as client:
                response = await client.post(
                    f"/validate/{action_name}", json={"args": args}
                )
            response.raise_for_status()
            return RegistryActionValidateResponse.model_validate_json(response.content)
        except httpx.HTTPStatusError as e:
            raise RegistryError(
                f"Failed to list registries: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RegistryError(
                f"Network error while listing registries: {str(e)}"
            ) from e
        except Exception as e:
            raise RegistryError(
                f"Unexpected error while listing registries: {str(e)}"
            ) from e

    # === Utility ===

    def _handle_http_status_error(
        self, e: httpx.HTTPStatusError, action_type: str
    ) -> NoReturn:
        self.logger.info("Handling HTTP status error", error=e)
        if e.response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise RateLimitExceeded.from_response(e.response)
        try:
            resp = e.response.json()
        except JSONDecodeError:
            self.logger.warning("Failed to decode JSON response, returning empty dict")
            resp = {}
        match resp:
            case {"detail": detail} if _looks_like_error_info(detail):
                logger.debug("Looks like error info")
                detail = str(ExecutorActionErrorInfo(**detail))
            case {"detail": detail} if isinstance(detail, list) and all(
                _looks_like_error_info(r) for r in detail
            ):
                logger.debug("Looks like list of error info", n_errors=len(detail))
                length = len(detail)
                body = []
                if length > 1:
                    body.append(f"Showing the first of {length} similar errors.")
                body.append(str(ExecutorActionErrorInfo(**detail[0])))
                detail = "\n\n".join(body)
            case _:
                logger.debug("Looks like unknown error")
                detail = e.response.text
        logger.warning("Executor returned an error", error=detail)
        if e.response.status_code / 100 == 5:
            logger.warning("There was an error in the executor when calling action")
            raise ExecutorClientError(
                f"There was an error in the executor when calling action {action_type!r}.\n\n{detail}"
            ) from e
        else:
            logger.error("Unexpected executor error")
            raise ExecutorClientError(
                f"Unexpected executor error ({e.response.status_code}):\n\n{e}\n\n{detail}"
            ) from e


def _looks_like_error_info(detail: Any) -> bool:
    if not isinstance(detail, Mapping):
        return False
    # Check keyset
    detail_keyset = set(detail.keys())
    expected_keyset = set(ExecutorActionErrorInfo.model_fields.keys())
    return detail_keyset <= expected_keyset
