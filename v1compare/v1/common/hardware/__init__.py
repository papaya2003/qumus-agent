from .robot_arm_controller import RobotArmController
from .relay_controller import RelayController
from .motor_controller import DualMotorController
from .printer_controller import SharedPrinterController
from .power_controller import TP3005PController
from .sample_PDV_controller import SamplePDVController
from .turret_controller import TurretController
from .focus_controller import FocusController
from .camera_controller import CameraController
from .temp_controller import TemperatureController
try:  # optional hardware deps (pythonnet/mono)
    from .transfer_control_controller import TransferController
except Exception:
    TransferController = None
try:  # optional hardware deps (pythonnet/mono)
    from .KDC101_controller import KDC101Controller
    from .KST201_controller import KST201Controller
    from .stamp_PDV_controller import PDVController
except Exception:
    KDC101Controller = None
    KST201Controller = None
    PDVController = None


def __getattr__(name):
    if name in {"RecordingResult", "StampMove", "StampVideoRecorder"}:
        from .stamp_video_recorder import RecordingResult, StampMove, StampVideoRecorder

        values = {
            "RecordingResult": RecordingResult,
            "StampMove": StampMove,
            "StampVideoRecorder": StampVideoRecorder,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
