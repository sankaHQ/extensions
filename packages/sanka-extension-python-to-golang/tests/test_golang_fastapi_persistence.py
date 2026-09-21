# SPDX-License-Identifier: Apache-2.0
"""Static FastAPI persistence contract capture."""

from pathlib import Path

from sanka_extension_python_to_golang.capture import capture, configuration
from sanka_extension_python_to_golang.persistence import capture_fastapi_persistence


def _write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_captures_validation_schema_history_and_transactions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/schemas.py",
        """from pydantic import BaseModel, ConfigDict, Field

class Address(BaseModel):
    line1: str

class WidgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=True)
    name: str = Field(
        min_length=1,
        max_length=80,
        validation_alias="displayName",
        serialization_alias="display_name",
    )
    note: str | None
    address: Address
    tags: list[str] = Field(default_factory=list)
""",
    )
    _write(
        tmp_path,
        "app/models.py",
        """from uuid import UUID
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    widgets: Mapped[list["Widget"]] = relationship(back_populates="workspace")

class Widget(Base):
    __tablename__ = "widgets"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80), unique=True)
    workspace: Mapped[Workspace] = relationship(back_populates="widgets")
""",
    )
    _write(
        tmp_path,
        "alembic/versions/0001_workspaces.py",
        """from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("workspaces", sa.Column("id", sa.UUID(), primary_key=True))

def downgrade():
    op.drop_table("workspaces")
""",
    )
    _write(
        tmp_path,
        "alembic/versions/0002_widgets.py",
        """from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "widgets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        ),
    )
    op.create_index("ix_widgets_workspace", "widgets", ["workspace_id"])

def downgrade():
    op.drop_index("ix_widgets_workspace", table_name="widgets")
    op.drop_table("widgets")
""",
    )
    _write(
        tmp_path,
        "app/repositories/widgets.py",
        """from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Widget

async def find_widget(session: AsyncSession, widget_id):
    result = await session.execute(select(Widget).where(Widget.id == widget_id))
    return result.scalar_one_or_none()

async def create_widget(session: AsyncSession, widget: Widget):
    async with session.begin():
        session.add(widget)
        await session.flush()
        await session.refresh(widget)
    return widget
""",
    )

    captured = capture_fastapi_persistence(tmp_path)

    assert captured is not None
    assert captured["schema"] == "sanka.python-to-golang.fastapi-persistence/v1"
    assert captured["files"] == [
        "alembic/versions/0001_workspaces.py",
        "alembic/versions/0002_widgets.py",
        "app/models.py",
        "app/repositories/widgets.py",
        "app/schemas.py",
    ]
    create = next(model for model in captured["pydantic_models"] if model["name"] == "WidgetCreate")
    assert create["config"] == {
        "extra": "forbid",
        "validate_by_alias": True,
        "validate_by_name": True,
    }
    assert create["fields"] == [
        {
            "name": "name",
            "annotation": "str",
            "required": True,
            "nullable": False,
            "field": {
                "max_length": 80,
                "min_length": 1,
                "serialization_alias": "display_name",
                "validation_alias": "displayName",
            },
        },
        {
            "name": "note",
            "annotation": "str | None",
            "required": True,
            "nullable": True,
            "field": {},
        },
        {
            "name": "address",
            "annotation": "Address",
            "required": True,
            "nullable": False,
            "field": {},
        },
        {
            "name": "tags",
            "annotation": "list[str]",
            "required": False,
            "nullable": False,
            "field": {"default_factory": "list"},
        },
    ]
    widget = next(model for model in captured["sqlalchemy_models"] if model["name"] == "Widget")
    assert widget["table"] == "widgets"
    assert widget["columns"][1] == {
        "name": "workspace_id",
        "annotation": "UUID",
        "type": None,
        "foreign_keys": [{"target": "workspaces.id", "options": {"ondelete": "CASCADE"}}],
        "options": {"index": True},
    }
    assert widget["relationships"] == [
        {"name": "workspace", "annotation": "Workspace", "options": {"back_populates": "widgets"}}
    ]
    assert [(item["revision"], item["down_revision"]) for item in captured["migrations"]] == [
        ("0001", None),
        ("0002", "0001"),
    ]
    assert [operation["name"] for operation in captured["migrations"][1]["upgrade"]] == [
        "create_table",
        "create_index",
    ]
    repositories = {item["name"]: item for item in captured["repositories"]}
    assert repositories["find_widget"]["transaction"] == "implicit"
    assert repositories["find_widget"]["operations"] == [
        {
            "name": "execute",
            "arguments": ["select(Widget).where(Widget.id == widget_id)"],
            "options": {},
        }
    ]
    assert repositories["create_widget"]["transaction"] == "begin"
    assert [operation["name"] for operation in repositories["create_widget"]["operations"]] == [
        "add",
        "flush",
        "refresh",
    ]
    assert captured["gaps"] == []


def test_source_capture_includes_persistence_without_generic_file_gaps(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        """from fastapi import FastAPI
app = FastAPI()
@app.get("/health")
def health():
    return {"ok": True}
""",
    )
    _write(
        tmp_path,
        "schemas.py",
        """from pydantic import BaseModel
class Health(BaseModel):
    ok: bool
""",
    )

    first = capture(tmp_path, configuration({"source_framework": "fastapi"}))
    second = capture(tmp_path, configuration({"source_framework": "fastapi"}))

    assert first == second
    assert first["fastapi_persistence"]["files"] == ["schemas.py"]
    assert "persistence: captured contracts require Go lowering" in first["gaps"]
    assert not any("additional Python modules" in gap for gap in first["gaps"])


def test_dynamic_persistence_behavior_is_prefixed_capture_gap(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        """from fastapi import FastAPI
app = FastAPI()
@app.get("/health")
def health():
    return {"ok": True}
""",
    )
    _write(
        tmp_path,
        "schemas.py",
        """from pydantic import BaseModel, Field
class Input(BaseModel):
    name: str = Field(validation_alias=build_alias())
""",
    )
    _write(
        tmp_path,
        "alembic/versions/0001_dynamic.py",
        """from alembic import op
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    op.execute(build_sql())
def downgrade():
    op.drop_table("widgets")
""",
    )
    _write(
        tmp_path,
        "repositories.py",
        """from sqlalchemy.ext.asyncio import AsyncSession
async def stream_widgets(session: AsyncSession):
    return await session.stream(build_query())
""",
    )

    captured = capture(tmp_path, configuration({"source_framework": "fastapi"}))

    assert any(gap.startswith("persistence: schemas.py:Input:") for gap in captured["gaps"])
    assert any("dynamic migration operation" in gap for gap in captured["gaps"])
    assert any("unsupported AsyncSession operation stream" in gap for gap in captured["gaps"])
