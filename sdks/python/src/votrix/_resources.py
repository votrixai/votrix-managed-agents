from __future__ import annotations

import mimetypes
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._client import AsyncVotrix, BinaryResponse
from ._constants import (
    API_KEYS_PATH,
    AGENTS_PATH,
    ENVIRONMENTS_PATH,
    FILES_PATH,
    MODEL_PROVIDERS_PATH,
    SESSIONS_PATH,
    SKILLS_PATH,
    VAULTS_PATH,
)
from ._models import (
    Agent,
    ApiKey,
    ApiKeyCreated,
    ApiKeyScope,
    DeletedObject,
    Environment,
    FileObject,
    ModelCredential,
    ModelProvider,
    SendEventsResult,
    Session,
    SessionEvent,
    SessionResource,
    Skill,
    SkillVersion,
    Vault,
    VotrixModel,
)
from ._pagination import AsyncPaginator
from ._sse import AsyncEventStream


class _NotGiven:
    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


def _path_id(value: str) -> str:
    return quote(str(value), safe="")


def _body(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not NOT_GIVEN}


def _paginator(
    client: AsyncVotrix,
    path: str,
    model: type[VotrixModel],
    params: Mapping[str, Any],
    *,
    cursor_param: str = "page",
) -> AsyncPaginator:
    async def load(page_params: Mapping[str, Any]):
        return await client.request_list("GET", path, model=model, params=page_params)

    return AsyncPaginator(load, params=params, cursor_param=cursor_param)


class ApiKeysResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def create(
        self,
        *,
        name: str,
        scopes: Sequence[ApiKeyScope] | _NotGiven = NOT_GIVEN,
        expires_at: datetime | str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> ApiKeyCreated:
        return await self._client.request(
            "POST",
            API_KEYS_PATH,
            model=ApiKeyCreated,
            json=_body(
                name=name,
                scopes=list(scopes) if not isinstance(scopes, _NotGiven) else NOT_GIVEN,
                expires_at=expires_at,
                metadata=metadata,
            ),
        )

    def list(
        self,
        *,
        limit: int = 50,
        page: str | None = None,
        include_revoked: bool = True,
    ) -> AsyncPaginator[ApiKey]:
        return _paginator(
            self._client,
            API_KEYS_PATH,
            ApiKey,
            {
                "limit": limit,
                "page": page,
                "include_revoked": include_revoked,
            },
        )

    async def retrieve(self, key_id: str) -> ApiKey:
        return await self._client.request(
            "GET",
            f"{API_KEYS_PATH}/{_path_id(key_id)}",
            model=ApiKey,
        )

    async def revoke(
        self,
        key_id: str,
        *,
        reason: str | None | _NotGiven = NOT_GIVEN,
    ) -> ApiKey:
        body = _body(reason=reason)
        return await self._client.request(
            "POST",
            f"{API_KEYS_PATH}/{_path_id(key_id)}/revoke",
            model=ApiKey,
            json=body or None,
        )

    async def rotate(
        self,
        key_id: str,
        *,
        expires_at: datetime | str | None | _NotGiven = NOT_GIVEN,
        reason: str | None | _NotGiven = NOT_GIVEN,
    ) -> ApiKeyCreated:
        body = _body(expires_at=expires_at, reason=reason)
        return await self._client.request(
            "POST",
            f"{API_KEYS_PATH}/{_path_id(key_id)}/rotate",
            model=ApiKeyCreated,
            json=body or None,
        )


class AgentsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client
        self.versions = AgentVersionsResource(client)

    async def create(
        self,
        *,
        name: str,
        model: str | Mapping[str, Any],
        system: str | None | _NotGiven = NOT_GIVEN,
        description: str | None | _NotGiven = NOT_GIVEN,
        tools: Sequence[Mapping[str, Any]] | _NotGiven = NOT_GIVEN,
        mcp_servers: Sequence[Mapping[str, Any]] | _NotGiven = NOT_GIVEN,
        skills: Sequence[Mapping[str, Any]] | _NotGiven = NOT_GIVEN,
        multiagent: Mapping[str, Any] | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
        runtime: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> Agent:
        return await self._client.request(
            "POST",
            AGENTS_PATH,
            model=Agent,
            json=_body(
                name=name,
                model=model,
                system=system,
                description=description,
                tools=tools,
                mcp_servers=mcp_servers,
                skills=skills,
                multiagent=multiagent,
                metadata=metadata,
                runtime=runtime,
            ),
        )

    async def retrieve(self, agent_id: str, *, version: int | None = None) -> Agent:
        return await self._client.request(
            "GET", f"{AGENTS_PATH}/{_path_id(agent_id)}", model=Agent, params={"version": version}
        )

    async def update(self, agent_id: str, *, version: int, **changes: Any) -> Agent:
        return await self._client.request(
            "POST", f"{AGENTS_PATH}/{_path_id(agent_id)}", model=Agent, json={"version": version, **changes}
        )

    def list(
        self,
        *,
        limit: int = 50,
        page: str | None = None,
        include_archived: bool = False,
        created_at_gte: str | None = None,
        created_at_lte: str | None = None,
    ) -> AsyncPaginator[Agent]:
        return _paginator(
            self._client,
            AGENTS_PATH,
            Agent,
            {
                "limit": limit,
                "page": page,
                "include_archived": include_archived,
                "created_at[gte]": created_at_gte,
                "created_at[lte]": created_at_lte,
            },
        )

    async def archive(self, agent_id: str) -> Agent:
        return await self._client.request("POST", f"{AGENTS_PATH}/{_path_id(agent_id)}/archive", model=Agent)


class AgentVersionsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    def list(self, agent_id: str, *, limit: int = 50, page: str | None = None) -> AsyncPaginator[Agent]:
        return _paginator(
            self._client,
            f"{AGENTS_PATH}/{_path_id(agent_id)}/versions",
            Agent,
            {"limit": limit, "page": page},
        )


class EnvironmentsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def create(
        self,
        *,
        name: str,
        config: Mapping[str, Any] | None | _NotGiven = NOT_GIVEN,
        description: str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
        scope: str | None | _NotGiven = NOT_GIVEN,
    ) -> Environment:
        return await self._client.request(
            "POST",
            ENVIRONMENTS_PATH,
            model=Environment,
            json=_body(name=name, config=config, description=description, metadata=metadata, scope=scope),
        )

    async def retrieve(self, environment_id: str) -> Environment:
        return await self._client.request(
            "GET", f"{ENVIRONMENTS_PATH}/{_path_id(environment_id)}", model=Environment
        )

    async def update(self, environment_id: str, **changes: Any) -> Environment:
        return await self._client.request(
            "POST", f"{ENVIRONMENTS_PATH}/{_path_id(environment_id)}", model=Environment, json=changes
        )

    def list(
        self, *, limit: int = 50, page: str | None = None, include_archived: bool = False
    ) -> AsyncPaginator[Environment]:
        return _paginator(
            self._client,
            ENVIRONMENTS_PATH,
            Environment,
            {"limit": limit, "page": page, "include_archived": include_archived},
        )

    async def archive(self, environment_id: str) -> Environment:
        return await self._client.request(
            "POST", f"{ENVIRONMENTS_PATH}/{_path_id(environment_id)}/archive", model=Environment
        )

    async def delete(self, environment_id: str) -> DeletedObject:
        return await self._client.request(
            "DELETE", f"{ENVIRONMENTS_PATH}/{_path_id(environment_id)}", model=DeletedObject
        )


class SessionsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client
        self.events = SessionEventsResource(client)
        self.resources = SessionResourcesResource(client)

    async def create(
        self,
        *,
        agent: str | Mapping[str, Any],
        environment_id: str,
        title: str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
        resources: Sequence[Mapping[str, Any]] | _NotGiven = NOT_GIVEN,
        vault_ids: Sequence[str] | _NotGiven = NOT_GIVEN,
        idempotency_key: str | None = None,
    ) -> Session:
        return await self._client.request(
            "POST",
            SESSIONS_PATH,
            model=Session,
            json=_body(
                agent=agent,
                environment_id=environment_id,
                title=title,
                metadata=metadata,
                resources=resources,
                vault_ids=vault_ids,
            ),
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        )

    async def retrieve(self, session_id: str) -> Session:
        return await self._client.request("GET", f"{SESSIONS_PATH}/{_path_id(session_id)}", model=Session)

    async def update(self, session_id: str, **changes: Any) -> Session:
        return await self._client.request(
            "POST", f"{SESSIONS_PATH}/{_path_id(session_id)}", model=Session, json=changes
        )

    def list(
        self,
        *,
        limit: int = 50,
        page: str | None = None,
        include_archived: bool = False,
        order: str = "desc",
        agent_id: str | None = None,
        agent_version: int | None = None,
        deployment_id: str | None = None,
        statuses: Sequence[str] | None = None,
        created_at_gt: str | None = None,
        created_at_gte: str | None = None,
        created_at_lt: str | None = None,
        created_at_lte: str | None = None,
    ) -> AsyncPaginator[Session]:
        return _paginator(
            self._client,
            SESSIONS_PATH,
            Session,
            {
                "limit": limit,
                "page": page,
                "include_archived": include_archived,
                "order": order,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "deployment_id": deployment_id,
                "statuses": list(statuses) if statuses is not None else None,
                "created_at[gt]": created_at_gt,
                "created_at[gte]": created_at_gte,
                "created_at[lt]": created_at_lt,
                "created_at[lte]": created_at_lte,
            },
        )

    async def archive(self, session_id: str) -> Session:
        return await self._client.request(
            "POST", f"{SESSIONS_PATH}/{_path_id(session_id)}/archive", model=Session
        )

    async def cancel(self, session_id: str) -> Session:
        return await self._client.request(
            "POST", f"{SESSIONS_PATH}/{_path_id(session_id)}/cancel", model=Session
        )

    async def resume(self, session_id: str) -> Session:
        return await self._client.request(
            "POST", f"{SESSIONS_PATH}/{_path_id(session_id)}/resume", model=Session
        )

    async def delete(self, session_id: str) -> DeletedObject:
        return await self._client.request(
            "DELETE", f"{SESSIONS_PATH}/{_path_id(session_id)}", model=DeletedObject
        )


class SessionEventsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def send(
        self,
        session_id: str,
        *,
        events: Sequence[Mapping[str, Any]],
        idempotency_key: str | None = None,
    ) -> SendEventsResult:
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return await self._client.request(
            "POST",
            f"{SESSIONS_PATH}/{_path_id(session_id)}/events",
            model=SendEventsResult,
            json={"events": list(events)},
            headers=headers,
        )

    def list(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        page: str | None = None,
        order: str = "asc",
        types: Sequence[str] | None = None,
        created_at_gt: str | None = None,
        created_at_gte: str | None = None,
        created_at_lt: str | None = None,
        created_at_lte: str | None = None,
    ) -> AsyncPaginator[SessionEvent]:
        return _paginator(
            self._client,
            f"{SESSIONS_PATH}/{_path_id(session_id)}/events",
            SessionEvent,
            {
                "after_seq": after_seq,
                "limit": limit,
                "page": page,
                "order": order,
                "types": list(types) if types is not None else None,
                "created_at[gt]": created_at_gt,
                "created_at[gte]": created_at_gte,
                "created_at[lt]": created_at_lt,
                "created_at[lte]": created_at_lte,
            },
        )

    async def stream(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        event_deltas: Sequence[str] | None = None,
        last_event_id: str | None = None,
        max_reconnects: int | None = None,
    ) -> AsyncEventStream:
        headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else None
        return AsyncEventStream(
            self._client,
            method="GET",
            path=f"{SESSIONS_PATH}/{_path_id(session_id)}/events/stream",
            params={
                "after_seq": after_seq,
                "event_deltas": list(event_deltas) if event_deltas is not None else None,
            },
            headers=headers,
            max_reconnects=max_reconnects,
        )


class SessionResourcesResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def add(
        self,
        session_id: str,
        *,
        file_id: str,
        mount_path: str | _NotGiven = NOT_GIVEN,
        type: str = "file",
    ) -> SessionResource:
        if type != "file":
            raise ValueError("Session resources.add currently supports only type='file'")
        return await self._client.request(
            "POST",
            f"{SESSIONS_PATH}/{_path_id(session_id)}/resources",
            model=SessionResource,
            json=_body(type="file", file_id=file_id, mount_path=mount_path),
        )

    async def add_file(
        self, session_id: str, *, file_id: str, mount_path: str | _NotGiven = NOT_GIVEN
    ) -> SessionResource:
        return await self.add(session_id, file_id=file_id, mount_path=mount_path)

    def list(
        self, session_id: str, *, limit: int = 50, page: str | None = None
    ) -> AsyncPaginator[SessionResource]:
        return _paginator(
            self._client,
            f"{SESSIONS_PATH}/{_path_id(session_id)}/resources",
            SessionResource,
            {"limit": limit, "page": page},
        )

    async def retrieve(self, resource_id: str, *, session_id: str) -> SessionResource:
        return await self._client.request(
            "GET",
            f"{SESSIONS_PATH}/{_path_id(session_id)}/resources/{_path_id(resource_id)}",
            model=SessionResource,
        )

    async def update(
        self, resource_id: str, *, session_id: str, authorization_token: str
    ) -> SessionResource:
        return await self._client.request(
            "POST",
            f"{SESSIONS_PATH}/{_path_id(session_id)}/resources/{_path_id(resource_id)}",
            model=SessionResource,
            json={"authorization_token": authorization_token},
        )

    async def delete(self, resource_id: str, *, session_id: str) -> DeletedObject:
        return await self._client.request(
            "DELETE",
            f"{SESSIONS_PATH}/{_path_id(session_id)}/resources/{_path_id(resource_id)}",
            model=DeletedObject,
        )


class FilesResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def upload(
        self,
        *,
        file: bytes | bytearray | os.PathLike[str] | tuple[str, bytes] | tuple[str, bytes, str] | Any,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> FileObject:
        upload = _upload_tuple(file, filename=filename, mime_type=mime_type)
        return await self._client.request(
            "POST", FILES_PATH, model=FileObject, files={"file": upload}
        )

    async def retrieve_metadata(self, file_id: str) -> FileObject:
        return await self._client.request("GET", f"{FILES_PATH}/{_path_id(file_id)}", model=FileObject)

    def list(
        self,
        *,
        limit: int = 50,
        after_id: str | None = None,
        before_id: str | None = None,
        scope_id: str | None = None,
    ) -> AsyncPaginator[FileObject]:
        return _paginator(
            self._client,
            FILES_PATH,
            FileObject,
            {
                "limit": limit,
                "after_id": after_id,
                "before_id": before_id,
                "scope_id": scope_id,
            },
            cursor_param="before_id" if before_id is not None else "after_id",
        )

    async def download(self, file_id: str, *, stream: bool = False) -> BinaryResponse:
        return await self._client.request_binary(
            "GET",
            f"{FILES_PATH}/{_path_id(file_id)}/content",
            stream=stream,
        )

    async def delete(self, file_id: str) -> DeletedObject:
        return await self._client.request("DELETE", f"{FILES_PATH}/{_path_id(file_id)}", model=DeletedObject)


class SkillsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client
        self.versions = SkillVersionsResource(client)

    async def create(
        self,
        *,
        display_title: str | None = None,
        files: Sequence[Mapping[str, Any] | tuple[str, bytes] | tuple[str, bytes, str]] | None = None,
        archive: bytes | bytearray | os.PathLike[str] | None = None,
        **metadata: Any,
    ) -> Skill:
        if archive is not None and files:
            raise ValueError("Pass either archive or files, not both")
        if archive is None and not files:
            raise ValueError("A skill requires archive or files")
        if archive is not None:
            return await self._client.request(
                "POST",
                SKILLS_PATH,
                model=Skill,
                data={"display_title": display_title} if display_title is not None else None,
                files=[("files", _upload_tuple(archive, filename="skill.zip", mime_type="application/zip"))],
            )
        if files and _skill_file_mode(files) == "multipart":
            multipart = [("files", _upload_tuple(item, filename=None, mime_type=None)) for item in files]
            return await self._client.request(
                "POST",
                SKILLS_PATH,
                model=Skill,
                data={"display_title": display_title} if display_title is not None else None,
                files=multipart,
            )
        return await self._client.request(
            "POST",
            SKILLS_PATH,
            model=Skill,
            json={"display_title": display_title, "files": list(files or []), **metadata},
        )

    async def retrieve(self, skill_id: str) -> Skill:
        return await self._client.request("GET", f"{SKILLS_PATH}/{_path_id(skill_id)}", model=Skill)

    def list(
        self, *, limit: int = 50, page: str | None = None, source: str | None = None
    ) -> AsyncPaginator[Skill]:
        return _paginator(
            self._client,
            SKILLS_PATH,
            Skill,
            {"limit": limit, "page": page, "source": source},
        )

    async def delete(self, skill_id: str) -> DeletedObject:
        return await self._client.request("DELETE", f"{SKILLS_PATH}/{_path_id(skill_id)}", model=DeletedObject)


class SkillVersionsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def create(
        self,
        skill_id: str,
        *,
        files: Sequence[Mapping[str, Any] | tuple[str, bytes] | tuple[str, bytes, str]] | None = None,
        archive: bytes | bytearray | os.PathLike[str] | None = None,
        **metadata: Any,
    ) -> SkillVersion:
        path = f"{SKILLS_PATH}/{_path_id(skill_id)}/versions"
        if archive is not None and files:
            raise ValueError("Pass either archive or files, not both")
        if archive is None and not files:
            raise ValueError("A skill version requires archive or files")
        if archive is not None:
            return await self._client.request(
                "POST",
                path,
                model=SkillVersion,
                files=[("files", _upload_tuple(archive, filename="skill.zip", mime_type="application/zip"))],
            )
        if files and _skill_file_mode(files) == "multipart":
            multipart = [("files", _upload_tuple(item, filename=None, mime_type=None)) for item in files]
            return await self._client.request("POST", path, model=SkillVersion, files=multipart)
        return await self._client.request(
            "POST", path, model=SkillVersion, json={"files": list(files or []), **metadata}
        )

    async def retrieve(self, version: int, *, skill_id: str) -> SkillVersion:
        return await self._client.request(
            "GET",
            f"{SKILLS_PATH}/{_path_id(skill_id)}/versions/{_path_id(str(version))}",
            model=SkillVersion,
        )

    def list(
        self, skill_id: str, *, limit: int = 50, page: str | None = None
    ) -> AsyncPaginator[SkillVersion]:
        return _paginator(
            self._client,
            f"{SKILLS_PATH}/{_path_id(skill_id)}/versions",
            SkillVersion,
            {"limit": limit, "page": page},
        )

    async def download(
        self,
        version: int,
        *,
        skill_id: str,
        stream: bool = False,
    ) -> BinaryResponse:
        return await self._client.request_binary(
            "GET",
            f"{SKILLS_PATH}/{_path_id(skill_id)}/versions/{_path_id(str(version))}/content",
            stream=stream,
        )

    async def delete(self, version: int, *, skill_id: str) -> DeletedObject:
        return await self._client.request(
            "DELETE",
            f"{SKILLS_PATH}/{_path_id(skill_id)}/versions/{_path_id(str(version))}",
            model=DeletedObject,
        )


class VaultsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client
        self.model_credentials = ModelCredentialsResource(client)

    async def create(
        self, *, display_name: str, metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN
    ) -> Vault:
        return await self._client.request(
            "POST", VAULTS_PATH, model=Vault, json=_body(display_name=display_name, metadata=metadata)
        )

    async def retrieve(self, vault_id: str) -> Vault:
        return await self._client.request("GET", f"{VAULTS_PATH}/{_path_id(vault_id)}", model=Vault)

    async def update(self, vault_id: str, **changes: Any) -> Vault:
        return await self._client.request(
            "POST", f"{VAULTS_PATH}/{_path_id(vault_id)}", model=Vault, json=changes
        )

    def list(
        self,
        *,
        limit: int = 50,
        page: str | None = None,
        include_archived: bool = False,
    ) -> AsyncPaginator[Vault]:
        return _paginator(
            self._client,
            VAULTS_PATH,
            Vault,
            {"limit": limit, "page": page, "include_archived": include_archived},
        )

    async def archive(self, vault_id: str) -> Vault:
        return await self._client.request(
            "POST", f"{VAULTS_PATH}/{_path_id(vault_id)}/archive", model=Vault
        )

    async def delete(self, vault_id: str) -> DeletedObject:
        return await self._client.request("DELETE", f"{VAULTS_PATH}/{_path_id(vault_id)}", model=DeletedObject)


class ModelCredentialsResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    async def create(
        self,
        vault_id: str,
        *,
        provider: str,
        api_key: str,
        display_name: str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> ModelCredential:
        return await self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials",
            model=ModelCredential,
            json=_body(
                provider=provider,
                api_key=api_key,
                display_name=display_name,
                metadata=metadata,
            ),
        )

    def list(
        self,
        vault_id: str,
        *,
        limit: int = 50,
        page: str | None = None,
        include_archived: bool = False,
    ) -> AsyncPaginator[ModelCredential]:
        return _paginator(
            self._client,
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials",
            ModelCredential,
            {
                "limit": limit,
                "page": page,
                "include_archived": include_archived,
            },
        )

    async def retrieve(
        self,
        credential_id: str,
        *,
        vault_id: str,
    ) -> ModelCredential:
        return await self._client.request(
            "GET",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=ModelCredential,
        )

    async def rotate(self, vault_id: str, credential_id: str, *, api_key: str) -> ModelCredential:
        return await self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=ModelCredential,
            json={"api_key": api_key},
        )

    async def archive(
        self,
        credential_id: str,
        *,
        vault_id: str,
    ) -> ModelCredential:
        return await self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}/archive",
            model=ModelCredential,
        )

    async def delete(
        self,
        credential_id: str,
        *,
        vault_id: str,
    ) -> DeletedObject:
        return await self._client.request(
            "DELETE",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=DeletedObject,
        )


