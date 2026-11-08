from typing import List, Mapping, Optional

from smolagents import Tool

# Exfoliation flows
from agents.processing_agent.workflows.exfoliation.assembly_flows import AssemblyExfoliation
from agents.processing_agent.workflows.exfoliation.atom_flows import (
    AtomExfoliatorHome,
    AtomExfoliatorPowerOff,
    AtomExfoliatorPowerOn,
    AtomExfoliationFanOff,
    AtomExfoliationFanOn,
    AtomHeaterOff,
    AtomHeaterOn,
    AtomExfoliationHeaterRelayOff,
    AtomExfoliationHeaterRelayOn,
    AtomHeaterSet,
    AtomLeftArmHome,
    AtomLeftArmMassage,
    AtomLeftArmMoveTo,
    AtomLeftArmPlaceRawCarrier,
    AtomLeftArmPlaceSiliconChip,
    AtomLeftArmRotateNose,
    AtomLeftArmStoreRawCarrier,
    AtomLeftArmStoreSiliconChip,
    AtomLeftArmVacuumOff,
    AtomLeftArmVacuumOn,
    AtomReadTemperature,
    AtomTapeAdjust,
    AtomTapeApply,
    AtomTapePeel,
    AtomTapeRelease,
    AtomVacuumOff,
    AtomVacuumOn,
    AtomWait,
    AtomWaitUntilDropTemperature,
    AtomWaitUntilReachTemperature,
)
from agents.processing_agent.workflows.exfoliation.molecule_flows import (
    MoleculeClearStage,
    MoleculeExfoliateChip,
    MoleculePrepareMotherTape,
    MoleculeResetSystem,
)

# Search flows
from agents.processing_agent.workflows.search.assembly_flows import AssemblySearchChip
from agents.processing_agent.workflows.search.atom_flows import (
    AtomCameraCapture,
    AtomCameraStart,
    AtomCameraStop,
    AtomFocusMoveRelative,
    AtomFocusSetZero,
    AtomRightArmHome,
    AtomRightArmMoveTo,
    AtomRightArmPlaceSiliconChipOnTransferStage,
    AtomRightArmStoreSearchSiliconChip,
    AtomRightArmVacuumOff,
    AtomRightArmVacuumOn,
    AtomSampleStageMoveHome,
    AtomSampleStageMoveRelative,
    AtomSampleStageMoveTo,
    AtomSampleStageSetChipDims,
    AtomSampleStageSetDirection,
    AtomSampleStageSetHome,
    AtomSampleStageWaitUntilStop,
    AtomStartObserver,
    AtomStopObserver,
    AtomTransferStageVacuumOff,
    AtomTransferStageVacuumOn,
    AtomTurretRotate,
)
from agents.processing_agent.workflows.search.molecule_flows import (
    MoleculeAutoSearch,
    MoleculeCornerDetection,
    MoleculeFlakeRefindAccurate,
    MoleculeFlakeRefindRough,
    MoleculePickupChipToTransferStage,
    MoleculeStoreSearchSiliconChip,
)

# Stacking flows
from agents.processing_agent.workflows.stacking.assembly_flows import (
    AssemblyDevice,
    AssemblyQuantumHallDevice,
    ResumeProcessingCheckpoint,
)
from agents.processing_agent.workflows.stacking.atom_flows import (
    AtomRightArmPlaceStampOnTransferArm,
    AtomRightArmPickUpItem,
    AtomRightArmPlaceItem,
    AtomTransferArmVacuumOff,
    AtomTransferArmVacuumOn,
    AtomTransferConnect,
    AtomTransferDisconnect,
    AtomTransferGetKSTSpeed,
    AtomTransferGetPositions,
    AtomTransferHomeAll,
    AtomTransferHomeAxis,
    AtomTransferJogAxis,
    AtomTransferMoveAxisBy,
    AtomTransferMoveAxisTo,
    AtomTransferSetKSTSpeed,
    AtomTransferSetPDVMaxSpeed,
    AtomTransferSetPDVSpeed,
    AtomTransferStopPDV,
)
from agents.processing_agent.workflows.stacking.molecule_flows import (
    MoleculeDropDown,
    MoleculeAdjustTouchingPoint,
    MoleculeInitializeStamp,
    MoleculeLoadStamp,
    MoleculePickUp,
    MoleculeRecordDevice,
    MoleculeRecycleStamp,
    MoleculeRemoveStamp,
    MoleculeSetInitialTemp,
)
from common.hardware.defaults import get_default_hardware_ports

