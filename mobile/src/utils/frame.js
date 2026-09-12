/**
 * Turning a phone camera shot into a frame the server can actually judge.
 *
 * ⚠️ **The server centre-crops every upload to 16:9 and the phone shoots
 * portrait.** `infra/uploads.py::canonicalise()` trims a frame to
 * CANONICAL_FRAME_WIDTH x CANONICAL_FRAME_HEIGHT (1920x1080) because every
 * threshold in `vision/pose.py` was tuned at that size. For a 1080x1440
 * portrait photo that keeps rows 416-1024 - 42% of the height, centred - and a
 * selfie puts the head *above* that band. Measured with a real enrolment face
 * composited into a portrait frame: detected in the photo the phone sent, gone
 * from the frame the route saw, for any head centred above ~40% of the frame.
 * The route then answered "No face detected." and never reached recognition at
 * all, which is why an unrecognised face was never reported as one.
 *
 * So the crop happens **here**, where the preview is, instead of on the server
 * where nobody can see it. `landscapeWindow()` is exactly the window a
 * `CameraView` fills in a 16:9 box, so what the student frames is what the
 * server judges. Both camera screens must keep that box shape; a square or
 * full-height preview reintroduces the bug silently.
 */

import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';

/** The server's canonical frame shape - vision/pose.py. */
export const FRAME_ASPECT = 16 / 9;

/** vision/pose.py CANONICAL_FRAME_WIDTH. Nothing is gained by sending more. */
const MAX_UPLOAD_WIDTH = 1920;

/**
 * The 16:9 rectangle a cover-fitted preview shows, in source pixels.
 *
 * Deliberately *not* upscaled when it comes out narrower than the server's
 * MIN_NATIVE_FRAME_WIDTH (960): a low-resolution camera should meet that
 * guard and get its own message, rather than be padded up into the blur gate
 * and told the picture is blurry.
 */
export function landscapeWindow(width, height) {
  if (width / height > FRAME_ASPECT) {
    // Already wider than 16:9 - trim the sides, as the server would.
    const cropWidth = Math.round(height * FRAME_ASPECT);
    return {
      originX: Math.round((width - cropWidth) / 2),
      originY: 0,
      width: cropWidth,
      height,
    };
  }

  const cropHeight = Math.round(width / FRAME_ASPECT);
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
export async function captureLandscapeFrame(camera) {
  const photo = await camera.takePictureAsync({ quality: 0.9, exif: false });

  const crop = landscapeWindow(photo.width, photo.height);
  const context = ImageManipulator.manipulate(photo.uri).crop(crop);

  if (crop.width > MAX_UPLOAD_WIDTH) {
    context.resize({ width: MAX_UPLOAD_WIDTH });
  }

  const image = await context.renderAsync();

  // JPEG because infra/uploads.py accepts nothing else, and checks the magic
  // bytes rather than trusting the declared type.
  return image.saveAsync({ format: SaveFormat.JPEG, compress: 0.8 });
}

/** The multipart body both frame routes expect. */
export function frameFormData(photo) {
  const formData = new FormData();

  formData.append('frame', {
    uri: photo.uri,
    name: 'frame.jpg',
    type: 'image/jpeg',
  });

  return formData;
}
