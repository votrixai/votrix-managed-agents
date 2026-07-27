from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import quote

from ._constants import API_KEYS_PATH, MODEL_PROVIDERS_PATH, VAULTS_PATH
from ._models import (
    ApiKey,
    ApiKeyCreated,
    ApiKeyScope,
    DeletedObject,
    ModelCredential,
    ModelProvider,
    Vault,
    VotrixModel,
)
from ._resources import NOT_GIVEN, _NotGiven
from ._sync_client import Votrix
from ._sync_pagination import SyncPage

T = TypeVar("T", bound=VotrixModel)


def _path_id(value: str) -> str:
    return quote(str(value), safe="")


def _body(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not NOT_GIVEN}


def _page(
    client: Votrix,
    path: str,
    model: type[T],
    params: Mapping[str, Any],
) -> SyncPage[T]:
    clean_params = {key: value for key, value in params.items() if value is not None}

    def load(page_params: Mapping[str, Any]):
        return client.request_list("GET", path, model=model, params=page_params)

    envelope = load(clean_params)
    return SyncPage(
        envelope,
        loader=load,
        base_params=clean_params,
        current_cursor=clean_params.get("page"),
    )


class SyncVaultsResource:
    def __init__(self, client: Votrix) -> None:
        self._client = client
        self.model_credentials = SyncModelCredentialsResource(client)

    def create(
        self,
        *,
        display_name: str,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> Vault:
        return self._client.request(
            "POST",
            VAULTS_PATH,
            model=Vault,
            json=_body(display_name=display_name, metadata=metadata),
        )

    def retrieve(self, vault_id: str) -> Vault:
        return self._client.request(
            "GET",
            f"{VAULTS_PATH}/{_path_id(vault_id)}",
            model=Vault,
        )

    def update(self, vault_id: str, **changes: Any) -> Vault:
        return self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}",
            model=Vault,
            json=changes,
        )

    def list(
        self,
        *,
        limit: int = 50,
        page: str | None = None,
        include_archived: bool = False,
    ) -> SyncPage[Vault]:
        return _page(
            self._client,
            VAULTS_PATH,
            Vault,
            {"limit": limit, "page": page, "include_archived": include_archived},
        )

    def archive(self, vault_id: str) -> Vault:
        return self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/archive",
            model=Vault,
        )

    def delete(self, vault_id: str) -> DeletedObject:
        return self._client.request(
            "DELETE",
            f"{VAULTS_PATH}/{_path_id(vault_id)}",
            model=DeletedObject,
        )


class SyncApiKeysResource:
    def __init__(self, client: Votrix) -> None:
        self._client = client

    def create(
        self,
        *,
        name: str,
        scopes: Sequence[ApiKeyScope] | _NotGiven = NOT_GIVEN,
        expires_at: datetime | str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> ApiKeyCreated:
        return self._client.request(
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
    ) -> SyncPage[ApiKey]:
        return _page(
            self._client,
            API_KEYS_PATH,
            ApiKey,
            {
                "limit": limit,
                "page": page,
                "include_revoked": include_revoked,
            },
        )

    def retrieve(self, key_id: str) -> ApiKey:
        return self._client.request(
            "GET",
            f"{API_KEYS_PATH}/{_path_id(key_id)}",
            model=ApiKey,
        )

    def revoke(
        self,
        key_id: str,
        *,
        reason: str | None | _NotGiven = NOT_GIVEN,
    ) -> ApiKey:
        body = _body(reason=reason)
        return self._client.request(
            "POST",
            f"{API_KEYS_PATH}/{_path_id(key_id)}/revoke",
            model=ApiKey,
            json=body or None,
        )

    def rotate(
        self,
        key_id: str,
        *,
        expires_at: datetime | str | None | _NotGiven = NOT_GIVEN,
        reason: str | None | _NotGiven = NOT_GIVEN,
    ) -> ApiKeyCreated:
        body = _body(expires_at=expires_at, reason=reason)
        return self._client.request(
            "POST",
            f"{API_KEYS_PATH}/{_path_id(key_id)}/rotate",
            model=ApiKeyCreated,
            json=body or None,
        )


class SyncModelCredentialsResource:
    def __init__(self, client: Votrix) -> None:
        self._client = client

    def create(
        self,
        vault_id: str,
        *,
        provider: str,
        api_key: str,
        display_name: str | None | _NotGiven = NOT_GIVEN,
        metadata: Mapping[str, Any] | _NotGiven = NOT_GIVEN,
    ) -> ModelCredential:
        return self._client.request(
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
    ) -> SyncPage[ModelCredential]:
        return _page(
            self._client,
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials",
            ModelCredential,
            {"limit": limit, "page": page, "include_archived": include_archived},
        )

    def retrieve(self, credential_id: str, *, vault_id: str) -> ModelCredential:
        return self._client.request(
            "GET",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=ModelCredential,
        )

    def rotate(self, vault_id: str, credential_id: str, *, api_key: str) -> ModelCredential:
        return self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=ModelCredential,
            json={"api_key": api_key},
        )

    def archive(self, credential_id: str, *, vault_id: str) -> ModelCredential:
        return self._client.request(
            "POST",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}/archive",
            model=ModelCredential,
        )

    def delete(self, credential_id: str, *, vault_id: str) -> DeletedObject:
        return self._client.request(
            "DELETE",
            f"{VAULTS_PATH}/{_path_id(vault_id)}/model_credentials/{_path_id(credential_id)}",
            model=DeletedObject,
        )


class SyncModelProvidersResource:
    def __init__(self, client: Votrix) -> None:
        self._client = client

    def list(self) -> SyncPage[ModelProvider]:
        return _page(self._client, MODEL_PROVIDERS_PATH, ModelProvider, {})

    def retrieve(self, provider_id: str) -> ModelProvider:
        return self._client.request(
            "GET",
            f"{MODEL_PROVIDERS_PATH}/{_path_id(provider_id)}",
            model=ModelProvider,
        )
