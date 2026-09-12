/**
 * Profile Screen.
 * Shows user details and logout button.
 */

import React, { useState, useEffect } from 'react';
import { View, StyleSheet, Text, ScrollView, RefreshControl } from 'react-native';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Header from '../components/Header';
import Button from '../components/Button';
import Card from '../components/Card';
import { colors, spacing, typography, borderRadius } from '../theme/colors';
import { Ionicons } from '@expo/vector-icons';

export default function ProfileScreen() {
  const { logout, user } = useAuth();
  const [profile, setProfile] = useState(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchProfile = async () => {
    try {
      const response = await client.get('/api/me');
      if (response.data.success) {
        setProfile(response.data.profile);
      }
    } catch (err) {
      console.log('Error fetching profile:', err);
    }
  };

  useEffect(() => {
    fetchProfile();
  }, []);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await fetchProfile();
    setIsRefreshing(false);
  };

  return (
    <View style={styles.container}>
      <Header title="Profile" />
      
      <ScrollView 
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
      >
        <View style={styles.avatarContainer}>
          <View style={styles.avatar}>
            <Text style={styles.avatarText}>
              {user?.display_name?.charAt(0).toUpperCase() || 'U'}
            </Text>
          </View>
          <Text style={styles.name}>{user?.display_name}</Text>
          <Text style={styles.role}>{user?.role.toUpperCase()}</Text>
        </View>

        <Card style={styles.infoCard}>
          <Text style={styles.sectionTitle}>Account Details</Text>
          
          <View style={styles.infoRow}>
            <Ionicons name="id-card-outline" size={20} color={colors.secondary} style={styles.icon} />
            <View>
              <Text style={styles.infoLabel}>ID Number</Text>
              <Text style={styles.infoValue}>{user?.user_id}</Text>
            </View>
          </View>
          
          {profile?.role === 'student' && (
            <>
              <View style={styles.divider} />
              <View style={styles.infoRow}>
                <Ionicons name="business-outline" size={20} color={colors.secondary} style={styles.icon} />
                <View>
                  <Text style={styles.infoLabel}>College / Department</Text>
                  <Text style={styles.infoValue}>{profile.college_department || 'N/A'}</Text>
                </View>
              </View>
              
              <View style={styles.divider} />
              <View style={styles.infoRow}>
                <Ionicons name="school-outline" size={20} color={colors.secondary} style={styles.icon} />
                <View>
                  <Text style={styles.infoLabel}>Program</Text>
                  <Text style={styles.infoValue}>{profile.program || 'N/A'}</Text>
                </View>
              </View>
              
              <View style={styles.divider} />
              <View style={styles.infoRow}>
                <Ionicons name="layers-outline" size={20} color={colors.secondary} style={styles.icon} />
                <View>
                  <Text style={styles.infoLabel}>Year & Section</Text>
                  <Text style={styles.infoValue}>
                    {profile.year_level || 'N/A'} - {profile.section || 'N/A'}
                  </Text>
                </View>
              </View>
            </>
          )}
        </Card>

        <Button 
          title="Log Out" 
          onPress={logout} 
          variant="outline"
          style={styles.logoutButton} 
        />
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  content: {
    padding: spacing.lg,
  },
  avatarContainer: {
    alignItems: 'center',
    marginBottom: spacing.xl,
    marginTop: spacing.md,
  },
  avatar: {
    width: 100,
    height: 100,
    borderRadius: 50,
    backgroundColor: colors.primary,
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: spacing.md,
  },
  avatarText: {
    ...typography.h1,
    color: colors.secondary,
  },
  name: {
    ...typography.h2,
    marginBottom: spacing.xs,
  },
  role: {
    ...typography.caption,
    backgroundColor: colors.surface,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: borderRadius.pill,
    overflow: 'hidden',
  },
  infoCard: {
    marginBottom: spacing.xl,
  },
  sectionTitle: {
    ...typography.h3,
    marginBottom: spacing.lg,
  },
  infoRow: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  icon: {
    marginRight: spacing.md,
    width: 24,
  },
  infoLabel: {
    ...typography.caption,
    marginBottom: 2,
  },
  infoValue: {
    ...typography.body,
  },
  divider: {
    height: 1,
    backgroundColor: colors.border,
    marginVertical: spacing.md,
    marginLeft: 40, // align with text
  },
  logoutButton: {
    marginBottom: spacing.xxl,
  },
});
