/**
 * Turning a phone camera shot into a frame the server can actually judge.
 *
 * ⚠️ **What the student sees in the preview must be exactly what is sent**
 * (lessons.md L33). A `CameraView` covers its box, so the box's shape decides
 * which part of the photo the student is looking at; `previewWindow()` crops
 * the photo to that same part, here on the phone where it is visible.
 *
 * History, because both shapes have been wrong in turn:
 *
 * 1. The preview was a square while the server centre-cropped to 16:9. For a
 *    1080x1440 portrait photo the crop kept rows 416-1024, and a selfie puts
 *    the head above that band: a perfectly framed face came back
 *    "No face detected." (L33).
 * 2. The fix made the preview a 16:9 strip. Correct, but a phone is held
 *    upright, so a whole face had to fit into a short strip - the student had
 *    to hold the phone at arm's length, which the user reported as too hard.
 * 3. Now: the preview is a **square**, the app sends that square, and
 *    `/api/attendance/frame` **letterboxes** it (`infra/uploads.py::letterbox`)
 *    into the 1280x720 frame the gates were tuned at, instead of cropping it.
 *    The square is 1.78x the strip's height, so the face can be that much
 *    closer. Nothing the student framed is cut away.
 *
 * ⚠️ The preview box and FRAME_ASPECT must stay the same shape, and the
 * server must keep letterboxing this route. Change one without the others and
 * (1) comes back silently.
 */

import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';

/** The shape of the attendance preview and of every frame it sends. */
export const FRAME_ASPECT = 1;

/**
 * Largest square sent: exactly what the server keeps. It fits the square to
 * 720 high (infra/uploads.py `decode_letterboxed()`), so a bigger one is only
 * more bytes to upload and more time per frame, and a smaller one is refused.
 */
export const UPLOAD_WIDTH = 720;

/**
 * The short side asked of the camera. More than UPLOAD_WIDTH as a margin,
 * because a phone can return less than the size it was asked for (one
 * returned a 768 square, 2026-10-24).
 */
const MIN_CAPTURE_SIDE = 960;

/**
 * The smallest picture size the camera offers with a short side of at least
 * MIN_CAPTURE_SIDE, as a `pictureSize` prop value - or undefined to leave the
 * camera's default.
 *
 * ⚠️ **This is most of a frame's time.** `takePictureAsync()` defaults to the
 * sensor's full resolution - 12 MP or more on a current phone - which is then
 * cropped and shrunk to UPLOAD_WIDTH. Capturing at ~1280x960 instead makes each
 * picture a fraction of the work, and nothing the server judges is lost.
 *
 * `sizes` is what `getAvailablePictureSizesAsync()` returns: "WIDTHxHEIGHT"
 * strings on Android and most of iOS, and preset names ("Photo", "High") that
 * are skipped here because their size is not known.
 */
export function smallestUsablePictureSize(sizes) {
  const usable = (sizes || [])
    .map((size) => {
      const match = /^(\d+)x(\d+)$/.exec(size);
      return match ? { size, width: Number(match[1]), height: Number(match[2]) } : null;
    })
    .filter((entry) => entry && Math.min(entry.width, entry.height) >= MIN_CAPTURE_SIDE)
    .sort((a, b) => a.width * a.height - b.width * b.height);

  return usable.length ? usable[0].size : undefined;
}

/**
 * The `aspect`-shaped rectangle a cover-fitted preview shows, in source pixels.
 *
 * Deliberately *not* upscaled when the source is small: a low-resolution camera
 * should meet the server's width guard and get its own message, rather than be
 * padded up into the blur gate and told the picture is blurry.
 */
export function previewWindow(width, height, aspect = FRAME_ASPECT) {
  if (width / height > aspect) {
    // Wider than the box - trim the sides.
    const cropWidth = Math.round(height * aspect);
    return {
      originX: Math.round((width - cropWidth) / 2),
      originY: 0,
      width: cropWidth,
      height,
    };
  }

  // Taller than the box - trim top and bottom.
  const cropHeight = Math.round(width / aspect);
  return {
    originX: 0,
    originY: Math.round((height - cropHeight) / 2),
    width,
    height: cropHeight,
  };
}

/**
 * Take one picture and return it cropped to the window the preview showed.
 *
 * `skipProcessing` is left at its default so the photo arrives upright and
 * `photo.width`/`photo.height` describe what the student saw - the crop
 * arithmetic below is wrong if the frame is still in sensor orientation.
 */
export async function captureFrame(camera) {
  // No shutter sound: attendance takes pictures back to back.
  // quality 0.7: the photo is re-encoded below anyway, and a lighter capture
  // is a faster one.
  //
  // `pictureRef`: the photo stays in memory and goes straight to the cropper,
  // instead of being written to a file and read back (2026-10-24: the phone
  // delivered 0.5 frames/s, and the camera's own time is part of that). The
  // orientation processing still runs - `skipProcessing` is faster again but
  // can leave the picture rotated on some phones, and a sideways face is
  // never detected.
  const picture = await camera.takePictureAsync({
    quality: 0.7,
    exif: false,
    shutterSound: false,
    pictureRef: true,
  });

  try {
    const crop = previewWindow(picture.width, picture.height);
    // A phone that ignores `pictureRef` returns an ordinary photo with a uri.
    const context = ImageManipulator.manipulate(picture.uri || picture).crop(crop);

    if (crop.width > UPLOAD_WIDTH) {
      context.resize({ width: UPLOAD_WIDTH });
    }

    const image = await context.renderAsync();

    // JPEG because infra/uploads.py accepts nothing else, and checks the magic
    // bytes rather than trusting the declared type.
    //
    // 0.6: 30 KB a frame at 720, half of the 60 KB a 960 square at 0.8 was.
    // Measured on the real-face phone frames (2026-10-24): 960/0.8, 720/0.8,
    // 720/0.6 and 720/0.5 were all recorded after the same 48 frames - the
    // same quality the browser camera was proven at (lessons.md L39).
    return await image.saveAsync({ format: SaveFormat.JPEG, compress: 0.6 });
  } finally {
    // Native memory, not garbage collected promptly: a picture every few
    // hundred milliseconds would pile up.
    if (!picture.uri && typeof picture.release === 'function') picture.release();
  }
}

/**
 * Frames sent in one request at most - web/api.py MAX_FRAMES_PER_REQUEST,
 * which ignores any more.
 */
export const MAX_FRAMES_PER_REQUEST = 4;

/**
 * The multipart body /api/attendance/frame expects: one `frame` part per
 * photo, oldest first - the server judges them in this order.
 */
export function frameFormData(photos) {
  const formData = new FormData();

  [].concat(photos).forEach((photo, index) => {
    formData.append('frame', {
      uri: photo.uri,
      name: `frame${index}.jpg`,
      type: 'image/jpeg',
    });
  });

  return formData;
}
