"""Database schemas for Tracecat."""

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import UUID4, BaseModel, ConfigDict, computed_field
from sqlalchemy import TIMESTAMP, Column, ForeignKey, Identity, Index, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import UUID, Field, Relationship, SQLModel, UniqueConstraint

from tracecat import config
from tracecat.auth.models import UserRole
from tracecat.authz.models import WorkspaceRole
from tracecat.cases.enums import (
    CaseEventType,
    CasePriority,
    CaseSeverity,
    CaseStatus,
)
from tracecat.db.adapter import (
    SQLModelBaseAccessToken,
    SQLModelBaseOAuthAccount,
    SQLModelBaseUserDB,
)
from tracecat.ee.interactions.enums import InteractionStatus, InteractionType
from tracecat.identifiers import OwnerID, action, id_factory
from tracecat.identifiers.workflow import WorkflowUUID
from tracecat.integrations.enums import IntegrationStatus, OAuthGrantType
from tracecat.secrets.constants import DEFAULT_SECRETS_ENVIRONMENT

DEFAULT_SA_RELATIONSHIP_KWARGS = {
    "lazy": "selectin",
}


class TimestampMixin(BaseModel):
    created_at: datetime = Field(
        default_factory=datetime.now,  # Appease type checker
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        sa_column_kwargs={
            "server_default": func.now(),
            "nullable": False,
        },
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,  # Appease type checker
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
            "nullable": False,
        },
    )


class Resource(SQLModel, TimestampMixin):
    """Base class for all resources in the system."""

    surrogate_id: int | None = Field(default=None, primary_key=True, exclude=True)
    owner_id: OwnerID


class OAuthAccount(SQLModelBaseOAuthAccount, table=True):
    user_id: UUID4 = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="oauth_accounts")


class Membership(SQLModel, table=True):
    """Link table for users and workspaces (many to many)."""

    user_id: UUID4 = Field(foreign_key="user.id", primary_key=True)
    workspace_id: UUID4 = Field(foreign_key="workspace.id", primary_key=True)
    role: WorkspaceRole = Field(
        default=WorkspaceRole.EDITOR,
        description="User's role in this workspace",
        nullable=False,
    )


class Ownership(SQLModel, table=True):
    """Table to map resources to owners.

    - Organization owns all workspaces
    - One specific user owns the organization
    - Workspaces own all resources (e.g. workflows, secrets) except itself

    Three types of owners:
    - User
    - Workspace
    - Organization (given by a  UUID4 sentinel value created on database creation)
    """

    resource_id: str = Field(nullable=False, primary_key=True)
    resource_type: str
    owner_id: OwnerID
    owner_type: str


