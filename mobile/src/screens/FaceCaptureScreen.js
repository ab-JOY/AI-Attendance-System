/**
 * Face Capture Screen.
 * Uses expo-camera to capture face frames and send them to the API.
 * Matches the web app's enrolment flow (/api/enrol/start -> /api/enrol/frame -> /api/enrol/finish).
 */

import React, { useState, useEffect, useRef } from 'react';
import { View, StyleSheet, Text, Alert } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import client from '../api/client';
import Button from '../components/Button';
import Header from '../components/Header';
import { colors, spacing, typography } from '../theme/colors';

export default function FaceCaptureScreen({ navigation, route }) {
  const { studentId, studentName } = route.params;
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef(null);
  
  const [isCapturing, setIsCapturing] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState('');
  
  // Clean up capture session if user leaves
  useEffect(() => {
    return () => {
      if (isCapturing) {
        client.post('/api/enrol/cancel').catch(() => {});
      }
    };
  }, [isCapturing]);

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
      });

      if (response.data.success) {
        setProgress(response.data.progress);
        captureFramesLoop();
      } else {
        setError(response.data.message);
        setIsCapturing(false);
      }
    } catch (err) {
      setError('Could not start capture session.');
      setIsCapturing(false);
    }
  };

  const captureFramesLoop = async () => {
    if (!cameraRef.current) return;

    try {
      // Capture a frame
      const photo = await cameraRef.current.takePictureAsync({
        quality: 0.5,
        base64: true,
      });

      // Send to server
      // Note: the server expects a multipart/form-data with 'frame' or a base64 string depending on API setup.
      // Since our API blueprint defers to the existing web flow, we format it as multipart.
      // However, for simplicity in React Native, we'll send it as base64 in a JSON payload.
      // *Wait, the web backend expects standard form-data with a file upload.*
      
      const formData = new FormData();
      formData.append('frame', {
        uri: photo.uri,
        name: 'frame.jpg',
        type: 'image/jpeg',
      });

      const response = await client.post('/api/enrol/frame', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      if (response.data.success) {
        setProgress(response.data.progress);
        
        if (response.data.progress?.done) {
          finishCapture();
        } else {
          // Keep capturing
          setTimeout(captureFramesLoop, 200); // 5 frames a second
        }
      } else {
        setError(response.data.message);
        setTimeout(captureFramesLoop, 500); // retry on failure (e.g. no face)
      }
    } catch (err) {
      // Ignore temporary network errors during frame loop
      setTimeout(captureFramesLoop, 500);
    }
  };

  const finishCapture = async () => {
    try {
      const response = await client.post('/api/enrol/finish');
      if (response.data.success) {
        setIsCapturing(false);
        Alert.alert('Success', 'Face capture complete! Model is retraining.', [
          { text: 'OK', onPress: () => navigation.replace('Main') }
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
      
      <View style={styles.cameraContainer}>
        <CameraView style={styles.camera} facing="front" ref={cameraRef} />
        
        <View style={styles.overlay}>
          <View style={styles.frameOutline} />
        </View>
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
  cameraContainer: {
    flex: 1,
    position: 'relative',
    backgroundColor: '#000',
  },
  camera: {
    flex: 1,
  },
  overlay: {
    ...StyleSheet.absoluteFillObject,
    justifyContent: 'center',
    alignItems: 'center',
  },
  frameOutline: {
    width: 250,
    height: 300,
    borderWidth: 4,
    borderColor: colors.primary,
    borderRadius: 150, // Oval shape
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
