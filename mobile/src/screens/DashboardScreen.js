/**
 * Dashboard Screen.
 * The student's welcome screen. The staff overview it also had (counts from
 * /api/dashboard) went when the app became student-only.
 */

import React from 'react';
import { View, StyleSheet, Text, ScrollView } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useAuth } from '../context/AuthContext';
import Header from '../components/Header';
import { colors, spacing, typography } from '../theme/colors';

export default function DashboardScreen() {
  const { user } = useAuth();

  return (
    <View style={styles.container}>
      <Header title="Dashboard" subtitle={`Welcome, ${user?.display_name || ''}`} />
      <ScrollView contentContainerStyle={styles.center}>
        <Ionicons name="school-outline" size={64} color={colors.primary} style={styles.icon} />
        <Text style={styles.welcomeText}>Welcome to PRMSU Attendance</Text>
        <Text style={styles.subText}>
          Navigate to the Attendance tab to view your history, or Monitoring to join a session.
        </Text>
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
  icon: {
    marginBottom: spacing.md,
  },
  welcomeText: {
    ...typography.h2,
    textAlign: 'center',
    marginBottom: spacing.xs,
  },
  subText: {
    ...typography.body,
    color: colors.textSecondary,
    textAlign: 'center',
  },
});
