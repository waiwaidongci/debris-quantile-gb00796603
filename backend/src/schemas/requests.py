from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    model_config = ConfigDict(extra='allow')

    name: str = Field(min_length=1, max_length=180)
    csv: str | None = Field(default=None, max_length=1_000_000)
    rows: list[dict[str, Any]] | None = None
    kind: Literal['orders', 'scenarios'] | None = None


class ScenarioImport(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str | None = Field(default=None, max_length=1_000_000)
    rows: Any = None
    work_date: str | None = Field(default=None, max_length=10)
    dataset_id: int | None = Field(default=None, gt=0)


class Run(BaseModel):
    model_config = ConfigDict(extra='allow')

    dataset_id: int | None = Field(default=None, gt=0)
    options: dict = Field(default_factory=dict)
    scenario_dataset_id: int | None = Field(default=None, gt=0)
    work_date: str | None = Field(default=None, max_length=10)
    scenarios: Any = None


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None
