"""
Landmark-to-pixel conversion and bounding-box arithmetic.

Extracted from the two copies of `get_face_box()` that MA-4 found. The copies
computed the same clamped bounding box and then returned it in two different
shapes - `(x1, y1, x2, y2)` in `recognize_face.py`, `(x, y, width, height)` in
`capture_dataset.py`. One function produces the box; `box_to_xywh()` converts.

`FaceBox` is a NamedTuple so existing tuple unpacking at the call sites keeps
working unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import NamedTuple, Protocol


class Landmark(Protocol):
    """The part of a MediaPipe NormalizedLandmark this package uses."""

    x: float
    y: float


class LandmarkList(Protocol):
    """
    The part of a MediaPipe NormalizedLandmarkList this package uses.

    Typed as a Protocol rather than importing MediaPipe: `vision/` stays
    cheap to import and its tests can pass a plain object with a `.landmark`
    sequence instead of building a real mesh result.
    """

    landmark: Sequence[Landmark]


class FaceBox(NamedTuple):
    """A face bounding box in pixels, clamped to the frame."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


def landmark_pixel(
    face_landmarks: LandmarkList,
    index: int,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float]:
    """One normalised landmark as floating-point pixel coordinates."""
    landmark = face_landmarks.landmark[index]
    return float(landmark.x * frame_width), float(landmark.y * frame_height)


def average_landmark_pixel(
    face_landmarks: LandmarkList,
    indices: Iterable[int],
    frame_width: int,
    frame_height: int,
) -> tuple[float, float]:
    """
    The centroid of several landmarks, in pixels.

    Passing a single index returns that landmark, which is how the enrolment
    profile's outer-corner eye definition is expressed without a second code
    path.
    """
    points = [
        landmark_pixel(face_landmarks, index, frame_width, frame_height)
        for index in indices
    ]

    count = len(points)

    if count == 0:
        raise ValueError("average_landmark_pixel needs at least one index")

    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
    )


def get_face_box(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
) -> FaceBox:
    """
    The axis-aligned bounding box of every mesh landmark, clamped to the frame.

    Clamping to `frame_width - 1` / `frame_height - 1` rather than to the
    dimensions themselves is deliberate and matches both original copies: the
    box is used to slice the frame, and an exclusive bound would be off by one
    at the right and bottom edges.
    """
    landmark_x = []
    landmark_y = []

    for landmark in face_landmarks.landmark:
        landmark_x.append(int(landmark.x * frame_width))
        landmark_y.append(int(landmark.y * frame_height))

    return FaceBox(
        x1=max(min(landmark_x), 0),
        y1=max(min(landmark_y), 0),
        x2=min(max(landmark_x), frame_width - 1),
        y2=min(max(landmark_y), frame_height - 1),
    )


def box_to_xywh(box: FaceBox) -> tuple[int, int, int, int]:
    """`(x1, y1, x2, y2)` as `(x, y, width, height)`."""
    return box.x1, box.y1, box.width, box.height


def box_center(box: FaceBox | tuple[int, int, int, int]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def box_area(box: FaceBox | tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = box
    return max(x2 - x1, 0) * max(y2 - y1, 0)


def box_iou(
    first_box: FaceBox | tuple[int, int, int, int],
    second_box: FaceBox | tuple[int, int, int, int],
) -> float:
    """Intersection over union, used by the tracker to match faces between frames."""
    first_x1, first_y1, first_x2, first_y2 = first_box
    second_x1, second_y1, second_x2, second_y2 = second_box

    intersection_width = max(min(first_x2, second_x2) - max(first_x1, second_x1), 0)
    intersection_height = max(min(first_y2, second_y2) - max(first_y1, second_y1), 0)

    intersection_area = intersection_width * intersection_height

    first_area = max((first_x2 - first_x1) * (first_y2 - first_y1), 1)
    second_area = max((second_x2 - second_x1) * (second_y2 - second_y1), 1)

    union_area = first_area + second_area - intersection_area

    return intersection_area / max(union_area, 1)
