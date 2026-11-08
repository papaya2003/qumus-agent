from __future__ import annotations

import json
from typing import Tuple
from smolagents import Tool

# Real Hardware
from common.hardware import (
    RobotArmController,
    RelayController,
    DualMotorController,
    SharedPrinterController,
    TP3005PController,
    SamplePDVController,
    TurretController,
    FocusController,
    CameraController,
    TransferController,
    TemperatureController,
)

from agents.processing_agent.tools.status_utils import mirror_true_status
from common.services.instrument_status_service import get_instrument_status_service

def _parse_xy_angle(s: str) -> Tuple[float, float, float]:
    try:
        parts = [p.strip() for p in str(s).split(",")]
        x = float(parts[0]) if len(parts) > 0 else 0.0
        y = float(parts[1]) if len(parts) > 1 else 0.0
        a = float(parts[2]) if len(parts) > 2 else 0.0
        return x, y, a
    except Exception:
        return 0.0, 0.0, 0.0

class BaseWorkflow(Tool):
    """
    Base class for all processing agent workflows (AtomFlow, MoleculeFlow, AssemblyFlow).
    Provides shared access to hardware controllers (real or fake) and status service.
    """
    # Optional explicit UI camera routing metadata.
    # Set per workflow class to "left" or "right" when routing should be deterministic.
    camera_route: str | None = None

    def __init__(
        self,
        robot_arm: RobotArmController,
        vacuum_relay: RelayController,
        printer: SharedPrinterController,
        motors: DualMotorController,
        power: TP3005PController,
        stage: SamplePDVController | None = None,
        turret: TurretController | None = None,
        focus: FocusController | None = None,
        camera: CameraController | None = None,
        transfer: TransferController | None = None,
        temp: TemperatureController | None = None,
        fan_relay: RelayController | None = None,
        exfoliation_heater_relay: RelayController | None = None,
        transfer_heater_relay: RelayController | None = None,
        transfer_arm_vacuum_relay: RelayController | None = None,
    ):
        super().__init__()
        self.arm = robot_arm
        self.vacuum_relay = vacuum_relay
        self.fan_relay = fan_relay
        self.printer = printer
        self.motors = motors
        self.power = power
        self.stage = stage
        self.turret = turret
        self.focus = focus
        self.camera = camera
        self.transfer = transfer
        self.temp = temp
        self.exfoliation_heater_relay = exfoliation_heater_relay
        self.transfer_heater_relay = transfer_heater_relay
        self.transfer_arm_vacuum_relay = transfer_arm_vacuum_relay
        self.status = get_instrument_status_service()

    def _update_stage(self, updates: dict, event: str) -> None:
        self.status.update_instrument(updates, event=event)
        self.status.update_true_instrument(updates, event=f"{event}_true")
        mirror_true_status(self.status)

    def _sync_status_file(self) -> None:
        mirror_true_status(self.status)

    def __call__(self, *args, **kwargs):
        # Auto-detect flow type from class name if not explicitly set
        # Heuristic: Starts with 'Atom' -> atom, 'Molecule' -> molecule, 'Assembly' -> assembly
        flow_type = getattr(self, "flow_type", "tool")
        cls_name = self.__class__.__name__
        
        if flow_type == "tool":
            if cls_name.startswith("Atom"):
                flow_type = "atom"
            elif cls_name.startswith("Molecule"):
                flow_type = "molecule"
            elif cls_name.startswith("Assembly"):
                flow_type = "assembly"
                
        camera_route = getattr(self, "camera_route", None)
        if camera_route not in ("left", "right", "none", "off"):
            camera_route = None
        start_event = {"type": flow_type, "name": self.name, "state": "start"}
        if camera_route:
            start_event["camera_route"] = camera_route
        start_payload = json.dumps(start_event, ensure_ascii=True)
        print(f"[FLOW_STATUS] {start_payload}", flush=True)
        
        try:
            result = super().__call__(*args, **kwargs)
            
            complete_event = {"type": flow_type, "name": self.name, "state": "complete"}
            if camera_route:
                complete_event["camera_route"] = camera_route
            complete_payload = json.dumps(complete_event, ensure_ascii=True)
            print(f"[FLOW_STATUS] {complete_payload}", flush=True)
            
            return result
        except Exception as e:
            error_event = {"type": flow_type, "name": self.name, "state": "error"}
            if camera_route:
                error_event["camera_route"] = camera_route
            error_payload = json.dumps(error_event, ensure_ascii=True)
            print(f"[FLOW_STATUS] {error_payload}", flush=True)
            raise e