class Workspace(Resource, table=True):
    id: UUID4 = Field(default_factory=uuid.uuid4, nullable=False, unique=True)
    name: str = Field(..., index=True, nullable=False)
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    members: list["User"] = Relationship(
        back_populates="workspaces",
        link_model=Membership,
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )
    workflows: list["Workflow"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    secrets: list["Secret"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    tags: list["Tag"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    folders: list["WorkflowFolder"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    integrations: list["OAuthIntegration"] = Relationship(
        back_populates="owner",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )

    @computed_field
    @property
    def n_members(self) -> int:
        return len(self.members)


class User(SQLModelBaseUserDB, table=True):
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    role: UserRole = Field(nullable=False, default=UserRole.BASIC)
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    last_login_at: datetime | None = Field(
        sa_column=Column(TIMESTAMP(timezone=True)),
    )
    # Relationships
    oauth_accounts: list["OAuthAccount"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    workspaces: list["Workspace"] = Relationship(
        back_populates="members",
        link_model=Membership,
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )
    assigned_cases: list["Case"] = Relationship(
        back_populates="assignee",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )
    access_tokens: list["AccessToken"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class AccessToken(SQLModelBaseAccessToken, table=True):
    id: UUID4 = Field(default_factory=uuid.uuid4, nullable=False, unique=True)
    user: "User" = Relationship(
        back_populates="access_tokens",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class SAMLRequestData(SQLModel, table=True):
    """Stores SAML request data for validating responses.

    This replaces the disk cache to support distributed environments like Fargate.
    """

    __tablename__: str = "saml_request_data"

    id: str = Field(primary_key=True, description="SAML Request ID")
    relay_state: str = Field(nullable=False)
    expires_at: datetime = Field(
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        nullable=False,
        description="Timestamp when this request data expires",
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        sa_column_kwargs={
            "server_default": func.now(),
            "nullable": False,
        },
    )


class BaseSecret(Resource):
    model_config: ConfigDict = ConfigDict(from_attributes=True)
    id: str = Field(
        default_factory=id_factory("secret"), nullable=False, unique=True, index=True
    )
    type: str = "custom"  # "custom", "token", "oauth2"
    name: str = Field(
        ...,
        max_length=255,
        index=True,
        nullable=False,
        description="Secret names should be unique within a user's scope.",
    )
    description: str | None = Field(default=None, max_length=255)
    # We store this object as encrypted bytes, but first validate that it's the correct type
    encrypted_keys: bytes
    environment: str = Field(default=DEFAULT_SECRETS_ENVIRONMENT, nullable=False)
    # Use sa_type over sa_column for inheritance
    tags: dict[str, str] | None = Field(sa_type=JSONB)

    def __repr__(self) -> str:
        return f"BaseSecret(name={self.name}, owner_id={self.owner_id}, environment={self.environment})"


class OrganizationSecret(BaseSecret, table=True):
    __table_args__ = (UniqueConstraint("name", "environment"),)


class Secret(BaseSecret, table=True):
    """Workspace secrets."""

    __table_args__ = (UniqueConstraint("name", "environment", "owner_id"),)

    owner_id: OwnerID = Field(
        sa_column=Column(UUID, ForeignKey("workspace.id", ondelete="CASCADE"))
    )
    owner: Workspace | None = Relationship(back_populates="secrets")


class WorkflowDefinition(Resource, table=True):
    """A workflow definition.

    This is the underlying representation/snapshot of a workflow in the system, which
    can directly execute in the runner.

    Shoulds
    -------
    1. Be convertible into a Workspace Workflow + Acitons
    2. Be convertible into a YAML DSL
    3. Be able to be versioned

    Shouldn'ts
    ----------
    1. Have any stateful information

    Relationships
    -------------
    - 1 Workflow to many WorkflowDefinitions

    """

    # Metadata
    id: str = Field(
        default_factory=id_factory("wf-defn"), nullable=False, unique=True, index=True
    )
    version: int = Field(..., index=True, description="DSL spec version")
    workflow_id: uuid.UUID = Field(
        sa_column=Column(UUID, ForeignKey("workflow.id", ondelete="CASCADE"))
    )

    # DSL content
    content: dict[str, Any] = Field(sa_column=Column(JSONB))
    workflow: "Workflow" = Relationship(
        back_populates="definitions",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class WorkflowTag(SQLModel, table=True):
    """Link table for workflows and tags with optional metadata."""

    tag_id: UUID4 = Field(foreign_key="tag.id", primary_key=True)
    workflow_id: uuid.UUID = Field(foreign_key="workflow.id", primary_key=True)


class WorkflowFolder(Resource, table=True):
    """Folder for organizing workflows.

    Uses materialized path pattern for hierarchical structure.
    Path format: "/parent/child/" where each segment is the folder name.
    Root folders have path "/foldername/".
    """

    __tablename__: str = "workflow_folder"
    __table_args__ = (
        UniqueConstraint("path", "owner_id", name="uq_workflow_folder_path_owner"),
    )
    # Owner (workspace)
    owner_id: OwnerID = Field(
        sa_column=Column(UUID, ForeignKey("workspace.id", ondelete="CASCADE"))
    )
    id: uuid.UUID = Field(
        default_factory=uuid.uuid4, nullable=False, unique=True, index=True
    )
    name: str = Field(..., description="Display name of the folder")
    path: str = Field(index=True, description="Full materialized path: /parent/child/")

    # Relationships
    owner: "Workspace" = Relationship(back_populates="folders")
    workflows: list["Workflow"] = Relationship(
        back_populates="folder",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )

    @property
    def parent_path(self) -> str:
        """Get the parent path of this folder."""
        if self.path == "/":
            return "/"

        # Remove trailing slash, split by slashes, remove the last segment, join back
        path_parts = self.path.rstrip("/").split("/")
        if len(path_parts) <= 2:  # Root level folder like "/folder/"
            return "/"

        return "/".join(path_parts[:-1]) + "/"

    @property
    def is_root(self) -> bool:
        """Check if this is a root-level folder."""
        return self.path.count("/") <= 2  # "/foldername/" has two slashes


class Tag(Resource, table=True):
    """A tag for organizing and filtering entities."""

    __table_args__ = (UniqueConstraint("name", "owner_id"),)

    id: UUID4 = Field(
        default_factory=uuid.uuid4, nullable=False, unique=True, index=True
    )
    owner_id: OwnerID = Field(
        sa_column=Column(UUID, ForeignKey("workspace.id", ondelete="CASCADE"))
    )
    name: str = Field(index=True, nullable=False)
    color: str | None = Field(default=None)
    # Relationships
    owner: "Workspace" = Relationship(back_populates="tags")
    workflows: list["Workflow"] = Relationship(
        back_populates="tags",
        link_model=WorkflowTag,
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class Workflow(Resource, table=True):
    """The workflow state.

    Notes
    -----
    - This table serves as the source of truth for the workflow regardless of operating
     mode (headless or not)
    - Workflow controls (status, scheduels, etc.) are stored and modified here
    - Workflow definitions are executable instances (snapshots) of the workflow

    Relationships
    -------------
    - 1 Workflow to many WorkflowDefinitions
    """

    __table_args__ = (
        UniqueConstraint("alias", "owner_id", name="uq_workflow_alias_owner_id"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4, nullable=False, unique=True, index=True
    )
    title: str
    description: str
    status: str = "offline"  # "online" or "offline"
    version: int | None = None
    entrypoint: str | None = Field(
        default=None,
        description="ID of the node directly connected to the trigger.",
    )
    static_inputs: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Static inputs for the workflow",
    )
    expects: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Input schema for the workflow",
    )
    returns: Any | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Workflow return values",
    )
    object: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB), description="React flow graph object"
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Workflow configuration",
    )
    alias: str | None = Field(
        default=None, description="Alias for the workflow", index=True
    )
    error_handler: str | None = Field(
        default=None,
        description="Workflow alias or ID for the workflow to run when this fails.",
    )
    icon_url: str | None = None
    # Owner
    owner_id: OwnerID = Field(
        sa_column=Column(UUID, ForeignKey("workspace.id", ondelete="CASCADE"))
    )
    owner: Workspace | None = Relationship(back_populates="workflows")
    # Folder
    folder_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(UUID, ForeignKey("workflow_folder.id", ondelete="CASCADE")),
    )
    folder: WorkflowFolder | None = Relationship(back_populates="workflows")
    # Relationships
    actions: list["Action"] | None = Relationship(
        back_populates="workflow",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    definitions: list["WorkflowDefinition"] | None = Relationship(
        back_populates="workflow",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    # Triggers
    webhook: "Webhook" = Relationship(
        back_populates="workflow",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    schedules: list["Schedule"] | None = Relationship(
        back_populates="workflow",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    tags: list["Tag"] = Relationship(
        back_populates="workflows",
        link_model=WorkflowTag,
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class Webhook(Resource, table=True):
    id: str = Field(
        default_factory=id_factory("wh"), nullable=False, unique=True, index=True
    )
    status: str = "offline"  # "online" or "offline"
    methods: list[str] = Field(
        default_factory=lambda: ["POST"], sa_column=Column(JSONB, nullable=False)
    )
    filters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    # Relationships
    workflow_id: uuid.UUID = Field(
        sa_column=Column(UUID, ForeignKey("workflow.id", ondelete="CASCADE"))
    )
    workflow: Workflow | None = Relationship(
        back_populates="webhook", sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS
    )

    @computed_field
    @property
    def secret(self) -> str:
        secret = os.getenv("TRACECAT__SIGNING_SECRET")
        if not secret:
            raise ValueError("TRACECAT__SIGNING_SECRET is not set")
        return hashlib.sha256(f"{self.id}{secret}".encode()).hexdigest()

    @computed_field
    @property
    def url(self) -> str:
        short_wf_id = WorkflowUUID.make_short(self.workflow_id)
        return f"{config.TRACECAT__PUBLIC_API_URL}/webhooks/{short_wf_id}/{self.secret}"

    @computed_field
    @property
    def normalized_methods(self) -> tuple[str, ...]:
        return tuple(m.lower() for m in self.methods)


class Schedule(Resource, table=True):
    id: str = Field(
        default_factory=id_factory("sch"), nullable=False, unique=True, index=True
    )
    status: str = "online"  # "online" or "offline"
    cron: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    every: timedelta = Field(..., description="ISO 8601 duration string")
    offset: timedelta | None = Field(None, description="ISO 8601 duration string")
    start_at: datetime | None = Field(None, description="ISO 8601 datetime string")
    end_at: datetime | None = Field(None, description="ISO 8601 datetime string")
    timeout: float | None = Field(
        None,
        description="The maximum number of seconds to wait for the workflow to complete",
    )
    # Relationships
    workflow_id: uuid.UUID = Field(
        sa_column=Column(UUID, ForeignKey("workflow.id", ondelete="CASCADE"))
    )
    workflow: Workflow | None = Relationship(
        back_populates="schedules",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class Action(Resource, table=True):
    """The workspace action state."""

    id: str = Field(
        default_factory=id_factory("act"), nullable=False, unique=True, index=True
    )
    type: str = Field(..., description="The action type, i.e. UDF key")
    title: str
    description: str
    status: str = "offline"  # "online" or "offline"
    inputs: str = Field(
        default="",
        description=(
            "YAML string containing input configuration. The default value is an empty "
            "string, which is `null` in YAML flow style."
        ),
    )
    control_flow: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    is_interactive: bool = Field(
        default=False,
        description="Whether the action is interactive",
        nullable=False,
    )
    interaction: dict[str, Any] | None = Field(
        default=None,
        description="The interaction configuration for the action",
        sa_column=Column(JSONB),
    )
    workflow_id: uuid.UUID = Field(
        sa_column=Column(UUID, ForeignKey("workflow.id", ondelete="CASCADE"))
    )
    workflow: Workflow | None = Relationship(
        back_populates="actions", sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS
    )

    @property
    def ref(self) -> str:
        """Slugified title of the action. Used for references."""
        return action.ref(self.title)


class RegistryRepository(Resource, table=True):
    """A repository of templates and actions."""

    id: UUID4 = Field(default_factory=uuid.uuid4, nullable=False, unique=True)
    origin: str = Field(
        ...,
        description=(
            "Tells you where the template action was created from."
            "Can use this to track the hierarchy of templates."
            "Depending on where the TA was created, this could be a few things:\n"
            "- Git url if created via a git sync\n"
            "- file://<path> if it's a custom action that was created from a file"
            "- None if it's a custom action that was created from scratch"
        ),
        unique=True,
        nullable=False,
    )
    last_synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
    )
    commit_sha: str | None = Field(
        default=None,
        description="The SHA of the last commit that was synced from the repository",
    )
    # Relationships
    actions: list["RegistryAction"] = Relationship(
        back_populates="repository",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )


class RegistryAction(Resource, table=True):
    """A registry action.


    A registry action can be a template action or a udf.
    A udf is a python user-defined function that can be used to create new actions.
    A template action is a reusable action that can be used to create new actions.
    Template actions loaded from tracecat base can be cloned but not edited.
    This is to ensure portability of templates across different users/systems.
    Custom template actions can be edited and cloned

    """

    __table_args__ = (
        UniqueConstraint("namespace", "name", name="uq_registry_action_namespace_name"),
    )

    id: UUID4 = Field(default_factory=uuid.uuid4, nullable=False, unique=True)
    name: str = Field(..., description="The name of the action")
    description: str = Field(..., description="The description of the action")
    namespace: str = Field(..., description="The namespace of the action")
    origin: str = Field(..., description="The origin of the action as a url")
    type: str = Field(..., description="The type of the action")
    default_title: str | None = Field(
        default=None, description="The default title of the action", nullable=True
    )
    display_group: str | None = Field(
        default=None, description="The presentation group of the action", nullable=True
    )
    doc_url: str | None = Field(
        default=None, description="Link to documentation", nullable=True
    )
    author: str | None = Field(
        default=None, description="Author of the action", nullable=True
    )
    deprecated: str | None = Field(
        default=None,
        description="Marks action as deprecated along with message",
        nullable=True,
    )
    secrets: list[dict[str, Any]] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="The secrets required by the action",
    )
    interface: dict[str, Any] = Field(
        ..., sa_column=Column(JSONB), description="The interface of the action"
    )
    implementation: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="The action's implementation",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="The action's options",
    )
    # Relationships
    repository_id: UUID4 = Field(
        sa_column=Column(UUID, ForeignKey("registryrepository.id", ondelete="CASCADE")),
    )
    repository: RegistryRepository = Relationship(back_populates="actions")

    @property
    def action(self):
        return f"{self.namespace}.{self.name}"


class OrganizationSetting(Resource, table=True):
    """An organization setting."""

    __tablename__: str = "organization_settings"

    id: UUID4 = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    key: str = Field(
        ...,
        description="A unique key that identifies the setting",
        index=True,
        unique=True,
    )
    value: bytes
    value_type: str = Field(
        ...,
        description="The data type of the setting value",
    )
    is_encrypted: bool = Field(
        default=False, description="Whether the setting is encrypted"
    )


class Table(Resource, table=True):
    """Metadata for lookup tables."""

    __tablename__: str = "tables"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    name: str = Field(..., index=True)
    # Add relationship to columns
    columns: list["TableColumn"] = Relationship(
        back_populates="table",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )


class TableColumn(SQLModel, TimestampMixin, table=True):
    """Column definitions for tables."""

    __tablename__: str = "table_columns"
    __table_args__ = (UniqueConstraint("table_id", "name"),)

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        nullable=False,
        unique=True,
        index=True,
    )
    table_id: uuid.UUID = Field(
        sa_column=Column(UUID, ForeignKey("tables.id", ondelete="CASCADE")),
    )
    name: str = Field(..., index=True)
    type: str = Field(..., description="SQL type like 'TEXT', 'INTEGER', etc.")
    nullable: bool = Field(default=True)
    default: Any | None = Field(default=None, sa_column=Column(JSONB))
    # Relationship back to the table
    table: Table = Relationship(
        back_populates="columns",
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS,
    )


class CaseFields(SQLModel, TimestampMixin, table=True):
    """A table of fields for a case."""

    __tablename__: str = "case_fields"
    model_config = ConfigDict(extra="allow")  # type: ignore

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        nullable=False,
        unique=True,
        index=True,
    )
    # Add required foreign key to Case
    case_id: uuid.UUID = Field(
        sa_column=Column(
            UUID,
            ForeignKey("cases.id", ondelete="CASCADE"),
            unique=True,  # Ensures one-to-one
            nullable=False,  # Ensures CaseFields must have a Case
        )
    )
    case: "Case" = Relationship(back_populates="fields")


class Case(Resource, table=True):
    """A case represents an incident or issue that needs to be tracked and resolved."""

    __tablename__: str = "cases"
    __table_args__ = (
        Index("idx_case_cursor_pagination", "owner_id", "created_at", "id"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    # Auto-incrementing case number for human readable IDs
    case_number: int | None = Field(
        default=None,  # Make optional in constructor, but DB will still require it
        sa_column=Column(
            "case_number",
            Integer,
            Identity(start=1, increment=1),
            unique=True,
            nullable=False,
            index=True,
        ),
        description="Auto-incrementing case number for human readable IDs like CASE-1234",
    )
    summary: str = Field(..., description="Case summary", max_length=255)
    description: str = Field(..., description="Case description", max_length=5000)
    priority: CasePriority = Field(
        ...,
        description="Case priority level",
    )
    severity: CaseSeverity = Field(
        ...,
        description="Case severity level",
    )
    status: CaseStatus = Field(
        default=CaseStatus.NEW,
        description="Current case status (open, closed, escalated)",
    )
    # Relationships
    fields: CaseFields | None = Relationship(
        back_populates="case",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            "uselist": False,  # Make this a one-to-one relationship
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    comments: list["CaseComment"] = Relationship(
        back_populates="case",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    events: list["CaseEvent"] = Relationship(
        back_populates="case",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    attachments: list["CaseAttachment"] = Relationship(
        back_populates="case",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )
    assignee_id: uuid.UUID | None = Field(
        default=None,
        description="The ID of the user who is assigned to the case.",
        sa_column=Column(UUID, ForeignKey("user.id", ondelete="SET NULL")),
    )
    assignee: User | None = Relationship(
        back_populates="assigned_cases",
        sa_relationship_kwargs={
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )


class CaseComment(Resource, table=True):
    """A comment on a case."""

    __tablename__: str = "case_comments"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    content: str = Field(..., max_length=5000)
    user_id: uuid.UUID | None = Field(
        default=None,
        description="The ID of the user who made the comment. If null, the comment is system generated.",
    )
    parent_id: uuid.UUID | None = Field(
        default=None,
        description="The ID of the parent comment. If null, the comment is a top-level comment.",
    )
    last_edited_at: datetime | None = Field(
        default=None,
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
    )
    # Relationships
    case_id: uuid.UUID = Field(
        sa_column=Column(
            UUID,
            ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    case: Case = Relationship(back_populates="comments")


class CaseEvent(Resource, table=True):
    """A activity record for a case.

    Uses a tagged union pattern where the 'type' field indicates the kind of activity,
    and the 'data' field contains variant-specific information for that activity type.
    """

    __tablename__: str = "case_event"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    type: CaseEventType = Field(..., description="The type of event")
    # Variant-specific data for this activity type
    data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Variant-specific data for this event type",
    )
    user_id: uuid.UUID | None = Field(
        default=None,
        description="The ID of the user who made the event. If null, the event is system generated.",
    )
    # Relationships
    case_id: uuid.UUID = Field(
        sa_column=Column(
            UUID,
            ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    case: Case = Relationship(back_populates="events")


class Interaction(Resource, table=True):
    """Database model for storing workflow interaction state.

    This table stores the state of interactions within workflows, allowing us
    to query them without relying on Temporal's event replay.
    """

    __tablename__: str = "interaction"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    wf_exec_id: str = Field(index=True, description="Workflow execution ID")
    action_ref: str = Field(description="Reference to the action step in the DSL")
    action_type: str = Field(description="Type of action")
    type: InteractionType = Field(description="Type of interaction")
    status: InteractionStatus = Field(
        default=InteractionStatus.PENDING,
        index=True,
        description="Status of the interaction",
    )
    request_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Data sent for the interaction",
    )
    response_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description="Data received from the interaction",
    )
    expires_at: datetime | None = Field(
        default=None,
        nullable=True,
        description="Timestamp for when the interaction expires",
    )
    actor: str | None = Field(
        default=None,
        nullable=True,
        description="ID of the user/actor who responded",
    )


class File(Resource, table=True):
    """A file entity with content-addressable storage using SHA256."""

    __tablename__: str = "file"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    sha256: str = Field(
        ...,
        max_length=64,
        index=True,
        description="SHA256 hash for content-addressable storage and deduplication",
    )
    name: str = Field(
        ...,
        max_length=config.TRACECAT__MAX_ATTACHMENT_FILENAME_LENGTH,
        description="Original filename when uploaded",
    )
    content_type: str = Field(
        ...,
        max_length=100,
        description="MIME type of the file",
    )
    size: int = Field(
        ...,
        gt=0,
        le=config.TRACECAT__MAX_ATTACHMENT_SIZE_BYTES,
        description="File size in bytes",
    )
    creator_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(UUID, nullable=True),
        description="ID of the user who uploaded the file. If None, assume is system created.",
    )
    deleted_at: datetime | None = Field(
        default=None,
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        description="Timestamp for soft deletion",
    )
    attachments: list["CaseAttachment"] = Relationship(
        back_populates="file",
        sa_relationship_kwargs={
            "cascade": "all, delete",
            **DEFAULT_SA_RELATIONSHIP_KWARGS,
        },
    )

    @computed_field
    @property
    def is_deleted(self) -> bool:
        """Check if file is soft deleted."""
        return self.deleted_at is not None


class CaseAttachment(SQLModel, TimestampMixin, table=True):
    """Link table between cases and files."""

    __tablename__: str = "case_attachment"
    __table_args__ = (
        UniqueConstraint("case_id", "file_id", name="uq_case_attachment_case_file"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        nullable=False,
        unique=True,
        index=True,
    )
    case_id: uuid.UUID = Field(
        sa_column=Column(
            UUID,
            ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    file_id: uuid.UUID = Field(
        sa_column=Column(
            UUID,
            ForeignKey("file.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    # Relationships
    case: Case = Relationship(back_populates="attachments")
    file: File = Relationship(back_populates="attachments")

    @computed_field
    @property
    def storage_path(self) -> str:
        """Generate the storage path for case attachments."""
        return f"attachments/{self.file.sha256}"


class OAuthIntegration(SQLModel, TimestampMixin, table=True):
    """Store user integrations with external services."""

    __tablename__: str = "oauth_integration"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "provider_id",
            "user_id",
            "grant_type",
            name="uq_oauth_integration_owner_provider_user_flow",
        ),
    )

    id: UUID4 = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        nullable=False,
        unique=True,
        index=True,
    )
    # Owner (workspace)
    owner_id: OwnerID = Field(
        sa_column=Column(UUID, ForeignKey("workspace.id", ondelete="CASCADE"))
    )
    user_id: UUID4 | None = Field(
        default=None,
        sa_column=Column(
            "user_id",
            ForeignKey("user.id", ondelete="CASCADE"),
        ),
        description="The user this integration belongs to",
    )
    provider_id: str = Field(
        ...,
        description="Integration provider identifier (e.g., 'microsoft-teams', 'google-gmail')",
        index=True,
    )
    encrypted_access_token: bytes = Field(
        ...,
        description="Encrypted OAuth access token for the integration",
    )
    encrypted_refresh_token: bytes | None = Field(
        default=None,
        description="Encrypted OAuth refresh token for the integration",
    )
    encrypted_client_id: bytes | None = Field(
        default=None,
        description="Encrypted OAuth client ID for the integration",
    )
    encrypted_client_secret: bytes | None = Field(
        default=None,
        description="Encrypted OAuth client secret for the integration",
    )
    use_workspace_credentials: bool = Field(
        default=False,
        description="Whether to use workspace-configured credentials instead of environment variables",
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type (typically Bearer)",
    )
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description="When the access token expires",
    )
    scope: str | None = Field(
        default=None,
        description="OAuth scopes granted for this integration",
    )
    requested_scopes: str | None = Field(
        default=None,
        description="OAuth scopes requested by user for this integration",
    )
    grant_type: OAuthGrantType = Field(
        default=OAuthGrantType.AUTHORIZATION_CODE,
        description="OAuth grant type used for this integration",
    )
    provider_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Provider-specific configuration for the integration",
    )

    # Relationships
    user: User | None = Relationship(
        sa_relationship_kwargs=DEFAULT_SA_RELATIONSHIP_KWARGS
    )
    owner: Workspace = Relationship(back_populates="integrations")

    @property
    def is_expired(self) -> bool:
        """Check if the access token is expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        """Check if the token needs to be refreshed soon (within 5 minutes)."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= (self.expires_at - timedelta(minutes=5))

    @property
    def status(self) -> IntegrationStatus:
        """Get the status of the integration."""
        # Is configured: Has client ID and Secret
        is_configured = (
            self.encrypted_client_id is not None
            and self.encrypted_client_secret is not None
        )

        # Is connected: Successfully authenticated
        is_connected = len(self.encrypted_access_token) > 0

        # Return status based on conditions
        if is_connected:
            return IntegrationStatus.CONNECTED
        elif is_configured:
            return IntegrationStatus.CONFIGURED
        else:
            return IntegrationStatus.NOT_CONFIGURED


class OAuthStateDB(SQLModel, TimestampMixin, table=True):
    """Store OAuth state parameters for CSRF protection during OAuth flows."""

    __tablename__: str = "oauth_state"
    __table_args__ = (Index("ix_oauth_state_expires_at", "expires_at"),)

    state: UUID4 = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        nullable=False,
        description="Unique state identifier for OAuth flow",
    )
    workspace_id: UUID4 = Field(
        sa_column=Column(
            UUID, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
        ),
        description="Workspace ID associated with this OAuth flow",
    )
    user_id: UUID4 = Field(
        sa_column=Column(
            UUID, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
        ),
        description="User ID initiating the OAuth flow",
    )
    provider_id: str = Field(
        nullable=False,
        description="Provider ID for this OAuth flow",
    )
    expires_at: datetime = Field(
        sa_type=TIMESTAMP(timezone=True),  # type: ignore
        nullable=False,
        description="When this state expires",
    )

    # Relationships
    workspace: Workspace = Relationship()
    user: User = Relationship()
