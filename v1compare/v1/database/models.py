from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class MaterialBase(DeclarativeBase):
    """Tables that describe physical materials tracked by the lab manager."""

    pass


class ProcessBase(DeclarativeBase):
    """Tables that describe hardware/instrument state and process flows."""

    pass


class ExperimentBase(DeclarativeBase):
    """Tables that describe design/physics experiment context and chat logs."""

    pass


# ---------------------------------------------------------------------------
# Material inventory (chips, flakes, carriers, devices, stamps).
# ---------------------------------------------------------------------------


class Chip(MaterialBase):
    __tablename__ = "chip"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    creation_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_updated_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stage_position_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stage_position_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    corner_location_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    corner_location_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_left_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_left_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_left_angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_right_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_right_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_right_angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    height_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    table_coordinate_left: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    table_coordinate_right: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_slot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip_slot.id"), nullable=True)
    is_searched: Mapped[bool] = mapped_column(Boolean, default=False)
    material_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    carrier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_material_carrier.id"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    corner_image_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)

    current_slot: Mapped[Optional["ChipSlot"]] = relationship(back_populates="chip")
    flakes: Mapped[list["Flake"]] = relationship(back_populates="chip", cascade="all, delete-orphan")
    devices: Mapped[list["Device"]] = relationship(back_populates="chip")
    bottom_electrodes: Mapped[list["BottomElectrode"]] = relationship(back_populates="chip", cascade="all, delete-orphan")
    corner_image: Mapped[Optional["Image"]] = relationship(foreign_keys=[corner_image_id])


class RawMaterialCarrier(MaterialBase):
    __tablename__ = "raw_material_carrier"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    frame_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    qr_message: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    qr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    alignment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_orientation_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slide_angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    crystal_center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    crystal_center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_x_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_y_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_x_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_y_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_distance_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_error_px: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    table_coordinate: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)


class Stamp(MaterialBase):
    """Transfer stamps that behave similarly to raw material carriers."""

    __tablename__ = "stamp"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    frame_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    qr_message: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    qr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    alignment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_orientation_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qr_center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pickup_center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    adhesion_rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="available")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)


class Image(MaterialBase):
    __tablename__ = "image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(512))
    type: Mapped[str] = mapped_column(String(32))
    creation_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)

    __table_args__ = (UniqueConstraint("path", name="uq_image_path"),)


class Flake(MaterialBase):
    __tablename__ = "flake"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chip_id: Mapped[int] = mapped_column(ForeignKey("chip.id"), nullable=False)
    type_label: Mapped[str] = mapped_column(String(64), nullable=False)
    type_index: Mapped[int] = mapped_column(Integer, nullable=False)
    location_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    date_created: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    size_width_um: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size_length_um: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thickness_layers: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thickness_label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    size_area_um: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cleanliness: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    device_id: Mapped[Optional[int]] = mapped_column(ForeignKey("device.id"), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    image_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    image_20x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    mask_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    mask_20x_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    chip: Mapped["Chip"] = relationship(back_populates="flakes")
    image: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_id])
    image_20x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_20x_id])
    device: Mapped[Optional["Device"]] = relationship(
        "Device",
        foreign_keys="Flake.device_id",
        primaryjoin="Flake.device_id == Device.id",
        uselist=False,
    )

    __table_args__ = (UniqueConstraint("type_label", "type_index", name="uq_flake_type_index"),)


class Device(MaterialBase):
    __tablename__ = "device"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    chip_id: Mapped[int] = mapped_column(ForeignKey("chip.id"), nullable=False)
    flake_id: Mapped[Optional[int]] = mapped_column(ForeignKey("flake.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    image_5x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    image_10x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    image_20x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    image_100x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)

    chip: Mapped["Chip"] = relationship(back_populates="devices")
    flake: Mapped[Optional["Flake"]] = relationship(
        "Flake",
        foreign_keys="Device.flake_id",
        primaryjoin="Device.flake_id == Flake.id",
    )
    image_5x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_5x_id])
    image_10x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_10x_id])
    image_20x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_20x_id])
    image_100x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_100x_id])


class BottomElectrode(MaterialBase):
    __tablename__ = "bottom_electrode"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chip_id: Mapped[int] = mapped_column(ForeignKey("chip.id"), nullable=False)
    location_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    image_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)
    image_20x_id: Mapped[Optional[int]] = mapped_column(ForeignKey("image.id"), nullable=True)

    chip: Mapped["Chip"] = relationship(back_populates="bottom_electrodes")
    image: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_id])
    image_20x: Mapped[Optional["Image"]] = relationship(foreign_keys=[image_20x_id])


class ChipSlot(MaterialBase):
    __tablename__ = "chip_slot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    x_left_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    y_left_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    x_right_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    y_right_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    occupied_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    chip: Mapped[Optional["Chip"]] = relationship(back_populates="current_slot", uselist=False)

    __table_args__ = (UniqueConstraint("label", name="uq_chip_slot_label"),)



