"""Guard the Store/Role enum semantics.

These enums are declared with `enum.StrEnum` (ruff UP042). The migration from
`class X(str, enum.Enum)` is only safe because of the properties asserted here:

  * name == value for every member, so SQLAlchemy's persisted representation is
    unchanged on disk
  * a DB round trip still yields the same enum type and value
  * Pydantic still coerces the member into a plain `str` field as the original
    API value

If any of these ever stops holding, serialized API responses or stored rows
would change silently.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Cookbook, Role, Store, User


@pytest.mark.parametrize("member", list(Store) + list(Role))
def test_name_matches_value(member):
    # SQLAlchemy Enum and the .value sites in the routers both rely on this.
    assert member.name == member.value


def test_enum_members_are_str_subclasses():
    assert issubclass(Store, str) and issubclass(Role, str)
    assert isinstance(Store.local, str) and isinstance(Role.admin, str)


def test_string_equality_and_name_lookup():
    assert Store.local == "local"
    assert Role.admin == "admin"
    assert Store["local"] is Store.local
    assert Role["user"] is Role.user


def test_pydantic_coerces_into_plain_str():
    from pydantic import BaseModel

    class UserOut(BaseModel):
        role: str

    assert UserOut(role=Role.admin).role == "admin"


def test_database_round_trip(database_url):
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        email = "enum-roundtrip@example.com"
        existing = session.query(User).filter(User.email == email).first()
        if existing is not None:
            session.query(Cookbook).filter(Cookbook.owner_id == existing.id).delete()
            session.query(User).filter(User.email == email).delete()
            session.commit()

        user = User(email=email, hashed_password="x", role=Role.admin)
        session.add(user)
        session.commit()
        session.refresh(user)

        assert user.role is Role.admin
        assert user.role.value == "admin"

        book = Cookbook(name="enum-roundtrip-cb", description="d",
                        store=Store.cloud, owner_id=user.id)
        session.add(book)
        session.commit()
        session.refresh(book)

        assert book.store is Store.cloud
        assert book.store.value == "cloud"

        # The value actually written to the column, not just the ORM cache.
        raw = session.execute(
            text("SELECT role FROM users WHERE email = :e"), {"e": email}
        ).scalar()
        assert raw == "admin"

        # Querying by enum member, as services/ingest.py does.
        assert session.query(Cookbook).filter(Cookbook.store == Store.cloud).count() >= 1

        session.query(Cookbook).filter(Cookbook.owner_id == user.id).delete()
        session.query(User).filter(User.email == email).delete()
        session.commit()
    finally:
        session.close()


@pytest.fixture
def database_url():
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url
