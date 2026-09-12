/**
 * Login Screen.
 * Role selection, ID, password, and link to student registration.
 */

import React, { useState, useEffect } from 'react';
import {
  View,
  StyleSheet,
  Text,
  TouchableOpacity,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  Image,
} from 'react-native';
import { useAuth } from '../context/AuthContext';
import Input from '../components/Input';
import Button from '../components/Button';
import Card from '../components/Card';
import { colors, spacing, typography, borderRadius } from '../theme/colors';
import * as SecureStore from 'expo-secure-store';

export default function LoginScreen({ navigation }) {
  const { login, isAuthenticated } = useAuth();
  const [role, setRole] = useState('student');
  const [userId, setUserId] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (isAuthenticated) {
      navigation.replace('Main');
    }
  }, [isAuthenticated, navigation]);

  const handleLogin = async () => {
    if (!userId.trim() || !password.trim()) {
      setError('Please enter both ID and password');
      return;
    }

    setIsLoading(true);
    setError('');

    const result = await login(role, userId, password);
    
    if (result.success) {
      navigation.replace('Main');
    } else {
      setError(result.message || 'Login failed');
    }
    
    setIsLoading(false);
  };

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.logoContainer}>
          <View style={styles.logoPlaceholder}>
            <Text style={styles.logoText}>PRMSU</Text>
          </View>
          <Text style={styles.title}>PRMSU Attendance System</Text>
          <Text style={styles.subtitle}>AI-Based Facial Recognition with Liveness Detection</Text>
        </View>

        <View style={styles.roleSelector}>
          {['student', 'instructor', 'admin'].map((r) => (
            <TouchableOpacity
              key={r}
              style={[
                styles.roleButton,
                role === r && styles.roleButtonActive,
              ]}
              onPress={() => setRole(r)}
            >
              <Text
                style={[
                  styles.roleText,
                  role === r && styles.roleTextActive,
                ]}
              >
                {r.charAt(0).toUpperCase() + r.slice(1)}
              </Text>
            </TouchableOpacity>
          ))}
        </View>

        <Card style={styles.card}>
          <Input
            placeholder={role === 'admin' ? "Username" : role === 'student' ? "Student Number" : "Instructor ID"}
            value={userId}
            onChangeText={setUserId}
            icon="person-outline"
            autoCapitalize="none"
          />
          <Input
            placeholder="Password"
            value={password}
            onChangeText={setPassword}
            icon="lock-closed-outline"
            secureTextEntry
          />
          
          {error ? <Text style={styles.errorText}>{error}</Text> : null}

          <Button
            title="Log-In"
            onPress={handleLogin}
            loading={isLoading}
            style={styles.loginButton}
          />
        </Card>

        {role === 'student' && (
          <View style={styles.registerContainer}>
            <Text style={styles.registerText}>
              Students who do not have an account can register below
            </Text>
            <TouchableOpacity onPress={() => navigation.navigate('StudentRegistration')}>
              <Text style={styles.registerLink}>Register as a student</Text>
            </TouchableOpacity>
          </View>
        )}

        <TouchableOpacity 
          style={styles.settingsLink}
          onPress={() => navigation.navigate('Settings')}
        >
          <Text style={styles.settingsText}>Configure Server URL</Text>
        </TouchableOpacity>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  content: {
    flexGrow: 1,
    padding: spacing.lg,
    justifyContent: 'center',
  },
  logoContainer: {
    alignItems: 'center',
    marginBottom: spacing.xl,
  },
  logoPlaceholder: {
    width: 100,
    height: 100,
    borderRadius: 50,
    backgroundColor: colors.primary,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: spacing.md,
    borderWidth: 2,
    borderColor: colors.secondary,
  },
  logoText: {
    ...typography.h3,
    color: colors.secondary,
  },
  title: {
    ...typography.h2,
    textAlign: 'center',
    marginBottom: spacing.xs,
  },
  subtitle: {
    ...typography.bodySmall,
    textAlign: 'center',
    paddingHorizontal: spacing.xl,
  },
  roleSelector: {
    flexDirection: 'row',
    justifyContent: 'center',
    marginBottom: spacing.lg,
    backgroundColor: colors.surface,
    borderRadius: borderRadius.pill,
    padding: 4,
  },
  roleButton: {
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
    borderRadius: borderRadius.pill,
  },
  roleButtonActive: {
    backgroundColor: colors.primary,
  },
  roleText: {
    ...typography.bodySmall,
    color: colors.textSecondary,
    fontWeight: '600',
  },
  roleTextActive: {
    color: colors.textOnPrimary,
  },
  card: {
    marginBottom: spacing.xl,
  },
  loginButton: {
    marginTop: spacing.md,
  },
  errorText: {
    ...typography.caption,
    color: colors.error,
    textAlign: 'center',
    marginBottom: spacing.sm,
  },
  registerContainer: {
    alignItems: 'center',
  },
  registerText: {
    ...typography.bodySmall,
    textAlign: 'center',
    marginBottom: spacing.md,
  },
  registerLink: {
    ...typography.button,
    color: colors.secondary,
  },
  settingsLink: {
    alignItems: 'center',
    marginTop: spacing.xxl,
  },
  settingsText: {
    ...typography.caption,
    color: colors.textSecondary,
    textDecorationLine: 'underline',
  },
});
