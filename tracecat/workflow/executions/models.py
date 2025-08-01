from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
    cast,
)

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.history.v1
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer
from temporalio.client import WorkflowExecution, WorkflowExecutionStatus

from tracecat.dsl.common import (
    ChildWorkflowMemo,
    DSLRunArgs,
    get_trigger_type_from_search_attr,
)
from tracecat.dsl.enums import JoinStrategy, PlatformAction, WaitStrategy
from tracecat.dsl.models import (
    ROOT_STREAM,
    ActionErrorInfo,
    ActionRetryPolicy,
    RunActionInput,
    StreamID,
    TriggerInputs,
)
from tracecat.ee.interactions.models import (
    InteractionInput,
    InteractionRead,
    InteractionResult,
)
from tracecat.identifiers import WorkflowExecutionID, WorkflowID
from tracecat.identifiers.workflow import AnyWorkflowID, WorkflowUUID
from tracecat.logger import logger
from tracecat.types.auth import Role
from tracecat.workflow.executions.common import (
    HISTORY_TO_WF_EVENT_TYPE,
    UTILITY_ACTIONS,
    extract_first,
    is_utility_activity,
)
from tracecat.workflow.executions.enums import (
    TriggerType,
    WorkflowEventType,
    WorkflowExecutionEventStatus,
)
from tracecat.workflow.management.models import GetWorkflowDefinitionActivityInputs

WorkflowExecutionStatusLiteral = Literal[
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "TERMINATED",
    "CONTINUED_AS_NEW",
    "TIMED_OUT",
]
"""Mapped literal types for workflow execution statuses."""


class WorkflowExecutionBase(BaseModel):
    id: str = Field(..., description="The ID of the workflow execution")
    run_id: str = Field(..., description="The run ID of the workflow execution")
    start_time: datetime = Field(
        ..., description="The start time of the workflow execution"
    )
    execution_time: datetime | None = Field(
        None, description="When this workflow run started or should start."
    )
    close_time: datetime | None = Field(
        None, description="When the workflow was closed if closed."
    )
    status: Annotated[
        WorkflowExecutionStatus | None,
        PlainSerializer(
            lambda x: x.name if x else None,
            return_type=WorkflowExecutionStatusLiteral,
            when_used="always",
        ),
    ]

    workflow_type: str
    task_queue: str
    history_length: int = Field(..., description="Number of events in the history")
    parent_wf_exec_id: WorkflowExecutionID | None = None
    trigger_type: TriggerType


class WorkflowExecutionReadMinimal(WorkflowExecutionBase):
    @staticmethod
    def from_dataclass(execution: WorkflowExecution) -> WorkflowExecutionReadMinimal:
        return WorkflowExecutionReadMinimal(
            id=execution.id,
            run_id=execution.run_id,
            start_time=execution.start_time,
            execution_time=execution.execution_time,
            close_time=execution.close_time,
            status=execution.status,
            workflow_type=execution.workflow_type,
            task_queue=execution.task_queue,
            history_length=execution.history_length,
            parent_wf_exec_id=execution.parent_id,
            trigger_type=get_trigger_type_from_search_attr(
                execution.typed_search_attributes, execution.id
            ),
        )


class WorkflowExecutionRead(WorkflowExecutionBase):
    events: list[WorkflowExecutionEvent] = Field(
        ..., description="The events in the workflow execution"
    )
    interactions: list[InteractionRead] = Field(
        default_factory=list,
        description="The interactions in the workflow execution",
    )


class WorkflowExecutionReadCompact[TInput: Any, TResult: Any](WorkflowExecutionBase):
    events: list[WorkflowExecutionEventCompact[TInput, TResult]] = Field(
        ..., description="Compact events in the workflow execution"
    )
    interactions: list[InteractionRead] = Field(
        default_factory=list,
        description="The interactions in the workflow execution",
    )


def destructure_slugified_namespace(s: str, delimiter: str = "__") -> tuple[str, str]:
    *stem, leaf = s.split(delimiter)
    return (".".join(stem), leaf)


EventInput = (
    RunActionInput
    | DSLRunArgs
    | GetWorkflowDefinitionActivityInputs
    | InteractionResult
    | InteractionInput
)


