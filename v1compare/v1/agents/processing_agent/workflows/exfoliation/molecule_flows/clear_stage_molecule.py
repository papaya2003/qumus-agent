from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.exfoliation.atom_flows import (
    AtomLeftArmMoveTo, AtomVacuumOn, AtomVacuumOff, AtomLeftArmStoreSiliconChip, AtomLeftArmStoreRawCarrier, AtomLeftArmHome
)
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool
from common.services import ChipInventoryService, RawMaterialCarrierService
from common.services.instrument_status_service import get_instrument_status_service
from common.services.chip_inventory_service import DEFAULT_CHIP_SLOT_ORIGIN
from common.logic.exfoliation_logic import find_exfoliation_obstructions, get_exfoliation_stage_bounds

class MoleculeClearStage(BaseWorkflow):
    name = "molecule_clear_stage"
    description = "Clear the exfoliation stage (center area) of any chips or carriers before starting."
    inputs = {}
    output_type = "string"
    
    DEFAULT_LEFT_TABLE_DOCK = (-260.0, 120.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chip_inventory = ChipInventoryService()
        self.carrier_service = RawMaterialCarrierService()
        self.status_service = get_instrument_status_service()
        
        # Tools
        self.atom_move = AtomLeftArmMoveTo(*args, **kwargs)
        self.atom_vacuum_on = AtomVacuumOn(*args, **kwargs)
        self.atom_vacuum_off = AtomVacuumOff(*args, **kwargs)
        self.atom_store_chip = AtomLeftArmStoreSiliconChip(*args, **kwargs)
        self.atom_store_carrier = AtomLeftArmStoreRawCarrier(*args, **kwargs)
        self.atom_home = AtomLeftArmHome(*args, **kwargs)
        
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_coordinate(value: str | None) -> tuple[float, float] | None:
        if not value:
            return None
        normalized = value.replace(",", ":")
        parts = [p.strip() for p in normalized.split(":") if p.strip()]
        if len(parts) < 2:
            return None
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return None

    def _is_in_bounds(self, x: float | None, y: float | None, bounds: dict) -> bool:
        if x is None or y is None:
            return False
        return (
            bounds["min_x"] <= x <= bounds["max_x"]
            and bounds["min_y"] <= y <= bounds["max_y"]
        )

    def _carrier_positions(self, carrier):
        if carrier.pickup_center_x_mm is not None and carrier.pickup_center_y_mm is not None:
            yield carrier.pickup_center_x_mm, carrier.pickup_center_y_mm
        parsed = self._parse_coordinate(getattr(carrier, "table_coordinate", None))
        if parsed is not None:
            yield parsed
        if carrier.qr_center_x_mm is not None and carrier.qr_center_y_mm is not None:
            yield carrier.qr_center_x_mm, carrier.qr_center_y_mm

    def _resolve_carrier_destination(self, carrier, candidates, bounds) -> tuple[float, float]:
        """
        Determine where to store the carrier.
        Rule 1: Search history for this QR code in the 'Left Table' zone (X < -100).
        Rule 2: Fallback to Default Left Dock.
        """
        qr_message = getattr(carrier, "qr_message", None)
        if qr_message:
            # Sort by ID descending (newest first) to find most recent valid position
            sorted_candidates = sorted(candidates, key=lambda c: c.id, reverse=True)
            
            for other in sorted_candidates:
                # Must match QR
                if getattr(other, "qr_message", None) != qr_message:
                    continue
                
                # Check its known positions
                for pos in self._carrier_positions(other):
                    # STRICT RULE: Must be on Left Table (X < -100)
                    if pos[0] < -100.0:
                         return pos

        # Fallback
        return self.DEFAULT_LEFT_TABLE_DOCK

    def forward(self) -> str:
        results = []
        
        # 1. Use Shared Logic to Find Obstructions
        # This uses the standard padding (default 10.0mm) and fallback logic
        chips_on_stage, carriers_on_stage, provenance = find_exfoliation_obstructions(padding_mm=10.0)
        
        # We also need 'bounds' for _resolve_carrier_destination later
        # Re-calculate or retrieve bounds using same logic
        bounds, _, _ = get_exfoliation_stage_bounds(padding_mm=10.0)
        
        # Filter logic (same as CheckStageObstructionTool)
        chips_on_stage = [c for c in chips_on_stage if "exfoliation stage" not in c.label.lower()]

        if not chips_on_stage and not carriers_on_stage:
            return f"✅ Stage Clear (Checked area with 10mm padding). {provenance}"

        results.append(f"⚠️ Found {len(chips_on_stage)} chip(s) and {len(carriers_on_stage)} carrier(s) on stage. {provenance} Clearing...")

        all_carriers = self.carrier_service.list_carriers()

        # 2. Clear Chips -> Right Storage
        for chip in chips_on_stage:
            cx = chip.stage_position_x
            cy = chip.stage_position_y
            
            if cx is None or cy is None:
                results.append(f"⚠️ Skipping chip {chip.label} (ID {chip.id}): missing coordinates.")
                continue

            try:
                slot = self.chip_inventory.find_first_free_slot()
                if slot is None:
                    raise RuntimeError("No free chip slots available.")
                tx = float(slot.x_left_mm if slot.x_left_mm is not None else DEFAULT_CHIP_SLOT_ORIGIN[0])
                ty = float(slot.y_left_mm if slot.y_left_mm is not None else DEFAULT_CHIP_SLOT_ORIGIN[1])
                
                results.append(f"Moving Chip {chip.label} from ({cx:.1f}, {cy:.1f}) to Slot {slot.id} ({tx:.1f}, {ty:.1f})")
                
                self.atom_store_chip.forward(tx, ty)
                
                self.chip_inventory.record_move(
                    chip.label or "cleared_chip",
                    chip_id=chip.id,
                    from_slot_label=None,
                    to_slot_label=slot.label,
                    source="cleared_from_stage",
                    notes="auto-cleared",
                )
                
                self.chip_inventory.promote_chip(
                    chip.id,
                    new_label="exfoliated_chip" if chip.label == "bare_silicon_chip" else chip.label,
                    stage_position_x=None,
                    stage_position_y=None,
                    pickup_left_x_mm=tx,
                    pickup_left_y_mm=ty,
                    table_coordinate_left=f"{int(tx)}:{int(ty)}",
                )
                
            except Exception as e:
                results.append(f"❌ Failed to clear chip {chip.id}: {e}")

        # 3. Clear Carriers -> Left Source
        for carrier in carriers_on_stage:
            cx = carrier.pickup_center_x_mm
            cy = carrier.pickup_center_y_mm
            
            # Determine Destination
            tx, ty = self._resolve_carrier_destination(carrier, all_carriers, bounds)
            
            results.append(f"Moving Carrier {carrier.id} from ({cx:.1f}, {cy:.1f}) to Source ({tx:.1f}, {ty:.1f})")
            
            try:
                self.atom_store_carrier.forward(tx, ty)
            except Exception as e:
                results.append(f"❌ Failed to clear carrier {carrier.id}: {e}")

        self.atom_home.forward()
        return "\n".join(results)
