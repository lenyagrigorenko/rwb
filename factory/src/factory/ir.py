"""Intermediate Representation (IR) of a generated application.

The IR is the single source of truth: analysis docs, OpenAPI, AsyncAPI, DB migrations,
backend, admin UI, mobile app and tests are all rendered from it. The LLM produces the IR,
deterministic generators consume it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field as PField, field_validator, model_validator

FieldType = Literal[
    "string",      # short text, max_length applies
    "text",        # long text
    "int",
    "decimal",
    "money",       # decimal with 2 digits, stored in minor units is NOT assumed
    "bool",
    "date",
    "datetime",
    "enum",        # enum_values required
    "ref",         # ref required: entity name or "User"
    "email",
    "phone",
    "url",
    "json",        # free-form JSON (jsonb)
    "geo_point",   # PostGIS Point, SRID 4326
    "geo_line",    # LineString / MultiLineString
    "geo_polygon",
    "geometry",    # any geometry
]

CrudAction = Literal["read", "read_own", "create", "update", "update_own", "delete"]

PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
CAMEL = re.compile(r"^[a-z][A-Za-z0-9]*$")


class Field(BaseModel):
    name: str = PField(description="camelCase, English")
    title: str = PField(description="Человекочитаемое название на русском")
    type: FieldType
    required: bool = False
    unique: bool = False
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    regex: str | None = None
    enum_values: list[str] | None = PField(default=None, description="latin identifiers")
    enum_titles: dict[str, str] | None = PField(default=None, description="value -> русская подпись")
    ref: str | None = None
    default: Any = None
    examples: list[str] = PField(default=[], description="Realistic sample values for seed data")
    description: str = ""
    list_visible: bool = True
    searchable: bool = False
    readonly: bool = PField(default=False, description="Заполняется системой, не редактируется в формах")

    @field_validator("name")
    @classmethod
    def _camel(cls, v: str) -> str:
        if not CAMEL.match(v):
            raise ValueError(f"field name must be camelCase: {v}")
        return v

    @model_validator(mode="after")
    def _type_specific(self) -> "Field":
        if self.type == "enum" and not self.enum_values:
            raise ValueError(f"enum field {self.name} needs enum_values")
        for v in self.enum_values or []:
            if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", v):
                raise ValueError(f"enum value must be a latin identifier: {v}")
        if self.type == "ref" and not self.ref:
            raise ValueError(f"ref field {self.name} needs ref")
        if self.type == "string" and self.max_length is None:
            self.max_length = 255
        return self


class Transition(BaseModel):
    name: str = PField(description="camelCase action name, e.g. close")
    title: str
    from_states: list[str]
    to: str
    roles: list[str]
    emits: str | None = PField(default=None, description="Event name emitted on transition")


class Lifecycle(BaseModel):
    field: str = PField(default="status", description="enum field holding the state")
    initial: str
    transitions: list[Transition]


class Entity(BaseModel):
    name: str = PField(description="PascalCase, singular, English")
    title: str
    title_plural: str
    description: str = ""
    fields: list[Field]
    owner_field: str | None = PField(
        default=None, description="ref(User) field used for *_own permissions"
    )
    display_field: str | None = PField(default=None, description="field shown as a label")
    lifecycle: Lifecycle | None = None
    seed_count: int = PField(default=5, description="Synthetic demo rows if no dataset")
    covers: list[str] = []

    @field_validator("name")
    @classmethod
    def _pascal(cls, v: str) -> str:
        if not PASCAL.match(v):
            raise ValueError(f"entity name must be PascalCase: {v}")
        return v

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def label_field(self) -> str:
        if self.display_field:
            return self.display_field
        for f in self.fields:
            if f.type in ("string", "email") and f.name not in ("id",):
                return f.name
        return "id"

    @property
    def geo_fields(self) -> list[Field]:
        return [f for f in self.fields if f.type.startswith("geo") or f.type == "geometry"]


class Role(BaseModel):
    name: str = PField(description="PascalCase, e.g. Driver")
    title: str
    description: str = ""
    demo_login: str = PField(description="login of the seeded demo user, lowercase latin")


class Permission(BaseModel):
    role: str
    entity: str
    actions: list[CrudAction]
    owner_field: str | None = PField(
        default=None, description="ref(User) поле «своих» записей для этой роли, если отличается от owner_field сущности")


class Operation(BaseModel):
    """Custom business operation. Its body is written by the LLM coder."""

    id: str = PField(description="OP-001 style id")
    name: str = PField(description="PascalCase verb phrase, e.g. SubmitTrackPoint")
    title: str
    description: str = PField(description="Precise behaviour spec for the coder")
    method: Literal["GET", "POST", "PUT", "DELETE"] = "POST"
    path: str = PField(description="Relative to /api/v1, e.g. /vehicles/{vehicleId}/track")
    roles: list[str]
    input: list[Field] = []
    output: list[Field] = []
    output_entity: str | None = PField(default=None, description="Return entity DTO instead")
    output_list: bool = False
    emits: list[str] = []
    idempotent: bool = False
    covers: list[str] = []

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.path)


class Event(BaseModel):
    name: str = PField(description="PascalCase past tense, e.g. ZoneEntered")
    title: str
    description: str = ""
    version: int = 1
    topic: str = PField(description="kafka topic, kebab-case")
    key_field: str
    payload: list[Field]
    notify: str | None = PField(
        default=None,
        description="Russian notification text template with {field} placeholders",
    )
    recipient_field: str | None = PField(
        default=None, description="payload field holding the User id to notify"
    )


class Job(BaseModel):
    name: str = PField(description="PascalCase, e.g. CheckExpiringSessions")
    title: str
    description: str
    interval_seconds: int = 30
    emits: list[str] = []
    covers: list[str] = []


class IntegrationMethod(BaseModel):
    name: str
    description: str
    input: list[Field] = []
    output: list[Field] = []
    mock_response: dict[str, Any] = PField(
        default={}, description="Ответ mock-сервиса; {inputField} подставляются из запроса"
    )


class Integration(BaseModel):
    name: str = PField(description="PascalCase, e.g. ParkingProvider")
    title: str
    description: str
    real_system: str = PField(description="Название внешней системы, которую заменяет mock")
    methods: list[IntegrationMethod]


class Dataset(BaseModel):
    name: str
    file: str = PField(description="path relative to case data dir")
    records_path: str = PField(description="dot path to the array of records, e.g. data.zones")
    entity: str
    mapping: dict[str, str] = PField(description="entity field -> record key (dot path allowed)")
    geometry_format: Literal["wkt", "geojson", "none"] = "none"


WidgetType = Literal[
    "kpi", "table", "map", "status_card", "countdown", "action_button",
    "event_feed", "notifications", "list",
]


class Widget(BaseModel):
    type: WidgetType
    title: str
    entity: str | None = None
    operation: str | None = None
    field: str | None = None
    props: dict[str, Any] = {}


class Screen(BaseModel):
    id: str
    name: str = PField(description="PascalCase")
    title: str
    platform: Literal["web", "mobile"]
    roles: list[str]
    description: str = ""
    widgets: list[Widget]
    covers: list[str] = []


class DemoStep(BaseModel):
    id: str
    title: str
    action: Literal["route", "operation", "create", "advance_time", "transition", "expect"]
    operation: str | None = None
    entity: str | None = None
    transition: str | None = None
    minutes: int | None = None
    args: dict[str, Any] = {}
    expect: str = ""
    expect_error: bool = PField(default=False, description="Шаг проверяет отказ: успех = ответ с ошибкой 4xx")


class Demo(BaseModel):
    title: str
    steps: list[DemoStep]


class Requirement(BaseModel):
    id: str = PField(description="FR-001 / NFR-001")
    text: str
    priority: Literal["must", "should", "could"] = "must"
    source: Literal["customer", "assumption"] = "customer"


class AcceptanceCriterion(BaseModel):
    id: str = PField(description="AC-001")
    fr: str
    given: str
    when: str
    then: str
    test_level: Literal["unit", "api", "ui"] = "api"


class Ambiguity(BaseModel):
    id: str
    question: str
    impact: Literal["high", "medium", "low"]
    affects: list[str] = []
    options: list[str] = []
    default_answer: str
    rationale: str
    answer: str | None = None


class Scenario(BaseModel):
    id: str
    title: str
    actor: str
    steps: list[str]
    alternatives: list[str] = []


class Analysis(BaseModel):
    """Stage 1 output: business analysis of the requirements."""

    product_name: str
    summary: str
    goals: list[str]
    scope_in: list[str]
    scope_out: list[str]
    roles: list[Role]
    functional: list[Requirement]
    non_functional: list[Requirement]
    constraints: list[str]
    scenarios: list[Scenario]
    integrations: list[str]
    security: list[str]
    acceptance: list[AcceptanceCriterion]
    risks: list[str]
    ambiguities: list[Ambiguity]
    assumptions: list[str]
    components: dict[str, bool] = PField(
        default_factory=lambda: {"web": True, "mobile": False, "kafka": False, "geo": False}
    )


class AppSpec(BaseModel):
    """Stage 3 output: complete technical model of the app."""

    slug: str = PField(description="kebab-case project id")
    namespace: str = PField(description="C# root namespace, PascalCase")
    product_name: str
    map_center: list[float] = PField(default=[55.7558, 37.6173], description="[lat, lon] центра карты")
    analysis: Analysis
    entities: list[Entity]
    permissions: list[Permission]
    operations: list[Operation] = []
    events: list[Event] = []
    jobs: list[Job] = []
    integrations: list[Integration] = []
    datasets: list[Dataset] = []
    screens: list[Screen] = []
    demo: Demo | None = None

    @property
    def roles(self) -> list[Role]:
        return self.analysis.roles

    def entity(self, name: str) -> Entity | None:
        return next((e for e in self.entities if e.name == name), None)

    def actions(self, role: str, entity: str) -> list[str]:
        for p in self.permissions:
            if p.role == role and p.entity == entity:
                return list(p.actions)
        return []

    @property
    def uses_geo(self) -> bool:
        return any(e.geo_fields for e in self.entities)

    @property
    def uses_kafka(self) -> bool:
        return bool(self.events)

    @property
    def has_mobile(self) -> bool:
        return any(s.platform == "mobile" for s in self.screens)
