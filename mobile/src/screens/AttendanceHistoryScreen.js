/**
 * Attendance History Screen.
 * Lists historical attendance records. Filtered automatically by role on backend.
 */

import React, { useState, useEffect } from 'react';
import { View, StyleSheet, Text, FlatList, RefreshControl } from 'react-native';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Header from '../components/Header';
import Card from '../components/Card';
import { colors, spacing, typography } from '../theme/colors';

export default function AttendanceHistoryScreen() {
  const { user } = useAuth();
  const [records, setRecords] = useState([]);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchHistory = async () => {
    try {
      setError('');
      const response = await client.get('/api/attendance/history');
      if (response.data.success) {
        setRecords(response.data.records);
      } else {
        setError(response.data.message);
      }
    } catch (err) {
      setError('Could not load attendance history.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHistory();
  }, []);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await fetchHistory();
    setIsRefreshing(false);
  };

  const renderItem = ({ item }) => {
    const isPresent = item.status === 'Present';
    
    return (
      <Card style={styles.card}>
        <View style={styles.cardHeader}>
          <Text style={styles.date}>{item.attendance_date}</Text>
          <View style={[styles.statusBadge, isPresent ? styles.badgePresent : styles.badgeAbsent]}>
            <Text style={[styles.statusText, isPresent ? styles.statusTextPresent : styles.statusTextAbsent]}>
              {item.status}
            </Text>
          </View>
        </View>
        
        <Text style={styles.subject}>{item.subject_code} - {item.subject_name}</Text>
        
        {user?.role !== 'student' && (
          <Text style={styles.studentName}>{item.student_name} ({item.student_id})</Text>
        )}
        
        {isPresent && item.time_in && (
          <Text style={styles.timeIn}>Time In: {item.time_in}</Text>
        )}
      </Card>
    );
  };

  return (
    <View style={styles.container}>
      <Header title="Attendance History" />
      
      {error ? (
        <Text style={styles.errorText}>{error}</Text>
      ) : null}
      
      <FlatList
        data={records}
        keyExtractor={(item) => item.id.toString()}
        renderItem={renderItem}
        contentContainerStyle={styles.listContent}
        refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
        ListEmptyComponent={
          !loading && (
            <Text style={styles.emptyText}>No attendance records found.</Text>
          )
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  listContent: {
    padding: spacing.lg,
  },
  card: {
    marginBottom: spacing.md,
    padding: spacing.md,
  },
  cardHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: spacing.sm,
  },
  date: {
    ...typography.body,
    fontWeight: '600',
  },
  statusBadge: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    borderRadius: 12,
  },
  badgePresent: {
    backgroundColor: '#E6FFFA', // Light green
  },
  badgeAbsent: {
    backgroundColor: '#FED7D7', // Light red
  },
  statusText: {
    ...typography.caption,
    fontWeight: '700',
  },
  statusTextPresent: {
    color: colors.statusPresent,
  },
  statusTextAbsent: {
    color: colors.statusAbsent,
  },
  subject: {
    ...typography.body,
    color: colors.secondary,
    marginBottom: spacing.xs,
  },
  studentName: {
    ...typography.bodySmall,
    color: colors.textSecondary,
    marginBottom: spacing.xs,
  },
  timeIn: {
    ...typography.caption,
    color: colors.textSecondary,
    marginTop: spacing.xs,
  },
  emptyText: {
    ...typography.body,
    color: colors.textSecondary,
    textAlign: 'center',
    marginTop: spacing.xxl,
  },
  errorText: {
    ...typography.body,
    color: colors.error,
    textAlign: 'center',
    margin: spacing.lg,
  },
});
