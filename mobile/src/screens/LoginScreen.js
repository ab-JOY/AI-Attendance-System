/**
 * Login Screen.
 * Student number and password. The app is for students only: administrators
 * and instructors use the website, and /api/login issues no token to them.
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
import { colors, spacing, typography } from '../theme/colors';
import * as SecureStore from 'expo-secure-store';

export default function LoginScreen({ navigation }) {
  const { login, isAuthenticated } = useAuth();
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

    const result = await login(userId, password);
    
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

        <Card style={styles.card}>
          <Input
            placeholder="Student Number"
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

        {/* Students cannot register here: only an administrator or an
            instructor registers a student and captures their face, on the
            website. */}
        <View style={styles.registerContainer}>
          <Text style={styles.registerText}>
            No account yet? Ask your instructor or the administrator to register you and capture your face.
          </Text>
        </View>

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
