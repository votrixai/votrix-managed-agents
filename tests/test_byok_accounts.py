"""Multi-provider BYOK Accounts stay isolated from Platform administration."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from app.db.models.accounts import (
    ACCOUNT_ACTIVE,
    ACCOUNT_SUSPENDED,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_SUSPENDED,
    FUNDING_BYOK,
)
from app.db.queries import accounts as accounts_q
from app.db.queries import sessions as sessions_q
from app.models.accounts import AccountCreateRequest
from app.models.errors import AccountUnavailable, Conflict, InvalidRequest, NotFound
from app.services import accounts as service
from app.services.account_credentials import (
    SubmittedByokCredential,
    credential_fingerprint,
)
from tests.conftest import FakeKeys


class FakeByokValidator:
    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    async def validate(self, *, backend: str, api_key: SecretStr) -> None:
        self.calls.append((backend, api_key.get_secret_value()))
        if error := self.errors.get(backend):
            raise error


def credentials(**values: str) -> tuple[SubmittedByokCredential, ...]:
    return tuple(
        SubmittedByokCredential(backend=backend, api_key=SecretStr(api_key))
        for backend, api_key in values.items()
    )


async def test_byok_creation_validates_encrypts_multiple_direct_keys_without_minting(
    db, org
):
    admin = FakeKeys()
    validator = FakeByokValidator()

    account = await service.create_account(
        db,
        organization_id=org,
        name="Direct providers",
        funding_mode="byok",
        byok_credentials=credentials(
            openai="sk-openai-user-owned",
            anthropic="sk-ant-user-owned",
        ),
        keys=admin,
        byok_validator=validator,
    )

    assert account.status == ACCOUNT_ACTIVE
    assert account.funding_mode == FUNDING_BYOK
    assert account.limit_usd is None
    assert [row.backend for row in account.model_credentials] == [
        "anthropic",
        "openai",
    ]
    assert all(row.status == CREDENTIAL_ACTIVE for row in account.model_credentials)
    assert all(row.provider_key_name is None for row in account.model_credentials)
    assert all(row.key_hash.startswith(f"byok:{row.backend}:") for row in account.model_credentials)
    assert all("user-owned" not in row.encrypted_key for row in account.model_credentials)
    assert admin.created == []
    assert Counter(validator.calls) == Counter(
        [
            ("openai", "sk-openai-user-owned"),
            ("anthropic", "sk-ant-user-owned"),
        ]
    )

    anthropic = await service.resolve_spendable_credential(
        db,
        organization_id=org,
        account_id=account.id,
        model_provider="anthropic",
    )
    openai = await service.resolve_spendable_credential(
        db,
        organization_id=org,
        account_id=account.id,
        model_provider="openai",
    )
    assert anthropic.backend == "anthropic"
    assert anthropic.api_key.get_secret_value() == "sk-ant-user-owned"
    assert openai.backend == "openai"
    assert openai.api_key.get_secret_value() == "sk-openai-user-owned"


def test_byok_request_rejects_platform_limits_duplicates_and_openrouter():
    with pytest.raises(ValidationError):
        AccountCreateRequest(
            name="Not provider-enforced",
            limit_usd=Decimal("10"),
            funding={
                "type": "byok",
                "credentials": [{"backend": "openai", "api_key": "sk-user"}],
            },
        )

    with pytest.raises(ValidationError, match="one key per backend"):
        AccountCreateRequest(
            name="Duplicate",
            funding={
                "type": "byok",
                "credentials": [
                    {"backend": "openai", "api_key": "one"},
                    {"backend": "openai", "api_key": "two"},
                ],
            },
        )

    with pytest.raises(ValidationError):
        AccountCreateRequest(
            name="Gateway is Platform-only",
            funding={
                "type": "byok",
                "credentials": [
                    {"backend": "openrouter", "api_key": "sk-or-user"}
                ],
            },
        )


async def test_service_rejects_duplicate_backends_before_validation(db, org):
    validator = FakeByokValidator()
    with pytest.raises(InvalidRequest, match="already has"):
        await service.create_account(
            db,
            organization_id=org,
            name="Duplicate",
            funding_mode="byok",
            byok_credentials=(
                *credentials(openai="one"),
                *credentials(openai="two"),
            ),
            byok_validator=validator,
        )
    assert validator.calls == []


async def test_invalid_byok_key_leaves_no_account_or_credentials(db, org):
    validator = FakeByokValidator(
        {"anthropic": InvalidRequest("anthropic rejected the BYOK api_key")}
    )

    with pytest.raises(InvalidRequest):
        await service.create_account(
            db,
            organization_id=org,
            name="Bad key set",
            funding_mode="byok",
            byok_credentials=credentials(anthropic="bad", openai="good"),
            byok_validator=validator,
        )

    accounts = await accounts_q.list_accounts(db, organization_id=org)
    assert [account.name for account in accounts.items] == [
        service.DEFAULT_ACCOUNT_NAME
    ]


async def test_byok_idempotency_compares_the_full_provider_set_order_independently(
    db, org
):
    validator = FakeByokValidator()
    first = await service.create_account(
        db,
        organization_id=org,
        name="Direct",
        idempotency_key="same-create",
        funding_mode="byok",
        byok_credentials=credentials(openai="sk-first", anthropic="sk-ant"),
        byok_validator=validator,
    )
    repeated = await service.create_account(
        db,
        organization_id=org,
        name="Direct",
        idempotency_key="same-create",
        funding_mode="byok",
        byok_credentials=credentials(anthropic="sk-ant", openai="sk-first"),
        byok_validator=validator,
    )

    assert repeated.id == first.id
    assert len(validator.calls) == 2

    with pytest.raises(Conflict):
        await service.create_account(
            db,
            organization_id=org,
            name="Direct",
            idempotency_key="same-create",
            funding_mode="byok",
            byok_credentials=credentials(openai="sk-different", anthropic="sk-ant"),
            byok_validator=validator,
        )


async def test_one_byok_key_cannot_fund_two_accounts(db, org):
    validator = FakeByokValidator()
    await service.create_account(
        db,
        organization_id=org,
        name="First",
        funding_mode="byok",
        byok_credentials=credentials(google="google-key"),
        byok_validator=validator,
    )

    with pytest.raises(Conflict):
        await service.create_account(
            db,
            organization_id=org,
            name="Second",
            funding_mode="byok",
            byok_credentials=credentials(google="google-key"),
            byok_validator=validator,
        )


async def test_byok_account_cannot_enter_platform_provisioning(db, org):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Direct only",
        funding_mode="byok",
        byok_credentials=credentials(openai="openai-key"),
        byok_validator=FakeByokValidator(),
    )
    admin = FakeKeys()

    with pytest.raises(Conflict, match="cannot be provisioned"):
        await service._provision_credential(db, account=account, keys=admin)

    assert admin.created == []


async def test_byok_key_can_be_added_replaced_and_repeated_idempotently(db, org):
    validator = FakeByokValidator()
    account = await service.create_account(
        db,
        organization_id=org,
        name="Mutable direct keys",
        funding_mode="byok",
        byok_credentials=credentials(anthropic="anthropic-key"),
        byok_validator=validator,
    )

    account = await service.set_byok_model_credential(
        db,
        organization_id=org,
        account_id=account.id,
        backend="openai",
        api_key=SecretStr("openai-key-one"),
        byok_validator=validator,
    )
    openai = next(
        row for row in account.model_credentials if row.backend == "openai"
    )
    credential_id = openai.id
    first_ciphertext = openai.encrypted_key
    assert openai.generation == 1
    assert [row.backend for row in account.model_credentials] == [
        "anthropic",
        "openai",
    ]

    # A retry with the same key is a true PUT retry: no validation call and no
    # artificial rotation generation.
    calls_before_retry = list(validator.calls)
    account = await service.set_byok_model_credential(
        db,
        organization_id=org,
        account_id=account.id,
        backend="openai",
        api_key=SecretStr("openai-key-one"),
        byok_validator=validator,
    )
    repeated = next(
        row for row in account.model_credentials if row.backend == "openai"
    )
    assert repeated.id == credential_id
    assert repeated.generation == 1
    assert validator.calls == calls_before_retry

    account = await service.set_byok_model_credential(
        db,
        organization_id=org,
        account_id=account.id,
        backend="openai",
        api_key=SecretStr("openai-key-two"),
        byok_validator=validator,
    )
    replaced = next(
        row for row in account.model_credentials if row.backend == "openai"
    )
    assert replaced.id == credential_id
    assert replaced.generation == 2
    assert replaced.encrypted_key != first_ciphertext
    assert "openai-key-two" not in replaced.encrypted_key
    resolved = await service.resolve_spendable_credential(
        db,
        organization_id=org,
        account_id=account.id,
        model_provider="openai",
    )
    assert resolved.api_key.get_secret_value() == "openai-key-two"


async def test_failed_byok_replacement_keeps_the_previous_key(db, org):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Keep the working key",
        funding_mode="byok",
        byok_credentials=credentials(openai="working-key"),
        byok_validator=FakeByokValidator(),
    )
    original = account.model_credentials[0]
    account_id = account.id
    original_hash = original.key_hash
    original_generation = original.generation
    rejecting = FakeByokValidator(
        {"openai": InvalidRequest("openai rejected the BYOK api_key")}
    )

    with pytest.raises(InvalidRequest):
        await service.set_byok_model_credential(
            db,
            organization_id=org,
            account_id=account_id,
            backend="openai",
            api_key=SecretStr("bad-replacement"),
            byok_validator=rejecting,
        )

    resolved = await service.resolve_spendable_credential(
        db,
        organization_id=org,
        account_id=account_id,
        model_provider="openai",
    )
    refreshed = await service.get_account(
        db, organization_id=org, account_id=account_id
    )
    assert resolved.api_key.get_secret_value() == "working-key"
    assert refreshed.model_credentials[0].key_hash == original_hash
    assert refreshed.model_credentials[0].generation == original_generation


async def test_key_owned_by_another_account_cannot_be_added(db, org):
    validator = FakeByokValidator()
    await service.create_account(
        db,
        organization_id=org,
        name="Key owner",
        funding_mode="byok",
        byok_credentials=credentials(openai="already-owned-key"),
        byok_validator=validator,
    )
    target = await service.create_account(
        db,
        organization_id=org,
        name="Other Account",
        funding_mode="byok",
        byok_credentials=credentials(anthropic="anthropic-key"),
        byok_validator=validator,
    )
    target_id = target.id

    with pytest.raises(Conflict, match="another Account"):
        await service.set_byok_model_credential(
            db,
            organization_id=org,
            account_id=target_id,
            backend="openai",
            api_key=SecretStr("already-owned-key"),
            byok_validator=validator,
        )

    target = await service.get_account(
        db, organization_id=org, account_id=target_id
    )
    assert [row.backend for row in target.model_credentials] == ["anthropic"]


async def test_byok_key_removal_keeps_one_key_and_blocks_removed_models(db, org):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Remove one backend",
        funding_mode="byok",
        byok_credentials=credentials(
            anthropic="anthropic-key",
            openai="openai-key",
        ),
        byok_validator=FakeByokValidator(),
    )

    account = await service.delete_byok_model_credential(
        db,
        organization_id=org,
        account_id=account.id,
        backend="openai",
    )
    account_id = account.id
    assert [row.backend for row in account.model_credentials] == ["anthropic"]
    with pytest.raises(AccountUnavailable, match="no openai credential"):
        await service.resolve_spendable_credential(
            db,
            organization_id=org,
            account_id=account_id,
            model_provider="openai",
        )
    with pytest.raises(NotFound, match="no openai credential"):
        await service.delete_byok_model_credential(
            db,
            organization_id=org,
            account_id=account_id,
            backend="openai",
        )
    await db.rollback()
    with pytest.raises(Conflict, match="at least one"):
        await service.delete_byok_model_credential(
            db,
            organization_id=org,
            account_id=account_id,
            backend="anthropic",
        )


async def test_platform_account_rejects_all_user_key_mutation(db, org):
    account = await accounts_q.get_default_account(db, organization_id=org)
    validator = FakeByokValidator()

    with pytest.raises(Conflict, match="Platform Account"):
        await service.set_byok_model_credential(
            db,
            organization_id=org,
            account_id=account.id,
            backend="openai",
            api_key=SecretStr("user-key"),
            byok_validator=validator,
        )
    assert validator.calls == []
    with pytest.raises(Conflict, match="Platform Account"):
        await service.delete_byok_model_credential(
            db,
            organization_id=org,
            account_id=account.id,
            backend="openai",
        )


async def test_key_added_to_suspended_byok_account_stays_suspended(db, org):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Suspended key changes",
        funding_mode="byok",
        byok_credentials=credentials(anthropic="anthropic-key"),
        byok_validator=FakeByokValidator(),
    )
    account = await service.suspend_account(
        db, organization_id=org, account_id=account.id
    )

    account = await service.set_byok_model_credential(
        db,
        organization_id=org,
        account_id=account.id,
        backend="google",
        api_key=SecretStr("google-key"),
        byok_validator=FakeByokValidator(),
    )

    assert account.status == ACCOUNT_SUSPENDED
    assert all(
        row.status == CREDENTIAL_SUSPENDED for row in account.model_credentials
    )


async def test_byok_never_falls_back_when_the_selected_provider_key_is_missing(
    db, org
):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Anthropic only",
        funding_mode="byok",
        byok_credentials=credentials(anthropic="anthropic-key"),
        byok_validator=FakeByokValidator(),
    )

    with pytest.raises(AccountUnavailable, match="no openai credential"):
        await service.resolve_spendable_credential(
            db,
            organization_id=org,
            account_id=account.id,
            model_provider="openai",
        )
    assert (
        await service.resolve_optional_spendable_credential(
            db,
            organization_id=org,
            account_id=account.id,
            model_provider="google",
        )
        is None
    )


async def test_byok_suspend_is_local_and_resume_revalidates_every_key(db, org):
    admin = FakeKeys()
    validator = FakeByokValidator()
    account = await service.create_account(
        db,
        organization_id=org,
        name="Direct",
        funding_mode="byok",
        byok_credentials=credentials(
            deepseek="deepseek-key",
            google="google-key",
        ),
        keys=admin,
        byok_validator=validator,
    )

    suspended = await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=admin
    )
    assert suspended.status == ACCOUNT_SUSPENDED
    assert all(
        row.status == CREDENTIAL_SUSPENDED
        for row in suspended.model_credentials
    )
    assert admin.updates == []

    resumed = await service.resume_account(
        db,
        organization_id=org,
        account_id=account.id,
        keys=admin,
        byok_validator=validator,
    )
    assert resumed.status == ACCOUNT_ACTIVE
    assert all(row.status == CREDENTIAL_ACTIVE for row in resumed.model_credentials)
    assert Counter(validator.calls) == Counter(
        [
            ("deepseek", "deepseek-key"),
            ("google", "google-key"),
            ("deepseek", "deepseek-key"),
            ("google", "google-key"),
        ]
    )
    assert admin.updates == []
    vision = await service.resolve_optional_spendable_credential(
        db,
        organization_id=org,
        account_id=account.id,
        model_provider="google",
    )
    assert vision is not None
    assert vision.backend == "google"
    assert vision.api_key.get_secret_value() == "google-key"


async def test_byok_usage_aggregates_account_tokens_without_fake_usd(
    db, org, agent, environment
):
    account = await service.create_account(
        db,
        organization_id=org,
        name="Direct",
        funding_mode="byok",
        byok_credentials=credentials(
            anthropic="anthropic-key",
            openai="openai-key",
        ),
        byok_validator=FakeByokValidator(),
    )
    session = await sessions_q.create_session(
        db,
        organization_id=org,
        agent_id=agent.id,
        agent_version=agent.active_version,
        environment_id=environment.id,
        account_id=account.id,
    )
    for backend, model, input_tokens, output_tokens in (
        ("anthropic", "claude-opus-5", 11, 7),
        ("openai", "gpt-5.6-sol", 5, 3),
    ):
        await sessions_q.append_event(
            db,
            session,
            type="model.usage",
            source="agent",
            payload={
                "model": model,
                "backend": backend,
                "source": "agent",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
        )
    await db.commit()

    usage = await service.get_account_usage(
        db, organization_id=org, account_id=account.id, keys=FakeKeys()
    )

    assert usage.backends == ("anthropic", "openai")
    assert usage.usage_usd is None
    assert usage.usage_daily_usd is None
    assert usage.limit_usd is None
    assert usage.observed_input_tokens == 16
    assert usage.observed_output_tokens == 10
    assert usage.observed_total_tokens == 26


def test_fingerprints_are_provider_scoped_and_never_contain_the_key():
    key = SecretStr("same-secret")
    anthropic = credential_fingerprint(backend="anthropic", api_key=key)
    openai = credential_fingerprint(backend="openai", api_key=key)

    assert anthropic != openai
    assert "same-secret" not in anthropic
    assert "same-secret" not in openai
