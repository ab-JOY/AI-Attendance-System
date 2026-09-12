/**
 * Attendance Monitoring Screen.
 * Shows live session status and allows students to join the session.
 */

import React, { useState, useEffect, useRef } from 'react';
import { View, StyleSheet, Text, ScrollView, RefreshControl, Alert } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { Ionicons } from '@expo/vector-icons';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Button from '../components/Button';
import Header from '../components/Header';
import Card from '../components/Card';
import { captureLandscapeFrame, frameFormData, FRAME_ASPECT } from '../utils/frame';
import { colors, spacing, typography, borderRadius } from '../theme/colors';

export default function AttendanceMonitoringScreen() {
  const { user } = useAuth();
  const [liveData, setLiveData] = useState(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isCameraOpen, setIsCameraOpen] = useState(false);
  const [isVerifying, setIsVerifying] = useState(false);
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef(null);

  const fetchLiveData = async () => {
    try {
      const response = await client.get('/api/attendance/live');
      if (response.data.success) {
        setLiveData(response.data);
      }
    } catch (err) {
      console.log('Error fetching live data:', err);
    }
  };

  useEffect(() => {
    fetchLiveData();
    const interval = setInterval(fetchLiveData, 5000); // poll every 5s
    return () => clearInterval(interval);
  }, []);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await fetchLiveData();
    setIsRefreshing(false);
  };

  const handleJoinSession = async () => {
    if (!isCameraOpen) {
      if (!permission?.granted) {
        const result = await requestPermission();
        if (!result.granted) return;
      }
      setIsCameraOpen(true);
      return;
    }

    if (!cameraRef.current) return;

    try {
      setIsVerifying(true);

      // Cropped to the 16:9 window the preview showed, because the server
      // crops to 16:9 itself and a portrait shot loses the head - see
      // ../utils/frame.js.
      const photo = await captureLandscapeFrame(cameraRef.current);
      const formData = frameFormData(photo);

      const response = await client.post('/api/attendance/frame', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      if (response.data.success) {
        if (response.data.done) {
          Alert.alert('Verified!', response.data.message);
          setIsCameraOpen(false);
          fetchLiveData();
        } else {
          Alert.alert('Try again', response.data.message);
        }
      } else {
        Alert.alert('Error', response.data.message);
      }
    } catch (err) {
      Alert.alert('Error', 'Verification failed: ' + (err.response?.data?.message || err.message));
    } finally {
      setIsVerifying(false);
    }
  };

  if (!liveData?.running) {
    return (
      <View style={styles.container}>
        <Header title="Attendance Monitoring" />
        <ScrollView
          contentContainerStyle={styles.center}
          refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
        >
          <Ionicons name="videocam-off-outline" size={64} color={colors.textSecondary} />
          <Text style={styles.noSessionTitle}>No Live Session</Text>
          <Text style={styles.noSessionSubtitle}>
            There is no active attendance session running right now.
          </Text>
        </ScrollView>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Header title="Attendance Monitoring" />
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
      >
        <Text style={styles.sectionTitle}>Live Attendance Session</Text>
        <Text style={styles.subjectCode}>{liveData.subject_code}</Text>

        <View style={styles.avatarContainer}>
          <View style={styles.avatarBox}>
            {isCameraOpen ? (
              <CameraView 
                style={StyleSheet.absoluteFill} 
                facing="front" 
                ref={cameraRef}
              />
            ) : (
              <Ionicons name="person-outline" size={96} color={colors.secondary} />
            )}
          </View>
          {isCameraOpen && (
            <Text style={styles.framingHint}>
              Keep your whole face inside the box - only what you can see here is sent.
            </Text>
          )}
        </View>

        <View style={styles.statsRow}>
          <Text style={styles.statsText}>
            Recognized so far: <Text style={styles.statsHighlight}>{liveData.recognised}</Text>
          </Text>
        </View>

        {user?.role === 'student' && (
          <Button
            title={isCameraOpen ? "Verify My Face" : "Join Attendance Session"}
            onPress={handleJoinSession}
            loading={isVerifying}
            style={styles.joinButton}
          />
        )}

        <Text style={styles.listTitle}>Recently Recognized</Text>
        {liveData.students.length === 0 ? (
          <Text style={styles.emptyText}>No students recognized yet.</Text>
        ) : (
          liveData.students.map((student) => (
            <Card key={student.student_id} style={styles.studentCard}>
              <View style={styles.studentInfo}>
                <Text style={styles.studentName}>{student.name}</Text>
                <Text style={styles.studentId}>{student.student_id}</Text>
              </View>
              <View style={styles.studentStatus}>
                <Text style={styles.timeText}>{student.time_in}</Text>
                <Text style={[styles.statusText, { color: colors.statusPresent }]}>
                  {student.status}
                </Text>
              </View>
            </Card>
          ))
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  center: {
    flexGrow: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: spacing.xl,
  },
  noSessionTitle: {
    ...typography.h2,
    marginTop: spacing.lg,
    marginBottom: spacing.xs,
  },
  noSessionSubtitle: {
    ...typography.body,
    color: colors.textSecondary,
    textAlign: 'center',
  },
  content: {
    flexGrow: 1,
    padding: spacing.lg,
  },
  sectionTitle: {
    ...typography.h3,
    color: colors.secondary,
    marginBottom: spacing.sm,
  },
  subjectCode: {
    ...typography.h2,
    color: colors.secondary,
    marginBottom: spacing.lg,
  },
  avatarContainer: {
    alignItems: 'center',
    marginBottom: spacing.xl,
  },
  // ⚠️ 16:9, matching the crop in ../utils/frame.js. A CameraView covers its
  // box, so this shape *is* the window that gets uploaded. The square this
  // used to be showed more of the frame than the server ever received, so a
  // correctly framed face could still be cropped away server-side and come
  // back as "No face detected."
  avatarBox: {
    width: '100%',
    aspectRatio: FRAME_ASPECT,
    backgroundColor: colors.primary,
    justifyContent: 'center',
    alignItems: 'center',
    overflow: 'hidden',
  },
  framingHint: {
    ...typography.caption,
    color: colors.textSecondary,
    textAlign: 'center',
    marginTop: spacing.sm,
  },
  statsRow: {
    alignItems: 'center',
    marginBottom: spacing.xl,
  },
  statsText: {
    ...typography.body,
    color: colors.textSecondary,
  },
  statsHighlight: {
    ...typography.h3,
    color: colors.secondary,
  },
  joinButton: {
    marginBottom: spacing.xl,
  },
  listTitle: {
    ...typography.h3,
    marginBottom: spacing.md,
  },
  emptyText: {
    ...typography.body,
    color: colors.textSecondary,
    fontStyle: 'italic',
  },
  studentCard: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: spacing.sm,
    padding: spacing.md,
  },
  studentInfo: {
    flex: 1,
  },
  studentName: {
    ...typography.body,
    fontWeight: '600',
  },
  studentId: {
    ...typography.caption,
  },
  studentStatus: {
    alignItems: 'flex-end',
  },
  timeText: {
    ...typography.caption,
    marginBottom: 4,
  },
  statusText: {
    ...typography.bodySmall,
    fontWeight: '700',
  },
});
