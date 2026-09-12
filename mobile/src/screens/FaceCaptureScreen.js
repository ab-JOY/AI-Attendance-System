/**
 * Face Capture Screen.
 * Uses expo-camera to capture face frames and send them to the API.
 * Matches the web app's enrolment flow (/api/enrol/start -> /api/enrol/frame -> /api/enrol/finish).
 */

import React, { useState, useEffect, useRef } from 'react';
import { View, StyleSheet, Text, Alert } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import client from '../api/client';
import { useAuth } from '../context/AuthContext';
import Button from '../components/Button';
import Header from '../components/Header';
import { captureLandscapeFrame, frameFormData, FRAME_ASPECT } from '../utils/frame';
import { colors, spacing, typography } from '../theme/colors';

// ⚠️ A frame that fails on the phone never reaches the server, so the server
// log cannot explain it. The capture loop used to swallow every such failure
// as a "temporary network error" and retry forever: the screen sat at
// 0 / 100 with no message, and the only trace was /api/enrol/start answering
// 201 with no /api/enrol/frame ever following it. Failures are counted and
// shown now, and the loop stops rather than retrying something that cannot
// succeed.
const MAX_CONSECUTIVE_FAILURES = 5;

export default function FaceCaptureScreen({ navigation, route }) {
  const { studentId, studentName } = route.params;
  const { completeRegistration, getPendingToken } = useAuth();
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef(null);
  const failures = useRef(0);
  
  const [isCapturing, setIsCapturing] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState('');

  // Create an axios config that includes the pending token
  const authHeaders = () => {
    const t = getPendingToken();
    return t ? { headers: { Authorization: `Bearer ${t}` } } : {};
  };
  
  // Clean up capture session if user leaves the screen
  useEffect(() => {
    const unsubscribe = navigation.addListener('beforeRemove', (e) => {
      // If we're capturing and user tries to go back, cancel the capture on the backend
      if (isCapturing) {
        client.post('/api/enrol/cancel', {}, authHeaders()).catch(() => {});
      }
    });
    return unsubscribe;
  }, [navigation, isCapturing]);

  if (!permission) {
    return <View style={styles.container} />;
  }

  if (!permission.granted) {
    return (
      <View style={styles.container}>
        <Header title="Camera Permission" />
        <View style={styles.center}>
          <Text style={styles.text}>We need your permission to show the camera</Text>
          <Button title="Grant Permission" onPress={requestPermission} style={styles.button} />
        </View>
      </View>
    );
  }

  const startCapture = async () => {
    try {
      setIsCapturing(true);
      setError('');
      
      const response = await client.post('/api/enrol/start', {
        student_id: studentId,
        student_name: studentName,
      }, authHeaders());

      if (response.data.success) {
        setProgress(response.data.progress);
        captureFramesLoop();
      } else {
        setError(response.data.message);
        setIsCapturing(false);
      }
    } catch (err) {
      setError('Could not start capture session: ' + (err.response?.data?.message || err.message));
      setIsCapturing(false);
    }
  };

  const captureFramesLoop = async () => {
    if (!cameraRef.current) {
      // The camera view may not have mounted yet. Wait for it rather than
      // returning into silence, which looked exactly like a frozen capture.
      setTimeout(captureFramesLoop, 300);
      return;
    }

    try {
      // Capture a frame, cropped to the 16:9 window the preview showed. The
      // server crops to 16:9 itself, and a portrait shot loses the head in
      // that crop - see ../utils/frame.js. The retry loop below hid this here
      // as a slow capture rather than as an error.
      const photo = await captureLandscapeFrame(cameraRef.current);
      const formData = frameFormData(photo);

      const config = {
        ...authHeaders(),
        headers: {
          ...(authHeaders().headers || {}),
          'Content-Type': 'multipart/form-data',
        },
      };

      const response = await client.post('/api/enrol/frame', formData, config);

      if (response.data.success) {
        failures.current = 0;
        setProgress(response.data.progress);

        if (response.data.progress?.done) {
          finishCapture();
        } else {
          // Keep capturing
          setTimeout(captureFramesLoop, 200); // 5 frames a second
        }
      } else {
        // A refusal the server considered - "no face", "too close". The
        // capture is working; this frame was not usable. Not a failure.
        failures.current = 0;
        setError(response.data.message);
        setTimeout(captureFramesLoop, 500); // retry on failure (e.g. no face)
      }
    } catch (err) {
      // Either the phone could not produce the frame or the request did not
      // complete. Both are invisible to the server, so say which one it was.
      failures.current += 1;

      const reason = err.response?.data?.message || err.message || String(err);

      if (failures.current >= MAX_CONSECUTIVE_FAILURES) {
        setError(
          `Capture stopped after ${failures.current} failed frames: ${reason}`
        );
        setIsCapturing(false);
        client.post('/api/enrol/cancel', {}, authHeaders()).catch(() => {});
        return;
      }

      setError(`Frame ${failures.current} failed: ${reason} - retrying.`);
      setTimeout(captureFramesLoop, 500);
    }
  };

  const finishCapture = async () => {
    try {
      const response = await client.post('/api/enrol/finish', {}, authHeaders());
      if (response.data.success) {
        setIsCapturing(false);
        // Activate the stored token now that face capture is done
        completeRegistration();
        Alert.alert('Success', 'Face capture complete! Model is retraining.', [
          { text: 'OK' }
        ]);
      } else {
        setError(response.data.message);
      }
    } catch (err) {
      setError('Failed to finish enrolment.');
    }
  };

  return (
    <View style={styles.container}>
      <Header title="Capture Face" onBack={() => {
        if (isCapturing) {
          Alert.alert('Cancel Capture', 'Are you sure you want to cancel?', [
            { text: 'No' },
            { text: 'Yes', onPress: () => navigation.goBack() }
          ]);
        } else {
          navigation.goBack();
        }
      }} />
      
      <View style={styles.cameraStage}>
        <View style={styles.cameraContainer}>
          <CameraView style={StyleSheet.absoluteFill} facing="front" ref={cameraRef} />

          <View style={styles.overlay}>
            <View style={styles.frameOutline} />
          </View>
        </View>
        <Text style={styles.framingHint}>
          Only what you can see here is sent - keep your whole face inside the oval.
        </Text>
      </View>

      <View style={styles.controls}>
        {error ? <Text style={styles.errorText}>{error}</Text> : null}
        
        {isCapturing ? (
          <View style={styles.progressContainer}>
            <Text style={styles.progressText}>
              Captured: {progress?.captured || 0} / 100
            </Text>
            {progress?.last_verdict && !progress?.last_verdict?.ok && (
              <Text style={styles.warningText}>
                {progress.last_verdict.reason}
              </Text>
            )}
          </View>
        ) : (
          <Button
            title="Start Capture"
            onPress={startCapture}
            style={styles.button}
          />
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  center: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: spacing.xl,
  },
  text: {
    ...typography.body,
    textAlign: 'center',
    marginBottom: spacing.lg,
  },
  button: {
    width: '100%',
  },
  cameraStage: {
    flex: 1,
    justifyContent: 'center',
  },
  // ⚠️ 16:9, matching the crop in ../utils/frame.js. A CameraView covers its
  // box, so this shape *is* the window that gets uploaded; a full-height
  // preview shows the student a frame the server never receives.
  cameraContainer: {
    width: '100%',
    aspectRatio: FRAME_ASPECT,
    position: 'relative',
    backgroundColor: '#000',
    overflow: 'hidden',
  },
  framingHint: {
    ...typography.caption,
    color: colors.textSecondary,
    textAlign: 'center',
    marginTop: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  overlay: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    zIndex: 10,
  },
  // Sized to sit inside the 16:9 box on a phone-width screen; the old
  // 250x300 oval was taller than the preview is now.
  frameOutline: {
    width: '38%',
    height: '82%',
    borderWidth: 4,
    borderColor: colors.primary,
    borderRadius: 999, // Oval shape
    backgroundColor: 'transparent',
  },
  controls: {
    padding: spacing.lg,
    backgroundColor: colors.surface,
  },
  progressContainer: {
    alignItems: 'center',
    paddingVertical: spacing.md,
  },
  progressText: {
    ...typography.h3,
    color: colors.secondary,
  },
  warningText: {
    ...typography.bodySmall,
    color: colors.warning,
    marginTop: spacing.xs,
  },
  errorText: {
    ...typography.caption,
    color: colors.error,
    textAlign: 'center',
    marginBottom: spacing.md,
  },
});
