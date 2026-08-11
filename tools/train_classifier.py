from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from pathlib import Path
import random
import re

import numpy as np
from PIL import Image
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from torchvision import models, transforms

from camera_agent.config import PROJECT_DIR


CLASSES = ("facebook_active", "facebook_mention", "other")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
TIMESTAMP_RE = re.compile(r"(\d{16,20})$")
SEED = 42


@dataclass(frozen=True)
class Sample:
    path: Path
    label: int
    timestamp_ns: int


class ImageDataset(Dataset):
    def __init__(self, samples: list[Sample], transform) -> None:
        self.samples = samples
        self.transform = transform

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int):
        sample = self.samples[index]
        with Image.open(sample.path) as image:
            tensor = self.transform(image.convert("RGB"))
        return tensor, sample.label


def timestamp_for(path: Path) -> int:
    match = TIMESTAMP_RE.search(path.stem)
    if match:
        return int(match.group(1))
    return path.stat().st_mtime_ns


def group_sessions(samples: list[Sample], gap_seconds: float) -> list[list[Sample]]:
    ordered = sorted(samples, key=lambda sample: sample.timestamp_ns)
    groups: list[list[Sample]] = []
    gap_ns = int(gap_seconds * 1_000_000_000)
    for sample in ordered:
        if not groups or sample.timestamp_ns - groups[-1][-1].timestamp_ns > gap_ns:
            groups.append([sample])
        else:
            groups[-1].append(sample)
    return groups


def split_by_session(samples: list[Sample], validation_ratio: float) -> tuple[list[Sample], list[Sample]]:
    groups = group_sessions(samples, gap_seconds=10.0)
    if len(groups) < 2:
        raise RuntimeError(
            "Each class needs at least two capture sessions separated by >10 seconds "
            "to create a leakage-resistant validation split"
        )
    target = max(1, round(len(samples) * validation_ratio))
    validation_group = min(groups, key=lambda group: abs(len(group) - target))
    validation = list(validation_group)
    training = [sample for group in groups if group is not validation_group for sample in group]
    return training, validation


def evaluate(model, loader, criterion, device, class_count: int):
    model.eval()
    total = correct = 0
    loss_sum = 0.0
    confusion = np.zeros((class_count, class_count), dtype=int)
    with torch.inference_mode():
        for images, labels in loader:
            images, labels = images.to(device), labels.to(device)
            outputs = model(images)
            loss_sum += criterion(outputs, labels).item() * images.size(0)
            predictions = outputs.argmax(dim=1)
            total += labels.size(0)
            correct += predictions.eq(labels).sum().item()
            for truth, prediction in zip(labels.cpu().numpy(), predictions.cpu().numpy()):
                confusion[truth, prediction] += 1
    return loss_sum / max(total, 1), correct / max(total, 1), confusion


def train_phase(model, train_loader, val_loader, criterion, optimizer, device, epochs, name):
    best_accuracy = -1.0
    best_state = copy.deepcopy(model.state_dict())
    for epoch in range(1, epochs + 1):
        model.train()
        total = correct = 0
        loss_sum = 0.0
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            loss_sum += loss.item() * images.size(0)
            total += labels.size(0)
            correct += outputs.argmax(dim=1).eq(labels).sum().item()
        val_loss, val_accuracy, _ = evaluate(
            model, val_loader, criterion, device, len(CLASSES)
        )
        print(
            f"{name} {epoch:02d}/{epochs} "
            f"train_loss={loss_sum / max(total, 1):.4f} "
            f"train_acc={correct / max(total, 1):.3f} "
            f"val_loss={val_loss:.4f} val_acc={val_accuracy:.3f}"
        )
        if val_accuracy > best_accuracy:
            best_accuracy = val_accuracy
            best_state = copy.deepcopy(model.state_dict())
    model.load_state_dict(best_state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the 3-class Facebook classifier.")
    parser.add_argument(
        "--dataset", type=Path, default=PROJECT_DIR / "data" / "facebook" / "cropped"
    )
    parser.add_argument(
        "--output", type=Path, default=PROJECT_DIR / "models" / "facebook_classifier.pt"
    )
    parser.add_argument("--head-epochs", type=int, default=3)
    parser.add_argument("--finetune-epochs", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(args.threads)
    class_to_idx = {name: index for index, name in enumerate(CLASSES)}
    train_samples: list[Sample] = []
    validation_samples: list[Sample] = []
    for class_name in CLASSES:
        directory = args.dataset / class_name
        paths = sorted(
            path
            for path in directory.glob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if len(paths) < 20:
            raise RuntimeError(f"{class_name} needs at least 20 images; found {len(paths)}")
        samples = [Sample(path, class_to_idx[class_name], timestamp_for(path)) for path in paths]
        train_class, validation_class = split_by_session(samples, validation_ratio=0.2)
        train_samples.extend(train_class)
        validation_samples.extend(validation_class)
        print(
            f"{class_name}: train={len(train_class)}, validation={len(validation_class)}, "
            f"sessions={len(group_sessions(samples, 10.0))}"
        )
    random.shuffle(train_samples)

    image_size = 224
    normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )
    train_transform = transforms.Compose(
        [
            transforms.Resize((256, 256)),
            transforms.RandomResizedCrop(image_size, scale=(0.78, 1.0)),
            transforms.RandomPerspective(distortion_scale=0.25, p=0.35),
            transforms.RandomRotation(4),
            transforms.ColorJitter(brightness=0.18, contrast=0.18, saturation=0.08),
            transforms.ToTensor(),
            normalize,
        ]
    )
    validation_transform = transforms.Compose(
        [
            transforms.Resize((256, 256)),
            transforms.CenterCrop(image_size),
            transforms.ToTensor(),
            normalize,
        ]
    )
    train_loader = DataLoader(
        ImageDataset(train_samples, train_transform),
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
    )
    validation_loader = DataLoader(
        ImageDataset(validation_samples, validation_transform),
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}; train={len(train_samples)}; validation={len(validation_samples)}")
    model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    for parameter in model.features.parameters():
        parameter.requires_grad = False
    in_features = model.classifier[3].in_features
    model.classifier[3] = nn.Linear(in_features, len(CLASSES))
    model = model.to(device)
    criterion = nn.CrossEntropyLoss()

    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=1e-3,
        weight_decay=1e-4,
    )
    train_phase(
        model,
        train_loader,
        validation_loader,
        criterion,
        optimizer,
        device,
        args.head_epochs,
        "head",
    )
    for parameter in model.features.parameters():
        parameter.requires_grad = False
    for block in model.features[-4:]:
        for parameter in block.parameters():
            parameter.requires_grad = True
    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=1e-4,
        weight_decay=1e-4,
    )
    train_phase(
        model,
        train_loader,
        validation_loader,
        criterion,
        optimizer,
        device,
        args.finetune_epochs,
        "finetune",
    )

    validation_loss, validation_accuracy, confusion = evaluate(
        model, validation_loader, criterion, device, len(CLASSES)
    )
    print(f"Validation loss={validation_loss:.4f}, accuracy={validation_accuracy:.3f}")
    print("Confusion matrix (rows=true, columns=predicted):")
    print(confusion)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "class_to_idx": class_to_idx,
            "image_size": image_size,
            "validation_accuracy": validation_accuracy,
            "confusion_matrix": confusion.tolist(),
            "split_strategy": "capture sessions separated by >10 seconds",
        },
        args.output,
    )
    print(f"Saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