class EventGroup[T: EventInput](BaseModel):
    event_id: int
    udf_namespace: str
    udf_name: str
    udf_key: str
    action_id: str | None = None
    action_ref: str | None = None
    action_title: str | None = None
    action_description: str | None = None
    action_input: T
    action_result: Any | None = None
    current_attempt: int | None = None
    retry_policy: ActionRetryPolicy = Field(default_factory=ActionRetryPolicy)
    start_delay: float = 0.0
    join_strategy: JoinStrategy = JoinStrategy.ALL
    related_wf_exec_id: WorkflowExecutionID | None = None

    @staticmethod
    async def from_scheduled_activity(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> EventGroup[EventInput] | None:
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ):
            raise ValueError("Event is not an activity task scheduled event.")
        # Load the input data
        attrs = event.activity_task_scheduled_event_attributes
        activity_input_data = await extract_first(attrs.input)

        act_type = attrs.activity_type.name
        if is_utility_activity(act_type):
            return None
        if act_type == "get_workflow_definition_activity":
            action_input = GetWorkflowDefinitionActivityInputs(**activity_input_data)
        else:
            action_input = RunActionInput(**activity_input_data)
        if action_input.task is None:
            # It's a utility action.
            return None
        # Create an event group
        task = action_input.task
        action_retry_policy = task.retry_policy

        namespace, task_name = destructure_slugified_namespace(
            task.action, delimiter="."
        )
        return EventGroup(
            event_id=event.event_id,
            udf_namespace=namespace,
            udf_name=task_name,
            udf_key=task.action,
            action_id=task.id,
            action_ref=task.ref,
            action_title=task.title,
            action_description=task.description,
            action_input=cast(EventInput, action_input),
            retry_policy=action_retry_policy,
            start_delay=task.start_delay,
            join_strategy=task.join_strategy,
        )

    @staticmethod
    async def from_initiated_child_workflow(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> EventGroup[DSLRunArgs]:
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        ):
            raise ValueError("Event is not a child workflow initiated event.")

        attrs = event.start_child_workflow_execution_initiated_event_attributes
        wf_exec_id = cast(WorkflowExecutionID, attrs.workflow_id)
        input = await extract_first(attrs.input)
        dsl_run_args = DSLRunArgs(**input)
        # Create an event group

        if dsl := dsl_run_args.dsl:
            action_title = dsl.title
            action_description = dsl.description
        else:
            action_title = None
            action_description = None

        wf_id = WorkflowUUID.new(dsl_run_args.wf_id)
        return EventGroup(
            event_id=event.event_id,
            udf_namespace="core.workflow",
            udf_name="execute",
            udf_key="core.workflow.execute",
            action_id=wf_id.short(),
            action_ref=None,
            action_title=action_title,
            action_description=action_description,
            action_input=dsl_run_args,
            related_wf_exec_id=wf_exec_id,
        )

    @staticmethod
    async def from_accepted_workflow_update(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> EventGroup[InteractionInput]:
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
            or not event.HasField("workflow_execution_update_accepted_event_attributes")
        ):
            raise ValueError("Event is not a workflow update accepted event.")

        attrs = event.workflow_execution_update_accepted_event_attributes
        input = await extract_first(attrs.accepted_request.input.args)
        group = EventGroup(
            event_id=event.event_id,
            udf_namespace="core.interact",
            udf_name="response",
            udf_key="core.interact.response",
            action_input=InteractionInput(**input),
        )
        logger.debug(
            "Workflow update accepted event", event_id=event.event_id, group=group
        )
        return group