DEFAULT_REAL_PORTS = get_default_hardware_ports()

def create_all_workflow_tools(
    hardware_ports: Optional[Mapping[str, str]] = None,
) -> List[Tool]:
    """
    Create all workflow tools (Structure, Molecule, Atom) for the Processing Agent.
    """

    print("🚀 Initializing REAL Hardware Controllers for Processing Agent...")
    from common.hardware import (
        RobotArmController,
        RelayController,
        SharedPrinterController,
        DualMotorController,
        TP3005PController,
        SamplePDVController,
        TurretController,
        FocusController,
        CameraController,
        TransferController,
        TemperatureController,
    )
    port_overrides = DEFAULT_REAL_PORTS.copy()
    if hardware_ports:
        for key, value in hardware_ports.items():
            if value is not None:
                port_overrides[key] = value

    def _build_controller(
        key: str,
        real_cls,
        *,
        default_port=None,
        arg_name: str = "port",
        **kwargs,
    ):
        arg_value = port_overrides.get(key, default_port)
        if arg_value is None:
            raise RuntimeError(f"{key} port is not configured.")
        if arg_name == "index":
            arg_value = int(arg_value)
        try:
            return real_cls(**{arg_name: arg_value}, **kwargs)
        except Exception as exc:
            raise RuntimeError(f"Failed to init {key} on {arg_value}: {exc}") from exc


    left_arm = _build_controller(
        "robot_arm_port_left",
        RobotArmController,
        instrument_key="robot_arm_left",
    )
    right_arm = _build_controller(
        "robot_arm_port_right",
        RobotArmController,
        instrument_key="robot_arm_right",
    )
    exfoliation_vacuum_relay = _build_controller("exfoliation_vacuum_relay_port", RelayController)
    exfoliation_fan_relay = _build_controller("exfoliation_fan_relay_port", RelayController)
    exfoliation_heater_relay = _build_controller("exfoliation_heater_relay_port", RelayController)
    transfer_heater_relay = _build_controller("transfer_heater_relay_port", RelayController)
    transfer_arm_vacuum_relay = _build_controller("transfer_arm_vacuum_relay_port", RelayController)
    sample_stage_vacuum_relay = _build_controller(
        "sample_stage_vacuum_relay_port",
        RelayController,
    )
    printer = _build_controller("printer_port", SharedPrinterController)
    motors = _build_controller("motor_port", DualMotorController)
    power = _build_controller("power_port", TP3005PController)
    temp = TemperatureController(
        exe_path=port_overrides.get("temp_exe_path", r"C:\Users\Qumus_PC\Desktop\DracalView\dracal-usb-get.exe"),
        default_sensor_serial=port_overrides.get("temp_sensor_exfoliation_serial", "E30559"),
        sensor_serial_by_stage={
            "exfoliation": port_overrides.get("temp_sensor_exfoliation_serial", "E30559"),
            "transfer": port_overrides.get("temp_sensor_transfer_serial", "E30753"),
        },
        default_stage="exfoliation",
        default_index="0",
        unit="C",
    )
        
    # New Hardware
    stage = _build_controller(
        "stage_port",
        SamplePDVController,
        magnification=10,
        x_axis="X",
        y_axis="Y",
    )
    turret = _build_controller("turret_port", TurretController)
    focus = _build_controller("focus_port", FocusController)
    camera = _build_controller(
        "microscope_camera_index",
        CameraController,
        arg_name="index",
    )

    transfer = TransferController(
        x_serial=port_overrides.get("transfer_x_serial", "26007120"),
        y_serial=port_overrides.get("transfer_y_serial", "26007081"),
        z_serial=port_overrides.get("transfer_z_serial", "26006985"),
        goni_serial=port_overrides.get("transfer_goni_serial", "27271252"),
        L_com_port=port_overrides.get("transfer_L_port", "COM19"),
        auto_connect=False,
    )

    search_controllers = (right_arm, sample_stage_vacuum_relay, printer, motors, power, stage, turret, focus, camera)
    stacking_controllers = (*search_controllers, transfer)
    
    exfoliation_controllers = (left_arm, exfoliation_vacuum_relay, printer, motors, power)

    exfoliation_kwargs = {
        "temp": temp,
        "fan_relay": exfoliation_fan_relay,
        "exfoliation_heater_relay": exfoliation_heater_relay,
        "transfer_heater_relay": transfer_heater_relay,
    }
    stacking_kwargs = {
        "temp": temp,
        "exfoliation_heater_relay": exfoliation_heater_relay,
        "transfer_heater_relay": transfer_heater_relay,
        "transfer_arm_vacuum_relay": transfer_arm_vacuum_relay,
    }
    # --- Assembly Flows (Level 1) ---
    wf_exfoliation = AssemblyExfoliation(*exfoliation_controllers, **exfoliation_kwargs)
    
    wf_assembly_device = AssemblyDevice(*stacking_controllers, **stacking_kwargs)
    wf_assembly_quantum_hall_device = AssemblyQuantumHallDevice(
        *stacking_controllers,
        **stacking_kwargs,
    )
    wf_resume_processing_checkpoint = ResumeProcessingCheckpoint(
        *stacking_controllers,
        **stacking_kwargs,
    )

    # --- Molecule Flows ---
    molecule_prep = MoleculePrepareMotherTape(*exfoliation_controllers, **exfoliation_kwargs)
    molecule_chip = MoleculeExfoliateChip(*exfoliation_controllers, **exfoliation_kwargs)
    molecule_reset = MoleculeResetSystem(*exfoliation_controllers, **exfoliation_kwargs)
    molecule_clear_stage = MoleculeClearStage(*exfoliation_controllers, **exfoliation_kwargs)
    
    # --- Search Molecule Flows ---
    molecule_corner = MoleculeCornerDetection(*search_controllers)
    molecule_flake_refind_accurate = MoleculeFlakeRefindAccurate(*search_controllers)
    molecule_flake_refind_rough = MoleculeFlakeRefindRough(*search_controllers)
    molecule_place_chip = MoleculePickupChipToTransferStage(*search_controllers)
    molecule_autosearch = MoleculeAutoSearch(*search_controllers)
    molecule_store_search_chip = MoleculeStoreSearchSiliconChip(*search_controllers)

    # --- Stacking Molecule Flows ---
    molecule_drop_down = MoleculeDropDown(*stacking_controllers, **stacking_kwargs)
    molecule_adjust_touching_point = MoleculeAdjustTouchingPoint(*stacking_controllers, **stacking_kwargs)
    molecule_initialize_stamp = MoleculeInitializeStamp(*stacking_controllers, **stacking_kwargs)
    molecule_load_stamp = MoleculeLoadStamp(*stacking_controllers, **stacking_kwargs)
    molecule_pick_up = MoleculePickUp(*stacking_controllers, **stacking_kwargs)
    molecule_record_device = MoleculeRecordDevice(*stacking_controllers, **stacking_kwargs)
    molecule_recycle_stamp = MoleculeRecycleStamp(*stacking_controllers, **stacking_kwargs)
    molecule_remove_stamp = MoleculeRemoveStamp(*stacking_controllers, **stacking_kwargs)
    molecule_set_initial_temp = MoleculeSetInitialTemp(*stacking_controllers, **stacking_kwargs)
    
    # --- Search Assembly Flows ---
    wf_search_chip = AssemblySearchChip(*search_controllers)

    # --- Atom Flows ---
    # Arm
    atom_left_arm_home = AtomLeftArmHome(*exfoliation_controllers)
    atom_left_arm_place_chip = AtomLeftArmPlaceSiliconChip(*exfoliation_controllers)
    atom_left_arm_place_carrier = AtomLeftArmPlaceRawCarrier(*exfoliation_controllers)
    atom_left_arm_store_chip = AtomLeftArmStoreSiliconChip(*exfoliation_controllers)
    atom_left_arm_store_carrier = AtomLeftArmStoreRawCarrier(*exfoliation_controllers)
    atom_left_arm_massage = AtomLeftArmMassage(*exfoliation_controllers)
    atom_left_arm_rotate = AtomLeftArmRotateNose(*exfoliation_controllers)
    atom_left_arm_move_to = AtomLeftArmMoveTo(*exfoliation_controllers)
    atom_left_arm_vac_on = AtomLeftArmVacuumOn(*exfoliation_controllers)
    atom_left_arm_vac_off = AtomLeftArmVacuumOff(*exfoliation_controllers)
    
    # Vacuum
    atom_vac_on = AtomVacuumOn(*exfoliation_controllers)
    atom_vac_off = AtomVacuumOff(*exfoliation_controllers)
    atom_fan_on = AtomExfoliationFanOn(*exfoliation_controllers, **exfoliation_kwargs)
    atom_fan_off = AtomExfoliationFanOff(*exfoliation_controllers, **exfoliation_kwargs)
    atom_transfer_stage_vac_on = AtomTransferStageVacuumOn(*search_controllers)
    atom_transfer_stage_vac_off = AtomTransferStageVacuumOff(*search_controllers)
    atom_transfer_arm_vac_on = AtomTransferArmVacuumOn(*stacking_controllers, **stacking_kwargs)
    atom_transfer_arm_vac_off = AtomTransferArmVacuumOff(*stacking_controllers, **stacking_kwargs)

    # Printer/Tape
    atom_print_on = AtomExfoliatorPowerOn(*exfoliation_controllers)
    atom_print_off = AtomExfoliatorPowerOff(*exfoliation_controllers)
    atom_tape_apply = AtomTapeApply(*exfoliation_controllers)
    atom_tape_peel = AtomTapePeel(*exfoliation_controllers)
    atom_exfoliator_home = AtomExfoliatorHome(*exfoliation_controllers)
    
    # Motors
    atom_tape_release = AtomTapeRelease(*exfoliation_controllers)
    atom_tape_adjust = AtomTapeAdjust(*exfoliation_controllers)

    # Heater
    atom_heat_on = AtomHeaterOn(*exfoliation_controllers, **exfoliation_kwargs)
    atom_heat_off = AtomHeaterOff(*exfoliation_controllers, **exfoliation_kwargs)
    atom_heat_set = AtomHeaterSet(*exfoliation_controllers, **exfoliation_kwargs)
    atom_exfol_heater_relay_on = AtomExfoliationHeaterRelayOn(*exfoliation_controllers, **exfoliation_kwargs)
    atom_exfol_heater_relay_off = AtomExfoliationHeaterRelayOff(*exfoliation_controllers, **exfoliation_kwargs)

    # Temperature
    atom_read_temp = AtomReadTemperature(*exfoliation_controllers, **exfoliation_kwargs)
    atom_wait_until_drop_temp = AtomWaitUntilDropTemperature(*exfoliation_controllers, **exfoliation_kwargs)
    atom_wait_until_reach_temp = AtomWaitUntilReachTemperature(*exfoliation_controllers, **exfoliation_kwargs)

    # Util
    atom_wait = AtomWait(*exfoliation_controllers)
    
    # Search Atoms
    atom_sample_stage_set_dir = AtomSampleStageSetDirection(*search_controllers)
    atom_sample_stage_set_home = AtomSampleStageSetHome(*search_controllers)
    atom_sample_stage_set_dims = AtomSampleStageSetChipDims(*search_controllers)
    atom_sample_stage_move_to = AtomSampleStageMoveTo(*search_controllers)
    atom_sample_stage_move_rel = AtomSampleStageMoveRelative(*search_controllers)
    atom_sample_stage_move_home = AtomSampleStageMoveHome(*search_controllers)
    atom_sample_stage_wait = AtomSampleStageWaitUntilStop(*search_controllers)
    atom_camera_start = AtomCameraStart(*search_controllers)
    atom_camera_stop = AtomCameraStop(*search_controllers)
    atom_camera_capture = AtomCameraCapture(*search_controllers)
    atom_turret_rotate = AtomTurretRotate(*search_controllers)
    atom_focus_zero = AtomFocusSetZero(*search_controllers)
    atom_focus_move = AtomFocusMoveRelative(*search_controllers)
    atom_start_obs = AtomStartObserver(*search_controllers)
    atom_stop_obs = AtomStopObserver(*search_controllers)
    atom_right_arm_home = AtomRightArmHome(*search_controllers)
    atom_right_arm_move = AtomRightArmMoveTo(*search_controllers)
    atom_right_arm_pick_up_item = AtomRightArmPickUpItem(*stacking_controllers, **stacking_kwargs)
    atom_right_arm_place_item = AtomRightArmPlaceItem(*stacking_controllers, **stacking_kwargs)
    atom_right_arm_place_transfer = AtomRightArmPlaceSiliconChipOnTransferStage(*search_controllers)
    atom_right_arm_place_stamp_transfer_arm = AtomRightArmPlaceStampOnTransferArm(*stacking_controllers, **stacking_kwargs)
    atom_right_arm_store_search_chip = AtomRightArmStoreSearchSiliconChip(*search_controllers)
    atom_right_arm_vac_on = AtomRightArmVacuumOn(*search_controllers)
    atom_right_arm_vac_off = AtomRightArmVacuumOff(*search_controllers)

    # Stacking transfer atoms
    atom_transfer_connect = AtomTransferConnect(*stacking_controllers)
    atom_transfer_disconnect = AtomTransferDisconnect(*stacking_controllers)
    atom_transfer_home_all = AtomTransferHomeAll(*stacking_controllers)
    atom_transfer_home_axis = AtomTransferHomeAxis(*stacking_controllers)
    atom_transfer_move_to = AtomTransferMoveAxisTo(*stacking_controllers)
    atom_transfer_move_by = AtomTransferMoveAxisBy(*stacking_controllers)
    atom_transfer_jog = AtomTransferJogAxis(*stacking_controllers)
    atom_transfer_positions = AtomTransferGetPositions(*stacking_controllers)
    atom_transfer_kst_speed = AtomTransferSetKSTSpeed(*stacking_controllers)
    atom_transfer_kst_speed_get = AtomTransferGetKSTSpeed(*stacking_controllers)
    atom_transfer_pdv_speed = AtomTransferSetPDVSpeed(*stacking_controllers)
    atom_transfer_pdv_max_speed = AtomTransferSetPDVMaxSpeed(*stacking_controllers)
    atom_transfer_pdv_stop = AtomTransferStopPDV(*stacking_controllers)

    structures = [
        wf_exfoliation,
        wf_search_chip,
        wf_assembly_device,
        wf_assembly_quantum_hall_device,
        wf_resume_processing_checkpoint,
    ]
    molecules = [
        molecule_prep,
        molecule_chip,
        molecule_reset,
        molecule_clear_stage,
        molecule_corner,
        molecule_flake_refind_accurate,
        molecule_flake_refind_rough,
        molecule_place_chip,
        molecule_autosearch,
        molecule_store_search_chip,
        molecule_drop_down,
        molecule_adjust_touching_point,
        molecule_initialize_stamp,
        molecule_load_stamp,
        molecule_pick_up,
        molecule_record_device,
        molecule_recycle_stamp,
        molecule_remove_stamp,
        molecule_set_initial_temp,
    ]
    atoms = [
        atom_left_arm_home,
        atom_left_arm_place_chip,
        atom_left_arm_place_carrier,
        atom_left_arm_store_chip,
        atom_left_arm_store_carrier,
        atom_left_arm_massage,
        atom_left_arm_rotate,
        atom_left_arm_move_to,
        atom_left_arm_vac_on,
        atom_left_arm_vac_off,
        atom_vac_on,
        atom_vac_off,
        atom_fan_on,
        atom_fan_off,
        atom_transfer_stage_vac_on,
        atom_transfer_stage_vac_off,
        atom_transfer_arm_vac_on,
        atom_transfer_arm_vac_off,
        atom_print_on,
        atom_print_off,
        atom_tape_apply,
        atom_tape_peel,
        atom_exfoliator_home,
        atom_tape_release,
        atom_tape_adjust,
        atom_heat_on,
        atom_heat_off,
        atom_heat_set,
        atom_exfol_heater_relay_on,
        atom_exfol_heater_relay_off,
        atom_read_temp,
        atom_wait_until_drop_temp,
        atom_wait_until_reach_temp,
        atom_wait,
        atom_sample_stage_set_dir,
        atom_sample_stage_set_home,
        atom_sample_stage_set_dims,
        atom_sample_stage_move_to,
        atom_sample_stage_move_rel,
        atom_sample_stage_move_home,
        atom_sample_stage_wait,
        atom_camera_start,
        atom_camera_stop,
        atom_camera_capture,
        atom_turret_rotate,
        atom_focus_zero,
        atom_focus_move,
        atom_start_obs,
        atom_stop_obs,
        atom_right_arm_home,
        atom_right_arm_move,
        atom_right_arm_pick_up_item,
        atom_right_arm_place_item,
        atom_right_arm_place_stamp_transfer_arm,
        atom_right_arm_place_transfer,
        atom_right_arm_store_search_chip,
        atom_right_arm_vac_on,
        atom_right_arm_vac_off,
        atom_transfer_connect,
        atom_transfer_disconnect,
        atom_transfer_home_all,
        atom_transfer_home_axis,
        atom_transfer_move_to,
        atom_transfer_move_by,
        atom_transfer_jog,
        atom_transfer_positions,
        atom_transfer_kst_speed,
        atom_transfer_kst_speed_get,
        atom_transfer_pdv_speed,
        atom_transfer_pdv_max_speed,
        atom_transfer_pdv_stop,
    ]
    return [*structures, *molecules, *atoms]