class ChipMove(MaterialBase):
    __tablename__ = "chip_move"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chip_id: Mapped[int] = mapped_column(ForeignKey("chip.id"), nullable=False)
    from_slot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip_slot.id"), nullable=True)
    to_slot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip_slot.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChipDetection(MaterialBase):
    __tablename__ = "chip_detection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chip_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip.id"), nullable=True)
    slot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip_slot.id"), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    center_x_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    center_y_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    angle_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    height_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    polygon_json: Mapped[Optional[dict[str, Any]]] = mapped_column("polygon", JSON, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detection_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    classification_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------------------
# Process database (instrument state, flows, telemetry).
# ---------------------------------------------------------------------------


class Instrument(ProcessBase):
    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    instrument_type: Mapped[str] = mapped_column(String(64))
    temperature_c: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    power_on: Mapped[bool] = mapped_column(Boolean, default=False)
    location_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location_z: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    ports: Mapped[list["InstrumentPort"]] = relationship(back_populates="instrument", cascade="all, delete-orphan")
    capabilities: Mapped[list["InstrumentCapability"]] = relationship(back_populates="instrument", cascade="all, delete-orphan")
    readings: Mapped[list["InstrumentReading"]] = relationship(back_populates="instrument", cascade="all, delete-orphan")


class InstrumentPort(ProcessBase):
    __tablename__ = "instrument_port"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    port_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="idle")
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    instrument: Mapped["Instrument"] = relationship(back_populates="ports")


class InstrumentCapability(ProcessBase):
    __tablename__ = "instrument_capability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    instrument: Mapped["Instrument"] = relationship(back_populates="capabilities")


class InstrumentReading(ProcessBase):
    __tablename__ = "instrument_reading"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), nullable=False)
    port_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instrument_port.id"), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    data_json: Mapped[dict[str, Any]] = mapped_column("data", JSON, default=dict)

    instrument: Mapped["Instrument"] = relationship(back_populates="readings")
    port: Mapped[Optional["InstrumentPort"]] = relationship()


class InstrumentStatus(ProcessBase):
    __tablename__ = "instrument_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    data_json: Mapped[dict[str, Any]] = mapped_column("data", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InstrumentStatusLog(ProcessBase):
    __tablename__ = "instrument_status_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(128))
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[Optional[dict[str, Any]]] = mapped_column("payload", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowTemplate(ProcessBase):
    """Templates for atom/molecule/assembly flows."""

    __tablename__ = "flow_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    flow_type: Mapped[str] = mapped_column(String(32))  # atom | molecule | assembly
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parameters: Mapped[list["FlowParameter"]] = relationship(back_populates="template", cascade="all, delete-orphan")
    runs: Mapped[list["FlowRun"]] = relationship(back_populates="template")


class FlowParameter(ProcessBase):
    __tablename__ = "flow_parameter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("flow_template.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128))
    data_type: Mapped[str] = mapped_column(String(32))
    default_value: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    min_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    allowed_values_json: Mapped[Optional[list[str]]] = mapped_column("allowed_values", JSON, nullable=True)
    units: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    template: Mapped["FlowTemplate"] = relationship(back_populates="parameters")


class FlowRun(ProcessBase):
    __tablename__ = "flow_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("flow_template.id"), nullable=False)
    instrument_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instrument.id"), nullable=True)
    port_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instrument_port.id"), nullable=True)
    run_type: Mapped[str] = mapped_column(String(32), default="atom")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    parameters_json: Mapped[Optional[dict[str, Any]]] = mapped_column("parameters", JSON, nullable=True)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column("result", JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    template: Mapped["FlowTemplate"] = relationship(back_populates="runs")
    instrument: Mapped[Optional["Instrument"]] = relationship()
    port: Mapped[Optional["InstrumentPort"]] = relationship()
    steps: Mapped[list["FlowRunStep"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["FlowRunArtifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class FlowRunStep(ProcessBase):
    __tablename__ = "flow_run_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("flow_run.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    log_json: Mapped[Optional[dict[str, Any]]] = mapped_column("log", JSON, nullable=True)

    run: Mapped["FlowRun"] = relationship(back_populates="steps")


class FlowRunArtifact(ProcessBase):
    __tablename__ = "flow_run_artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("flow_run.id"), nullable=False)
    step_id: Mapped[Optional[int]] = mapped_column(ForeignKey("flow_run_step.id"), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64))
    path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["FlowRun"] = relationship(back_populates="artifacts")
    step: Mapped[Optional["FlowRunStep"]] = relationship()


# ---------------------------------------------------------------------------
# Experiment database — session log and structured agent run traces.
# ---------------------------------------------------------------------------


class ExperimentSession(ExperimentBase):
    """Renamed from chat_history. One row per UI-dispatched agent run."""

    __tablename__ = "experiment_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Link to the structured AgentRun trace (populated after migration is wired up)
    agent_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_run.id"), nullable=True)
    # Legacy UI session string key (websocket session id from server.py)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    final_text: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    raw_log: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    images_json: Mapped[Optional[list[str]]] = mapped_column("images", JSON, default=list)
    summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(32), default="exfoliation")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    agent_run: Mapped[Optional["AgentRun"]] = relationship(back_populates="session_entry", foreign_keys=[agent_run_id])


class ChatSession(ExperimentBase):
    """A UI chat session containing multiple Q&A turns."""

    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_key: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    entries: Mapped[list["ChatHistory"]] = relationship(back_populates="chat_session", cascade="all, delete-orphan")


class ChatHistory(ExperimentBase):
    __tablename__ = "chat_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    chat_session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chat_session.id"), nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    final_text: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    raw_log: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    images_json: Mapped[Optional[list[str]]] = mapped_column("images", JSON, default=list)
    summary: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(32), default="qumus")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chat_session: Mapped[Optional["ChatSession"]] = relationship(back_populates="entries")


