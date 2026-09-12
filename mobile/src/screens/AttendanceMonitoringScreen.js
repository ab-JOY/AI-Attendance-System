/**
 * Attendance Monitoring Screen.
 * Shows live session status and allows students to join the session.
 */

import React, { useState, useEffect } from 'react';
import { View, StyleSheet, Text, ScrollView, RefreshControl, Alert } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Button from '../components/Button';
import Header from '../components/Header';
import Card from '../components/Card';
import { colors, spacing, typography, borderRadius } from '../theme/colors';

export default function AttendanceMonitoringScreen() {
  const { user } = useAuth();
  const [liveData, setLiveData] = useState(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isJoining, setIsJoining] = useState(false);

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

  const handleJoinSession = () => {
    // In this app architecture, joining a session implies opening the phone camera
    // to run face recognition, similar to FaceCaptureScreen but for attendance.
    // However, the backend recognize_face.py relies on a server-side camera.
    // For now, we will show an alert that this feature requires the server-side camera
    // or a dedicated mobile recognition endpoint.
    Alert.alert(
      'Join Session',
      'This feature will open your camera to verify your face and mark you present.',
      [{ text: 'OK' }]
    );
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
            <Ionicons name="person-outline" size={120} color={colors.secondary} />
          </View>
        </View>

        <View style={styles.statsRow}>
          <Text style={styles.statsText}>
            Recognized so far: <Text style={styles.statsHighlight}>{liveData.recognised}</Text>
          </Text>
        </View>

        {user?.role === 'student' && (
          <Button
            title="Join Attendance Session"
            onPress={handleJoinSession}
            loading={isJoining}
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
  avatarBox: {
    width: 250,
    height: 250,
    backgroundColor: colors.primary,
    justifyContent: 'center',
    alignItems: 'center',
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
