from sqlalchemy import BigInteger

from app.db.models.domain import ManagedResource


def test_managed_resource_version_supports_epoch_microseconds():
    assert isinstance(ManagedResource.__table__.c.version.type, BigInteger)
