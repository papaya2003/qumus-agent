"""Reactor job argument shapes the brain validates before sending a job.

Copied from the sim lab's reactor flow parameters (qumus-simlab) so the brain does
not import laboratory code. The station validates again with its own schema
(contract v1 describe().commands); keep these limits in step with it.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChipParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    chip_id: str


class GrowthParams(ChipParams):
    temperature_c: float = Field(820, ge=22, le=950)
    flow_sccm: float = Field(200, ge=0, le=400)
    duration_s: float = Field(60, gt=0, le=900)
    observation_interval_s: float = Field(20, ge=5, le=60)


class ObserveParams(ChipParams):
    autofocus: bool = False


class PositionParams(ChipParams):
    x_mm: float = Field(0, ge=-4, le=4)
    y_mm: float = Field(0, ge=-4, le=4)


class ShutdownParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