class AgentRun(ExperimentBase):
    """One top-level .run(task) invocation on any agent (root or nested sub-agent)."""

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Self-referential FK: NULL for root runs, set for nested sub-agent runs
    parent_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_run.id"), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    task_prompt: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    final_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    total_steps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_log: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    images_json: Mapped[Optional[list[str]]] = mapped_column("images", JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    parent_run: Mapped[Optional["AgentRun"]] = relationship("AgentRun", remote_side="AgentRun.id", back_populates="child_runs")
    child_runs: Mapped[list["AgentRun"]] = relationship("AgentRun", back_populates="parent_run")
    steps: Mapped[list["AgentStep"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["AgentRunArtifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    session_entry: Mapped[Optional["ExperimentSession"]] = relationship(back_populates="agent_run", foreign_keys="ExperimentSession.agent_run_id")


class AgentStep(ExperimentBase):
    """One reasoning loop iteration within an AgentRun."""

    __tablename__ = "agent_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer)
    agent_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    thought: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    step_type: Mapped[str] = mapped_column(String(32), default="tool_call")  # tool_call | final_answer | error
    llm_input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["AgentRun"] = relationship(back_populates="steps")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="step", cascade="all, delete-orphan")
    artifacts: Mapped[list["AgentRunArtifact"]] = relationship(back_populates="step")


class ToolCall(ExperimentBase):
    """One tool invocation within an AgentStep."""

    __tablename__ = "tool_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("agent_step.id"), nullable=False)
    # Denormalized for fast queries without joining through agent_step
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128))
    arguments_json: Mapped[Optional[dict[str, Any]]] = mapped_column("arguments", JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success")  # success | error | timeout
    called_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    step: Mapped["AgentStep"] = relationship(back_populates="tool_calls")
    response: Mapped[Optional["ToolCallResponse"]] = relationship(back_populates="tool_call", cascade="all, delete-orphan")
    artifacts: Mapped[list["AgentRunArtifact"]] = relationship(back_populates="tool_call")


class ToolCallResponse(ExperimentBase):
    """The observation returned by a tool call."""

    __tablename__ = "tool_call_response"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tool_call_id: Mapped[int] = mapped_column(ForeignKey("tool_call.id"), nullable=False, unique=True)
    output_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    output_json: Mapped[Optional[dict[str, Any]]] = mapped_column("output", JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    returned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tool_call: Mapped["ToolCall"] = relationship(back_populates="response")


class AgentRunArtifact(ExperimentBase):
    """Any file or image produced during an agent run (mirrors flow_run_artifact)."""

    __tablename__ = "agent_run_artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id"), nullable=False)
    step_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_step.id"), nullable=True)
    tool_call_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tool_call.id"), nullable=True)
    # e.g. "yolo_overlay", "detection_json", "microscope_image", "log_file"
    artifact_type: Mapped[str] = mapped_column(String(64))
    path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["AgentRun"] = relationship(back_populates="artifacts")
    step: Mapped[Optional["AgentStep"]] = relationship(back_populates="artifacts")
    tool_call: Mapped[Optional["ToolCall"]] = relationship(back_populates="artifacts")


class DeviceAnalysis(MaterialBase):
    """Store device image analysis results including segmentation and components."""

    __tablename__ = "device_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[Optional[int]] = mapped_column(ForeignKey("device.id"), nullable=True)
    chip_id: Mapped[Optional[int]] = mapped_column(ForeignKey("chip.id"), nullable=True)
    original_image_path: Mapped[str] = mapped_column(String(512))
    preview_image_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    electrode_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    components: Mapped[list["DeviceComponent"]] = relationship(back_populates="analysis", cascade="all, delete-orphan")


class DeviceComponent(MaterialBase):
    """Individual segmented components from device image analysis."""

    __tablename__ = "device_component"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("device_analysis.id"), nullable=False)
    component_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    component_type: Mapped[str] = mapped_column(String(64))
    area_pixels: Mapped[int] = mapped_column(Integer)
    centroid_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    centroid_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mask_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    color_bgr: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    stack_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    analysis: Mapped["DeviceAnalysis"] = relationship(back_populates="components")
