/**
 * Dashboard Screen.
 * Overview stats for admin/instructor, empty state for student.
 */

import React, { useState, useEffect } from 'react';
import { View, StyleSheet, Text, ScrollView, RefreshControl } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Header from '../components/Header';
import Card from '../components/Card';
import { colors, spacing, typography, borderRadius } from '../theme/colors';

export default function DashboardScreen() {
  const { user } = useAuth();
  const [stats, setStats] = useState(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchStats = async () => {
    try {
      const response = await client.get('/api/dashboard');
      if (response.data.success) {
        setStats(response.data.counters);
      }
    } catch (err) {
      console.log('Error fetching dashboard stats:', err);
    }
  };

  useEffect(() => {
    if (user?.role !== 'student') {
      fetchStats();
    }
  }, [user]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    if (user?.role !== 'student') {
      await fetchStats();
    }
    setIsRefreshing(false);
  };

  if (user?.role === 'student') {
    return (
      <View style={styles.container}>
        <Header title="Dashboard" subtitle={`Welcome, ${user.display_name}`} />
        <ScrollView
          contentContainerStyle={styles.center}
          refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
        >
          <Ionicons name="school-outline" size={64} color={colors.primary} style={styles.icon} />
          <Text style={styles.welcomeText}>Welcome to PRMSU Attendance</Text>
          <Text style={styles.subText}>
            Navigate to the Attendance tab to view your history, or Monitoring to join a session.
          </Text>
        </ScrollView>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Header title="Dashboard" subtitle={`Welcome, ${user?.display_name || 'Admin'}`} />
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
      >
        <Text style={styles.sectionTitle}>Overview</Text>
        
        <View style={styles.grid}>
          <Card style={styles.statCard}>
            <Ionicons name="people-outline" size={32} color={colors.secondary} />
            <Text style={styles.statValue}>{stats?.students || 0}</Text>
            <Text style={styles.statLabel}>Students Enrolled</Text>
          </Card>
          
          <Card style={styles.statCard}>
            <Ionicons name="book-outline" size={32} color={colors.secondary} />
            <Text style={styles.statValue}>{stats?.subjects || 0}</Text>
            <Text style={styles.statLabel}>Subjects</Text>
          </Card>
          
          <Card style={styles.statCard}>
            <Ionicons name="checkmark-done-outline" size={32} color={colors.secondary} />
            <Text style={styles.statValue}>{stats?.attendance_today || 0}</Text>
            <Text style={styles.statLabel}>Attendance Today</Text>
          </Card>
          
          <Card style={styles.statCard}>
            <Ionicons name="videocam-outline" size={32} color={colors.secondary} />
            <Text style={styles.statValue}>{stats?.active_sessions || 0}</Text>
            <Text style={styles.statLabel}>Active Sessions</Text>
          </Card>
        </View>

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
  content: {
    padding: spacing.lg,
  },
  sectionTitle: {
    ...typography.h3,
    marginBottom: spacing.md,
  },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    justifyContent: 'space-between',
  },
  statCard: {
    width: '48%',
    marginBottom: spacing.md,
    alignItems: 'center',
    paddingVertical: spacing.xl,
  },
  statValue: {
    ...typography.h1,
    marginTop: spacing.md,
    marginBottom: spacing.xs,
  },
  statLabel: {
    ...typography.bodySmall,
    textAlign: 'center',
  },
});