class EventFailure(BaseModel):
    message: str
    cause: dict[str, Any] | None = None

    @staticmethod
    def from_history_event(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> EventFailure:
        match event.event_type:
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                failure = event.activity_task_failed_event_attributes.failure
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
                failure = event.workflow_execution_failed_event_attributes.failure
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_FAILED:
                failure = event.child_workflow_execution_failed_event_attributes.failure
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED:
                failure = event.workflow_execution_update_completed_event_attributes.outcome.failure
            case _:
                raise ValueError("Event type not supported for failure extraction.")

        return EventFailure(
            message=failure.message,
            cause=MessageToDict(failure.cause) if failure.cause is not None else None,
        )


class WorkflowExecutionEvent[T: EventInput](BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    event_id: int
    event_time: datetime
    event_type: WorkflowEventType
    task_id: int
    event_group: EventGroup[T] | None = Field(
        default=None,
        description="The action group of the event. We use this to keep track of what events are related to each other.",
    )
    failure: EventFailure | None = None
    result: Any | None = None
    role: Role | None = None
    parent_wf_exec_id: WorkflowExecutionID | None = None
    workflow_timeout: float | None = None


class WorkflowExecutionEventCompact[TInput: Any, TResult: Any](BaseModel):
    """A compact representation of a workflow execution event."""

    source_event_id: int
    """The event ID of the source event."""
    schedule_time: datetime
    start_time: datetime | None = None
    close_time: datetime | None = None
    curr_event_type: WorkflowEventType
    """The type of the event."""
    status: WorkflowExecutionEventStatus
    action_name: str
    action_ref: str
    action_input: TInput | None = None
    action_result: TResult | None = None
    action_error: EventFailure | None = None
    stream_id: StreamID = ROOT_STREAM
    child_wf_exec_id: WorkflowExecutionID | None = None
    child_wf_count: int = 0
    loop_index: int | None = None
    child_wf_wait_strategy: WaitStrategy | None = None

    @staticmethod
    async def from_source_event(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> WorkflowExecutionEventCompact | None:
        match event.event_type:
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                return await WorkflowExecutionEventCompact.from_scheduled_activity(
                    event
                )
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
                return (
                    await WorkflowExecutionEventCompact.from_initiated_child_workflow(
                        event
                    )
                )
            case temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
                return (
                    await WorkflowExecutionEventCompact.from_workflow_update_accepted(
                        event
                    )
                )
            case _:
                return None

    @staticmethod
    async def from_scheduled_activity(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> WorkflowExecutionEventCompact | None:
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ):
            raise ValueError("Event is not an activity task scheduled event.")
        attrs = event.activity_task_scheduled_event_attributes
        activity_input_data = await extract_first(attrs.input)

        act_type = attrs.activity_type.name
        if act_type in (UTILITY_ACTIONS | {"get_workflow_definition_activity"}):
            logger.trace("Utility action is not supported.", act_type=act_type)
            return None
        action_input = RunActionInput(**activity_input_data)
        task = action_input.task
        if task is None:
            logger.debug("Action input is None", event_id=event.event_id)
            return None

        return WorkflowExecutionEventCompact(
            source_event_id=event.event_id,
            schedule_time=event.event_time.ToDatetime(UTC),
            curr_event_type=HISTORY_TO_WF_EVENT_TYPE[event.event_type],
            status=WorkflowExecutionEventStatus.SCHEDULED,
            action_name=task.action,
            action_ref=task.ref,
            action_input=task.args,
            stream_id=action_input.stream_id,
        )

    @staticmethod
    async def from_initiated_child_workflow(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> WorkflowExecutionEventCompact | None:
        """Creates a compact workflow execution event from a child workflow initiation event.

        Args:
            event: The temporal history event representing a child workflow initiation

        Returns:
            WorkflowExecutionEventCompact | None: The compact event representation, or None if invalid
        """
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        ):
            raise ValueError("Event is not a child workflow initiated event.")

        attrs = event.start_child_workflow_execution_initiated_event_attributes
        wf_exec_id = cast(WorkflowExecutionID, attrs.workflow_id)
        try:
            memo = ChildWorkflowMemo.from_temporal(attrs.memo)
        except Exception as e:
            logger.error("Error parsing child workflow memo", error=e)
            raise e

        if (
            attrs.parent_close_policy
            == temporalio.api.enums.v1.ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
            and memo.wait_strategy == WaitStrategy.DETACH
        ):
            status = WorkflowExecutionEventStatus.DETACHED
        else:
            status = WorkflowExecutionEventStatus.SCHEDULED
        logger.info(
            "Child workflow initiated event",
            status=status,
            wf_exec_id=wf_exec_id,
            memo=memo,
        )

        input_data = await extract_first(attrs.input)
        dsl_run_args = DSLRunArgs(**input_data)

        return WorkflowExecutionEventCompact(
            source_event_id=event.event_id,
            schedule_time=event.event_time.ToDatetime(UTC),
            curr_event_type=HISTORY_TO_WF_EVENT_TYPE[event.event_type],
            status=status,
            action_name=PlatformAction.CHILD_WORKFLOW_EXECUTE.value,
            action_ref=memo.action_ref,
            action_input=dsl_run_args.trigger_inputs,
            child_wf_exec_id=wf_exec_id,
            loop_index=memo.loop_index,
            child_wf_wait_strategy=memo.wait_strategy,
            stream_id=memo.stream_id,
        )

    @staticmethod
    async def from_workflow_update_accepted(
        event: temporalio.api.history.v1.HistoryEvent,
    ) -> WorkflowExecutionEventCompact | None:
        if (
            event.event_type
            != temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
        ):
            raise ValueError("Event is not a workflow update accepted event.")

        attrs = event.workflow_execution_update_accepted_event_attributes
        input_data = await extract_first(attrs.accepted_request.input.args)
        signal_input = InteractionInput(**input_data)
        return WorkflowExecutionEventCompact(
            source_event_id=event.event_id,
            schedule_time=event.event_time.ToDatetime(UTC),
            curr_event_type=HISTORY_TO_WF_EVENT_TYPE[event.event_type],
            status=WorkflowExecutionEventStatus.SCHEDULED,
            action_name=signal_input.action_ref,
            action_ref=signal_input.action_ref,
            action_input=signal_input,
        )


class WorkflowExecutionCreate(BaseModel):
    workflow_id: AnyWorkflowID
    inputs: TriggerInputs | None = None


class WorkflowExecutionCreateResponse(TypedDict):
    message: str
    wf_id: WorkflowID
    wf_exec_id: WorkflowExecutionID
    payload: NotRequired[Any]
    """The HTTP request body of the request that triggered the workflow."""


class WorkflowDispatchResponse(TypedDict):
    wf_id: WorkflowID
    result: Any


class WorkflowExecutionTerminate(BaseModel):
    reason: str | None = None


@dataclass(frozen=True)
class ErrorHandlerWorkflowInput:
    message: str
    handler_wf_id: WorkflowID
    orig_wf_id: WorkflowID
    orig_wf_exec_id: WorkflowExecutionID
    orig_wf_title: str
    trigger_type: TriggerType
    errors: list[ActionErrorInfo] | None = None
    orig_wf_exec_url: str | None = None


class ReceiveInteractionResponse(BaseModel):
    message: str
