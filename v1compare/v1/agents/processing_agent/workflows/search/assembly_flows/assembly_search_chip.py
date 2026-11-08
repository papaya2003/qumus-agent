import os
import re
from typing import Optional

from agents.processing_agent.workflows.base_workflow import BaseWorkflow
from agents.processing_agent.workflows.search.molecule_flows.molecule_autosearch import MoleculeAutoSearch
from agents.processing_agent.workflows.search.molecule_flows.molecule_corner_detection import (
    MoleculeCornerDetection,
)
from agents.processing_agent.workflows.search.molecule_flows.molecule_pickup_chip_to_transfer_stage import (
    MoleculePickupChipToTransferStage,
)
from agents.processing_agent.workflows.search.molecule_flows.molecule_store_search_silicon_chip import (
    MoleculeStoreSearchSiliconChip,
)
from agents.processing_agent.workflows.search.atom_flows import (
    AtomSampleStageMoveLHome,
    AtomSampleStageMoveToMicroscope,
)
from common.services.chip_inventory_service import ChipInventoryService
from common.material_semantics import normalize_material_code
from agents.qumus_agent.workflow_tools import BroadcastWorkflowPlanTool, BroadcastWorkflowStepTool

class AssemblySearchChip(BaseWorkflow):
    name = "search_chip_assembly"
    flow_type = "assembly"
    camera_route = "right"
    description = (
        "Full Assembly Search Workflow: Stage Home -> Pickup -> Move to Microscope -> "
        "AutoSearch -> Stage Home -> Store."
    )
    inputs = {
        "chip_id": {"type": "integer", "description": "Numeric chip id to pick up.", "nullable": True},
        "slot_label": {"type": "string", "description": "Slot label to pick up from (fallback if chip_id not provided).", "nullable": True},
        "angle": {"type": "number", "description": "Scan angle (default 0).", "nullable": True},
        "material_type": {"type": "string", "description": "Material code for detected flakes (e.g., Gr, hBN). Graphene uses Gr + monolayer; hBN uses thickness bins like 0-10/10-20/20-30/30-40 (not monolayer/bilayer).", "nullable": True},
        "min_area": {"type": "integer", "description": "Minimum area for candidate selection (pixels). Defaults to AutoSearch's magnification-specific values.", "nullable": True},
        # Freedom mode (currently disabled):
        # "chip_dims_inch": {
        #     "type": "array",
        #     "description": "Chip dimensions [long, short] in inches (derived from DB width_mm/height_mm).",
        #     "default": [0.5, 0.5],
        #     "nullable": True,
        # },
    }
    output_type = "string"

    def __init__(self, arm, relay, printer, motors, power, stage, turret, focus, camera):
        super().__init__(arm, relay, printer, motors, power, stage, turret, focus, camera)
        args = (arm, relay, printer, motors, power, stage, turret, focus, camera)
        self.mol_autosearch = MoleculeAutoSearch(*args)
        self.mol_corner_detection = MoleculeCornerDetection(*args)
        self.mol_pickup = MoleculePickupChipToTransferStage(*args)
        self.mol_store = MoleculeStoreSearchSiliconChip(*args)
        self.atom_stage_L_home = AtomSampleStageMoveLHome(*args)
        self.atom_stage_to_microscope = AtomSampleStageMoveToMicroscope(*args)
        self.chip_service = ChipInventoryService()
        self.broadcaster_plan = BroadcastWorkflowPlanTool()
        self.broadcaster_step = BroadcastWorkflowStepTool()

    @staticmethod
    def _parse_saved_corner_path(message: str) -> Optional[str]:
        match = re.search(r"\[CORNER\]\s+BL saved:\s+(.+?)(?:\s+\(score=|\s*$)", message, flags=re.MULTILINE)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _parse_corner_pos(message: str) -> Optional[list[float]]:
        patterns = [
            r"\[FINAL-CORNER-POS\]\s+BL:\s+\(([-+0-9.eE]+),\s*([-+0-9.eE]+)\)",
            r"\[CORNER-POS\]\s+BL:\s+\(([-+0-9.eE]+),\s*([-+0-9.eE]+)\)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                return [float(match.group(1)), float(match.group(2))]
        return None

    def _resolve_chip_id(self, chip_id: Optional[int]) -> int:
        if chip_id is not None:
            resolved = int(chip_id)
            if resolved <= 0:
                raise ValueError("chip_id must be a positive integer.")
            return resolved

        # Auto-select most recent exfoliated chip with right-arm pickup coordinates.
        candidates = list(self.chip_service.list_chips(label="exfoliated_chip"))
        candidates.sort(key=lambda c: (c.last_updated_date is None, c.last_updated_date), reverse=True)
        for chip in candidates:
            if chip.pickup_right_x_mm is None or chip.pickup_right_y_mm is None:
                continue
            return int(chip.id)

        raise ValueError("No available exfoliated chips with pickup coordinates in inventory.")

    @staticmethod
    def _create_search_run_dir() -> tuple[str, str]:
        current = os.getcwd()
        base_dir = os.path.join(current, "images", "autosearch")
        os.makedirs(base_dir, exist_ok=True)
        existing = []
        for name in os.listdir(base_dir):
            match = re.fullmatch(r"run_(\d+)", name)
            if match:
                existing.append(int(match.group(1)))
        next_run = max(existing, default=0) + 1
        run_dir = os.path.join(base_dir, f"run_{next_run}")
        corner_dir = os.path.join(run_dir, "chip_corner")
        os.makedirs(corner_dir, exist_ok=True)
        return run_dir, corner_dir

    # Freedom mode (currently disabled):
    # def _chip_dims_inch_from_db(self, chip_id: int) -> Optional[list[float]]:
    #     """Return [long_edge_inch, short_edge_inch] from DB width_mm/height_mm."""
    #     chip = self.chip_service.get_chip_by_id(int(chip_id))
    #     if chip is None or chip.width_mm is None or chip.height_mm is None:
    #         return None
    #     width_mm = float(chip.width_mm)   # DB width = long side by convention.
    #     height_mm = float(chip.height_mm) # DB height = short side by convention.
    #     if width_mm <= 0 or height_mm <= 0:
    #         return None
    #     mm_per_inch = 25.4
    #     return [width_mm / mm_per_inch, height_mm / mm_per_inch]

    def forward(
        self, 
        chip_id: Optional[int] = None,
        slot_label: Optional[str] = None,
        angle: float = 0, 
        material_type: Optional[str] = None,
        min_area: Optional[int] = None,
        # Freedom mode (currently disabled):
        # chip_dims_inch: Optional[list[float]] = None,  # optional override
    ) -> str:

        try:
            # If chip_id is omitted, resolve the most recent exfoliated chip in-processing-agent logic.
            chip_id = self._resolve_chip_id(chip_id if chip_id else None)
        except Exception as exc:
            return f"❌ {exc}"

        _material_type = material_type
        if not _material_type and chip_id is not None:
            try:
                chip_record = self.chip_service.get_chip_by_id(chip_id)
                if chip_record and chip_record.material_type:
                    _material_type = chip_record.material_type
            except Exception as exc:
                return f"❌ Failed to resolve material type from chip {chip_id}: {exc}"

        material_placeholder = str(_material_type or "").strip().lower()
        if not material_placeholder or material_placeholder in {"unknown", "none", "n/a", "unset"}:
            return (
                "❌ search_chip_assembly requires a material_type for flake segmentation. "
                f"Provide material_type='Gr' for graphene/graphite or material_type='hBN' for hBN, "
                f"or ensure chip {chip_id} has material_type set in the database."
            )
        _material_type = normalize_material_code(_material_type)

        # Freedom mode (currently disabled):
        # chip_dims_inch = self._chip_dims_inch_from_db(chip_id) or [0.5, 0.5]
        # if chip_dims_inch is not None:
        #     try:
        #         chip_dims_inch = [float(chip_dims_inch[0]), float(chip_dims_inch[1])]
        #     except (TypeError, ValueError, IndexError):
        #         chip_dims_inch = self._chip_dims_inch_from_db(chip_id) or [0.5, 0.5]

        log = []
        run_dir, corner_dir = self._create_search_run_dir()

        plan_desc = [
            "Stage Home",
            "Pickup Exfoliated Chip",
            "Stage to Microscope",
            "Corner Detection",
            "Stage to Microscope",
            "Auto Search",
            "Stage Home",
            "Store Exfoliated Chip"
        ]
        self.broadcaster_plan.forward(plan_desc)

        def _run_step(step_idx: int, label: str, fn):
            print(f"[ASSEMBLY_STEP] {step_idx} {label} - start", flush=True)
            self.broadcaster_step.forward(step_idx - 1)
            res = fn()
            print(f"[ASSEMBLY_STEP] {step_idx} {label} - complete", flush=True)
            # Restore parent plan in case a nested workflow overwrote it
            self.broadcaster_plan.forward(plan_desc)
            self.broadcaster_step.forward(step_idx - 1)
            if isinstance(res, str) and res.strip().startswith("❌"):
                raise RuntimeError(res)
            return res

        try:
            log.append(f"Search run directory: {run_dir}")

            # 1. Sample stage home
            res_stage_home = _run_step(1, "Stage L Home", self.atom_stage_L_home.forward)
            log.append(f"Step 1 (Stage Home): {res_stage_home}")

            # 2. Pickup chip to transfer stage
            res_pickup = _run_step(
            2,
            "Pickup",
            lambda: self.mol_pickup.forward(chip_id=chip_id, slot_label=slot_label),
        )
            log.append(f"Step 2 (Pickup): {res_pickup}")

            # 3. Sample stage move to microscope
            res_stage_micro = _run_step(3, "Stage to Microscope", self.atom_stage_to_microscope.forward)
            log.append(f"Step 3 (Stage to Microscope): {res_stage_micro}")

            # 4. Corner detection
            res_corner = _run_step(
                4,
                "Corner Detection",
                lambda: self.mol_corner_detection.forward(
                    save_dir=corner_dir,
                    save_name="chip_corner_bl.jpg",
                ),
            )
            log.append(f"Step 4 (Corner Detection): {res_corner}")

            new_corner_image = self._parse_saved_corner_path(res_corner) or os.path.join(corner_dir, "chip_corner_bl.jpg")
            new_corner_coord = self._parse_corner_pos(res_corner)
            if not os.path.exists(new_corner_image):
                raise RuntimeError(f"Corner detection did not produce the expected image: {new_corner_image}")
            if new_corner_coord is None:
                raise RuntimeError("Failed to parse BL corner coordinates from molecule_corner_detection output.")
            try:
                chip_record = self.chip_service.get_chip_by_id(chip_id)
                if chip_record is None:
                    raise RuntimeError(f"Chip {chip_id} not found in inventory.")
                self.chip_service.upsert_chip(
                    label=chip_record.label,
                    chip_id=chip_id,
                    stage_position_x=chip_record.stage_position_x,
                    stage_position_y=chip_record.stage_position_y,
                    chip_corner_coords=(new_corner_coord[0], new_corner_coord[1]),
                    chip_corner_image_path=new_corner_image,
                    pickup_left_x_mm=chip_record.pickup_left_x_mm,
                    pickup_left_y_mm=chip_record.pickup_left_y_mm,
                    pickup_left_angle_deg=chip_record.pickup_left_angle_deg,
                    pickup_right_x_mm=chip_record.pickup_right_x_mm,
                    pickup_right_y_mm=chip_record.pickup_right_y_mm,
                    pickup_right_angle_deg=chip_record.pickup_right_angle_deg,
                    angle_deg=chip_record.angle_deg,
                    width_mm=chip_record.width_mm,
                    height_mm=chip_record.height_mm,
                    confidence=chip_record.confidence,
                    present=chip_record.present,
                    table_coordinate_left=chip_record.table_coordinate_left,
                    table_coordinate_right=chip_record.table_coordinate_right,
                    metadata=chip_record.metadata,
                )
                log.append(
                    f"💾 Saved chip corner for chip {chip_id}: coord={new_corner_coord}, image={new_corner_image}"
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to save chip corner into database for chip {chip_id}: {exc}") from exc

            # 5. Sample stage move to microscope
            res_stage_micro_2 = _run_step(5, "Stage to Microscope", self.atom_stage_to_microscope.forward)
            log.append(f"Step 5 (Stage to Microscope): {res_stage_micro_2}")

            # 6. AutoSearch
            if material_type:
                log.append(f"ℹ️ Using material type '{_material_type}' from search request.")
            else:
                log.append(f"ℹ️ Auto-resolved material type '{_material_type}' from chip {chip_id}.")

            autosearch_log = _run_step(
                6,
                "AutoSearch",
                lambda: self.mol_autosearch.forward(
                    angle=angle,
                    run_dir=run_dir,
                    chip_id=chip_id,
                    material_type=_material_type,
                    min_area=min_area,
                    # Freedom mode (currently disabled):
                    # chip_dims_inch=chip_dims_inch,
                ),
            )
            log.append(f"Step 6 (Auto Search): {autosearch_log}")

            # 7. Sample stage home
            res_stage_home_2 = _run_step(7, "Stage L Home", self.atom_stage_L_home.forward)
            log.append(f"Step 7 (Stage Home): {res_stage_home_2}")

            # 8. Store chip from transfer/search stage
            res_store = _run_step(
            8,
            "Store",
            lambda: self.mol_store.forward(chip_id=chip_id, slot_label=slot_label),
        )
            log.append(f"Step 8 (Store): {res_store}")

        except Exception as exc:
            log.append(f"❌ Assembly Search failed: {exc}")
            return "\n".join(log)

        return "\n".join(log)
