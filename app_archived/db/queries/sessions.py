from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, AgentVersion, Environment, ManagedSession
from app.db.queries import session_funding_bindings as funding_q
from app.ids import new_id
from app.runtime.model_credentials import (
    MODEL_CREDENTIAL_BINDING_KEY,
    ModelCredentialRequiredError,
)
from app.runtime.funding import (
    SessionFundingUnavailableError,
    resolve_session_funding_binding,
)
from app.runtime.providers import runtime_model_id, runtime_provider_id
from app.organization import resolve_organization_id


async def create_session(
    db: AsyncSession,
    *,
    agent: Agent,
    agent_version: int,
    environment: Environment,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    resources: list[dict[str, Any]] | None = None,
    vault_ids: list[str] | None = None,
    funding_type: str | None = None,
    agent_config: dict[str, Any] | None = None,
    organization_id: str | None = None,
) -> ManagedSession:
    scoped_organization_id = resolve_organization_id(
        agent.organization_id if organization_id is None else organization_id
    )
    status_details: dict[str, Any] = {
        "resources": resources or [],
        "vault_ids": vault_ids or [],
    }
    if agent_config is not None:
        status_details["agent"] = agent_config
    version_result = await db.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id,
            AgentVersion.version == agent_version,
            AgentVersion.organization_id == scoped_organization_id,
        )
    )
    version_record = version_result.scalar_one_or_none()
    if version_record is None:
        raise RuntimeError(f"Agent version {agent.id}:{agent_version} not found")
    effective_model = (
        agent_config.get("model")
        if isinstance(agent_config, dict) and isinstance(agent_config.get("model"), dict)
        else version_record.model
    )
    await _require_single_multiagent_provider(
        db,
        version_record=version_record,
        effective_model=effective_model,
        organization_id=scoped_organization_id,
    )
    model_credential_binding = await resolve_session_funding_binding(
        db,
        model=effective_model,
        runtime=version_record.runtime,
        vault_ids=vault_ids,
        funding_type=funding_type,
        organization_id=scoped_organization_id,
    )
    status_details[MODEL_CREDENTIAL_BINDING_KEY] = model_credential_binding
    session = ManagedSession(
        id=new_id("sess"),
        runtime_thread_id=new_id("thread"),
        organization_id=scoped_organization_id,
        agent_id=agent.id,
        agent_version=agent_version,
        environment_id=environment.id,
        title=title,
        status="idle",
        metadata_=metadata or {},
        status_details=status_details,
        last_event_seq=0,
    )
    db.add(session)
    await db.flush()
    try:
        await funding_q.create_session_funding_binding(
            db,
            session_id=session.id,
            source=str(model_credential_binding.get("source") or ""),
            provider=runtime_provider_id(
                effective_model,
                runtime=version_record.runtime,
            ),
            model_id=runtime_model_id(
                effective_model,
                runtime=version_record.runtime,
            ),
            vault_id=model_credential_binding.get("vault_id"),
            model_credential_id=model_credential_binding.get("credential_id"),
            organization_billing_account_id=model_credential_binding.get(
                "organization_billing_account_id"
            ),
            organization_provider_key_binding_id=model_credential_binding.get(
                "organization_provider_key_binding_id"
            ),
            organization_id=scoped_organization_id,
        )
    except funding_q.SessionFundingBindingResourceError as exc:
        raise SessionFundingUnavailableError(
            "The selected Session funding source became unavailable"
        ) from exc
    return session


async def _require_single_multiagent_provider(
    db: AsyncSession,
    *,
    version_record: AgentVersion,
    effective_model: dict[str, Any],
    organization_id: str,
) -> None:
    """Keep the MVP's one immutable model Credential valid for every subagent."""

    roster = version_record.multiagent if isinstance(version_record.multiagent, dict) else {}
    primary_provider = runtime_provider_id(effective_model, runtime=version_record.runtime)
    for entry in roster.get("agents") or []:
        if not isinstance(entry, dict):
            continue
        agent_id = str(entry.get("id") or "")
        try:
            agent_version = int(entry.get("version"))
        except (TypeError, ValueError):
            continue
        result = await db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version == agent_version,
                AgentVersion.organization_id == organization_id,
            )
        )
        referenced = result.scalar_one_or_none()
        if referenced is None:
            continue
        if runtime_provider_id(referenced.model, runtime=referenced.runtime) != primary_provider:
            raise ModelCredentialRequiredError(
                "Multiagent Sessions currently require the coordinator and all subagents "
                "to use the same model provider"
            )


async def get_session(
    db: AsyncSession,
    session_id: str,
    *,
    organization_id: str | None = None,
    for_update: bool = False,
) -> ManagedSession | None:
    stmt = select(ManagedSession).where(ManagedSession.id == session_id)
    if organization_id is not None:
        stmt = stmt.where(ManagedSession.organization_id == organization_id)
    else:
        stmt = stmt.where(ManagedSession.organization_id == resolve_organization_id())
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_sessions(
    db: AsyncSession,
    *,
    limit: int = 50,
    include_archived: bool = False,
    organization_id: str | None = None,
) -> list[ManagedSession]:
    stmt = (
        select(ManagedSession)
        .where(
            ManagedSession.deleted_at.is_(None),
            ManagedSession.organization_id == resolve_organization_id(organization_id),
        )
        .order_by(ManagedSession.created_at.desc())
        .limit(limit)
    )
    if not include_archived:
        stmt = stmt.where(ManagedSession.archived_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_session(
    db: AsyncSession,
    session: ManagedSession,
    *,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: str | None = None,
    status_details: dict[str, Any] | None = None,
    stop_reason: dict[str, Any] | None = None,
    run_state: dict[str, Any] | None = None,
    sandbox_state: dict[str, Any] | None = None,
) -> ManagedSession:
    if title is not None:
        session.title = title
    if metadata is not None:
        session.metadata_ = metadata
    if status is not None:
        session.status = status
    if status_details is not None:
        session.status_details = status_details
    if stop_reason is not None:
        session.stop_reason = stop_reason
    if run_state is not None:
        session.run_state = run_state
    if sandbox_state is not None:
        session.sandbox_state = sandbox_state
    await db.flush()
    return session


async def archive_session(db: AsyncSession, session: ManagedSession) -> ManagedSession:
    session.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return session


async def delete_session(db: AsyncSession, session: ManagedSession) -> None:
    session.deleted_at = datetime.now(timezone.utc)
    session.status = "terminated"
    await db.flush()
