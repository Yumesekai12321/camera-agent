from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class Detection:
    box: tuple[int, int, int, int]
    confidence: float
    area: int


@dataclass(frozen=True)
class ScreenExtraction:
    computer_detected: bool
    screen: np.ndarray | None
    detection: Detection | None
    used_static_roi: bool = False


class ComputerScreenDetector:
    def __init__(
        self,
        model_path: Path,
        *,
        confidence: float = 0.35,
        image_size: int = 416,
        normalized_roi: tuple[float, float, float, float] | None = None,
        shared_model: Any | None = None,
        prediction_lock: Any | None = None,
    ) -> None:
        if not model_path.is_file():
            raise FileNotFoundError(f"Computer detector not found: {model_path}")
        self.confidence = confidence
        self.image_size = image_size
        self.normalized_roi = normalized_roi
        self.model = shared_model
        self._prediction_lock = prediction_lock or threading.Lock()
        if normalized_roi is None:
            if self.model is None:
                from ultralytics import YOLO

                self.model = YOLO(str(model_path))

    def _from_roi(self, frame: np.ndarray) -> ScreenExtraction:
        assert self.normalized_roi is not None
        x, y, width, height = self.normalized_roi
        frame_height, frame_width = frame.shape[:2]
        x1 = max(0, min(frame_width - 1, round(x * frame_width)))
        y1 = max(0, min(frame_height - 1, round(y * frame_height)))
        x2 = max(x1 + 1, min(frame_width, round((x + width) * frame_width)))
        y2 = max(y1 + 1, min(frame_height, round((y + height) * frame_height)))
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return ScreenExtraction(False, None, None, True)
        detection = Detection((x1, y1, x2, y2), 1.0, (x2 - x1) * (y2 - y1))
        return ScreenExtraction(True, crop.copy(), detection, True)

    def extract(self, frame: np.ndarray) -> ScreenExtraction:
        if frame is None or frame.size == 0:
            return ScreenExtraction(False, None, None)
        if self.normalized_roi is not None:
            return self._from_roi(frame)

        assert self.model is not None
        with self._prediction_lock:
            result = self.model.predict(
                source=frame,
                conf=self.confidence,
                imgsz=self.image_size,
                verbose=False,
            )[0]
        detections: list[Detection] = []
        frame_area = frame.shape[0] * frame.shape[1]
        for box in result.boxes:
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().tolist())
            width = max(0, x2 - x1)
            height = max(0, y2 - y1)
            area = width * height
            apparent_ratio = width / max(height, 1)
            if area < frame_area * 0.01 or not 0.40 <= apparent_ratio <= 4.0:
                continue
            detections.append(Detection((x1, y1, x2, y2), confidence, area))
        if not detections:
            return ScreenExtraction(False, None, None)

        best = max(detections, key=lambda item: item.confidence * np.sqrt(max(item.area, 1)))
        frame_height, frame_width = frame.shape[:2]
        x1, y1, x2, y2 = best.box
        pad_x = int((x2 - x1) * 0.02)
        pad_y = int((y2 - y1) * 0.02)
        x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        x2, y2 = min(frame_width, x2 + pad_x), min(frame_height, y2 + pad_y)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return ScreenExtraction(True, None, best)
        return ScreenExtraction(True, crop.copy(), best)


class FacebookClassifier:
    def __init__(
        self,
        model_path: Path,
        *,
        torch_threads: int = 2,
        prediction_lock: Any | None = None,
    ) -> None:
        if not model_path.is_file():
            raise FileNotFoundError(f"Facebook classifier not found: {model_path}")

        import torch
        import torch.nn as nn
        from torchvision import models, transforms

        try:
            torch.set_num_threads(torch_threads)
            torch.set_num_interop_threads(1)
        except RuntimeError:
            # PyTorch only permits setting interop threads before parallel work starts.
            pass

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        checkpoint: dict[str, Any] = torch.load(
            model_path,
            map_location=self.device,
            weights_only=True,
        )
        self.class_to_idx = dict(checkpoint["class_to_idx"])
        self.idx_to_class = {value: key for key, value in self.class_to_idx.items()}
        self.image_size = int(checkpoint.get("image_size", 224))
        self.model_version = str(checkpoint.get("model_version") or model_path.name)
        self._prediction_lock = prediction_lock or threading.Lock()

        self.model = models.mobilenet_v3_small(weights=None)
        in_features = self.model.classifier[3].in_features
        self.model.classifier[3] = nn.Linear(in_features, len(self.class_to_idx))
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model = self.model.to(self.device)
        self.model.eval()

        self.transform = transforms.Compose(
            [
                transforms.Resize((256, 256)),
                transforms.CenterCrop(self.image_size),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                ),
            ]
        )

    @property
    def degraded(self) -> bool:
        return "other" not in self.class_to_idx

    def predict(self, image_bgr: np.ndarray) -> dict[str, float]:
        from PIL import Image

        if image_bgr is None or image_bgr.size == 0:
            raise ValueError("Classifier input is empty")
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        tensor = self.transform(Image.fromarray(image_rgb)).unsqueeze(0).to(self.device)
        with self._prediction_lock, self.torch.inference_mode():
            logits = self.model(tensor)
            probabilities = self.torch.softmax(logits, dim=1)[0]
        return {
            self.idx_to_class[index]: float(probability)
            for index, probability in enumerate(probabilities)
        }