class ModelProvidersResource:
    def __init__(self, client: AsyncVotrix) -> None:
        self._client = client

    def list(self) -> AsyncPaginator[ModelProvider]:
        return _paginator(
            self._client,
            MODEL_PROVIDERS_PATH,
            ModelProvider,
            {},
        )

    async def retrieve(self, provider_id: str) -> ModelProvider:
        return await self._client.request(
            "GET", f"{MODEL_PROVIDERS_PATH}/{_path_id(provider_id)}", model=ModelProvider
        )


def _upload_tuple(
    value: bytes | bytearray | os.PathLike[str] | tuple[str, bytes] | tuple[str, bytes, str] | Any,
    *,
    filename: str | None,
    mime_type: str | None,
) -> tuple[str, Any, str]:
    if isinstance(value, tuple):
        if len(value) == 3:
            return value
        if len(value) == 2:
            tuple_name, content = value
            return tuple_name, content, mime_type or mimetypes.guess_type(tuple_name)[0] or "application/octet-stream"
        raise ValueError("file tuple must be (filename, content) or (filename, content, mime_type)")
    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        return (
            filename or path.name,
            path.read_bytes(),
            mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
    if isinstance(value, (bytes, bytearray)):
        name = filename or "upload"
        return name, bytes(value), mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    name = filename or Path(str(getattr(value, "name", "upload"))).name
    return name, value, mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _skill_file_mode(files: Sequence[Mapping[str, Any] | tuple]) -> str:
    mapping_count = sum(isinstance(item, Mapping) for item in files)
    if mapping_count == len(files):
        return "json"
    if mapping_count == 0:
        return "multipart"
    raise ValueError("Skill files must be all JSON file objects or all multipart tuples")
