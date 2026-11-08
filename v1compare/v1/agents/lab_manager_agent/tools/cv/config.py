from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class LiveYOLOConfig:
    """Runtime configuration for the live YOLO capture pipeline."""

    root: Path = field(default_factory=lambda: Path("agents") / "lab_manager_agent" / "runtime")
    camera_view: str = "left"  # "left" or "right"
    model_path: Optional[Path] = None
    camera_index: Optional[int] = None
    frame_width: int = 1920
    frame_height: int = 1080
    conf_threshold: float = 0.5
    window_size: tuple[int, int] = (1920, 1080)
    include_polygons: bool = True
    relabel_split: bool = True
    force_resize: bool = False
    set_camera_resolution: bool = True
    draw_overlays: bool = True
    display_window: bool = False
    log_per_frame: bool = True
    write_latest: bool = True
    enable_polygon_processor: bool = True
    polygon_frame_interval: int = 5
    save_frame_images: bool = True
    save_frame_labels: bool = True
    polygon_overlay_path: Optional[Path] = None
    polygon_csv_path: Optional[Path] = None
    table_qr_csv_path: Optional[Path] = None
    frame_image_dir: Optional[Path] = None
    frame_label_dir: Optional[Path] = None
    per_frame_dir: Optional[Path] = None
    latest_file: Optional[Path] = None
    camera_config_path: Optional[Path] = None
    overlay_frame_path: Optional[Path] = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        
        # Resolve defaults based on camera_view
        is_left = self.camera_view == "left"
        default_model = "left_yolo.pt" if is_left else "right_yolo.pt"
        
        # Keep relabel_split enabled by default for both cameras.
        
        if self.model_path is None:
            self.model_path = self.root / default_model
        else:
            self.model_path = Path(self.model_path)
        
        if self.camera_index is None:
            import os
            # Default: Left=0, Right=2
            env_var = "YOLO_CAM_LEFT_INDEX" if is_left else "YOLO_CAM_RIGHT_INDEX"
            default_idx = 0 if is_left else 2
            self.camera_index = int(os.environ.get(env_var, default_idx))

        def resolve_path(value: Optional[Path], default_name: str | None = None) -> Path | None:
            if value is not None:
                path = Path(value)
                return path if path.is_absolute() else (self.root / path)
            if default_name is None:
                return None
            return self.root / default_name

        self.polygon_overlay_path = resolve_path(
            self.polygon_overlay_path,
            Path("yolo_captures") / "processed_overlays" / f"{self.camera_view}_latest_processed_overlay.jpg",
        )
        self.polygon_csv_path = resolve_path(self.polygon_csv_path, Path("yolo_captures") / "slides_output.csv")
        self.table_qr_csv_path = resolve_path(
            self.table_qr_csv_path,
            Path("yolo_captures") / "table_qr_output.csv",
        )
        self.frame_image_dir = resolve_path(self.frame_image_dir, Path("yolo_captures") / "frame_images")
        self.frame_label_dir = resolve_path(self.frame_label_dir, Path("yolo_captures") / "frame_labels")
        self.per_frame_dir = resolve_path(self.per_frame_dir, Path("yolo_captures") / "frame_detections")
        # Keep side-specific latest detections to avoid left/right overwrites.
        self.latest_file = resolve_path(
            self.latest_file,
            f"latest_yolo_detection_{self.camera_view}.json",
        )
        self.camera_config_path = resolve_path(self.camera_config_path, "camera_config.json")
        self.overlay_frame_path = resolve_path(
            self.overlay_frame_path,
            Path("yolo_captures") / "overlay_frames" / f"{self.camera_view}_latest_overlay.jpg",
        )

    @property
    def window_name(self) -> str:
        return "YOLO Live Detection"
